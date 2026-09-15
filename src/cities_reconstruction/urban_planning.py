"""Strict loading and normalization for externally authored planning points."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, TypeGuard, cast

from cities_reconstruction.config import AppConfig, ConfigError
from cities_reconstruction.geometry.crs import (
    EARTH_RADIUS_M,
    WEB_MERCATOR,
    WEB_MERCATOR_LIMIT_M,
    WGS84,
    crs_equal,
    transform_xy,
    web_mercator_to_lonlat,
)

SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
KINDS = frozenset({"tree", "air_purifier"})
MODELLING_PROPERTIES_BY_KIND = {
    "tree": frozenset({"height_m", "crown_diameter_m", "trunk_diameter_m"}),
    "air_purifier": frozenset({"height_m", "width_m", "depth_m", "rotation_deg"}),
}
MODELLING_PROPERTIES = frozenset(
    {"id", "kind", "model"}.union(*MODELLING_PROPERTIES_BY_KIND.values())
)


class UrbanPlanningPointGeometry(TypedDict):
    """Normalized WGS84 Point geometry emitted by the planning loader."""

    type: Literal["Point"]
    coordinates: list[float]


class UrbanPlanningProperties(TypedDict):
    """Validated properties emitted for one urban-planning feature."""

    id: str
    kind: str
    model: str
    height_m: NotRequired[float]
    crown_diameter_m: NotRequired[float]
    trunk_diameter_m: NotRequired[float]
    width_m: NotRequired[float]
    depth_m: NotRequired[float]
    rotation_deg: NotRequired[float]
    urban_planning_input_id: str
    source: str
    source_crs: str
    source_feature_index: int
    source_properties: dict[str, object]
    roi_zone: str
    roi_distance_m: float
    contributes_to_geometry: bool


class UrbanPlanningFeature(TypedDict):
    """Normalized GeoJSON Feature emitted by the planning loader."""

    type: Literal["Feature"]
    geometry: UrbanPlanningPointGeometry
    properties: UrbanPlanningProperties


@dataclass(frozen=True)
class UrbanPlanningLoadResult:
    accepted_features: tuple[UrbanPlanningFeature, ...]
    outside_roi_features: tuple[UrbanPlanningFeature, ...]
    per_input: dict[str, dict[str, int]]

    @property
    def outside_roi(self) -> tuple[UrbanPlanningFeature, ...]:
        """Return the normalized features rejected only by the ROI policy."""

        return self.outside_roi_features


def load_inputs(config: AppConfig) -> UrbanPlanningLoadResult:
    """Load enabled planning GeoJSON inputs and normalize accepted Point features."""

    accepted: list[UrbanPlanningFeature] = []
    outside_roi: list[UrbanPlanningFeature] = []
    per_input: dict[str, dict[str, int]] = {}
    seen_ids: dict[str, tuple[str, int]] = {}
    model_names = {
        "tree": _model_names(config.trees.model_library_path, "tree"),
        "air_purifier": _model_names(config.air_purifiers.model_library_path, "air purifier"),
    }

    for planning_input in config.urban_planning.inputs:
        counts = {"source_features": 0, "accepted_features": 0, "outside_roi": 0}
        per_input[planning_input.name] = counts
        if not planning_input.enabled:
            continue
        source_features = _load_collection(planning_input.path, planning_input.name)
        counts["source_features"] = len(source_features)
        for feature_index, raw_feature in enumerate(source_features):
            normalized = _normalize_feature(
                raw_feature,
                input_name=planning_input.name,
                source_path=planning_input.path,
                source_crs=planning_input.crs,
                feature_index=feature_index,
                config=config,
                model_names=model_names,
            )
            feature_id = normalized["properties"]["id"]
            previous = seen_ids.get(feature_id)
            if previous is not None:
                raise ConfigError(
                    f"urban-planning input '{planning_input.name}' feature {feature_index} ({feature_id}) "
                    f"has duplicate id '{feature_id}'; already used by input '{previous[0]}' feature {previous[1]}"
                )
            seen_ids[feature_id] = (planning_input.name, feature_index)
            if normalized["properties"]["roi_zone"] == "outside":
                outside_roi.append(normalized)
                counts["outside_roi"] += 1
            else:
                accepted.append(normalized)
                counts["accepted_features"] += 1

    return UrbanPlanningLoadResult(tuple(accepted), tuple(outside_roi), per_input)


def _load_collection(path: Path, input_name: str) -> list[object]:
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read urban-planning input '{input_name}' GeoJSON {path}: {error}") from error
    if not _is_object_dict(payload) or payload.get("type") != "FeatureCollection":
        raise ConfigError(f"urban-planning input '{input_name}' must be a GeoJSON FeatureCollection")
    features = payload.get("features")
    if not _is_object_list(features):
        raise ConfigError(f"urban-planning input '{input_name}' FeatureCollection features must be an array")
    return features


def _normalize_feature(
    raw_feature: object,
    *,
    input_name: str,
    source_path: Path,
    source_crs: str,
    feature_index: int,
    config: AppConfig,
    model_names: dict[str, frozenset[str] | None],
) -> UrbanPlanningFeature:
    base_context = f"urban-planning input '{input_name}' feature {feature_index}"
    if not _is_object_dict(raw_feature) or raw_feature.get("type") != "Feature":
        raise ConfigError(f"{base_context} must be a GeoJSON Feature")
    raw_properties = raw_feature.get("properties")
    if not _is_object_dict(raw_properties):
        raise ConfigError(f"{base_context} properties must be an object")
    properties = _casefold_properties(raw_properties, base_context)
    raw_id = properties.get("id")
    display_id = raw_id if isinstance(raw_id, str) and raw_id else "<missing>"
    context = f"{base_context} ({display_id})"
    feature_id = _required_text(properties, "id", context)
    if not SAFE_ID_PATTERN.fullmatch(feature_id):
        raise ConfigError(f"{context} has unsafe id '{feature_id}'; expected {SAFE_ID_PATTERN.pattern}")
    kind = _required_text(properties, "kind", context)
    if kind not in KINDS:
        raise ConfigError(f"{context} has unknown kind '{kind}'; expected one of: {', '.join(sorted(KINDS))}")
    model = _required_text(properties, "model", context)
    allowed_models = model_names[kind]
    if allowed_models is not None and model not in allowed_models:
        raise ConfigError(f"{context} has unknown model '{model}' for kind '{kind}'")
    _validate_modelling_properties(properties, kind, context)

    lon, lat = _point_coordinates(raw_feature.get("geometry"), source_crs, context)
    roi_distance_m = _distance_m(config.region.center_lat, config.region.center_lon, lat, lon)
    roi_zone = _roi_zone(roi_distance_m, config)
    public_properties: dict[str, object] = {
        "id": feature_id,
        "kind": kind,
        "model": model,
    }
    for name in sorted(MODELLING_PROPERTIES_BY_KIND[kind] - {"rotation_deg"}):
        if name in properties:
            public_properties[name] = _finite_number(properties[name], name, context, positive=True)
    if "rotation_deg" in properties:
        public_properties["rotation_deg"] = _finite_number(
            properties["rotation_deg"], "rotation_deg", context, positive=False
        )
    public_properties.update(
        {
            "urban_planning_input_id": input_name,
            "source": str(source_path),
            "source_crs": source_crs,
            "source_feature_index": feature_index,
            "source_properties": {
                key: value
                for key, value in raw_properties.items()
                if isinstance(key, str) and key.lower() not in MODELLING_PROPERTIES
            },
            "roi_zone": roi_zone,
            "roi_distance_m": round(roi_distance_m, 3),
            "contributes_to_geometry": False,
        }
    )
    return UrbanPlanningFeature(
        type="Feature",
        geometry=UrbanPlanningPointGeometry(type="Point", coordinates=[lon, lat]),
        properties=cast(UrbanPlanningProperties, public_properties),
    )


def _casefold_properties(raw: dict[object, object], context: str) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ConfigError(f"{context} property names must be strings")
        public_name = key.lower()
        if public_name in normalized:
            raise ConfigError(f"{context} contains duplicate property name after lowercasing: {public_name}")
        normalized[public_name] = value
    return normalized


def _required_text(properties: dict[str, object], name: str, context: str) -> str:
    value = properties.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} requires {name} as a non-empty string")
    return value.strip()


def _validate_modelling_properties(properties: dict[str, object], kind: str, context: str) -> None:
    for name in properties:
        if name in MODELLING_PROPERTIES and name not in {"id", "kind", "model", *MODELLING_PROPERTIES_BY_KIND[kind]}:
            raise ConfigError(f"{context} property '{name}' is not allowed for kind '{kind}'")
        if name not in MODELLING_PROPERTIES and (name.endswith("_m") or name.startswith("rotation")):
            raise ConfigError(f"{context} has unknown modelling property '{name}'")


def _finite_number(value: object, name: str, context: str, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise ConfigError(f"{context} {name} must be a finite number")
    number = float(value)
    if positive and number <= 0.0:
        raise ConfigError(f"{context} {name} must be a finite positive number")
    return number


def _point_coordinates(geometry: object, source_crs: str, context: str) -> tuple[float, float]:
    if not _is_object_dict(geometry) or geometry.get("type") != "Point":
        raise ConfigError(f"{context} geometry must be a GeoJSON Point")
    coordinates = geometry.get("coordinates")
    if not _is_object_list(coordinates) or len(coordinates) != 2:
        raise ConfigError(f"{context} Point coordinates must contain exactly two values")
    first_value, second_value = coordinates
    if not _is_number(first_value) or not _is_number(second_value):
        raise ConfigError(f"{context} Point coordinates must be finite numbers")
    first, second = float(first_value), float(second_value)
    if not math.isfinite(first) or not math.isfinite(second):
        raise ConfigError(f"{context} Point coordinates must be finite numbers")
    if source_crs == WEB_MERCATOR:
        return _inverse_web_mercator(first, second, context)
    if not crs_equal(source_crs, WGS84):
        try:
            first, second = transform_xy(first, second, source_crs, WGS84)
        except ConfigError as exc:
            raise ConfigError(f"{context}: {exc}") from exc
    if not -180.0 <= first <= 180.0:
        raise ConfigError(f"{context} longitude must be between -180 and 180")
    if not -90.0 <= second <= 90.0:
        raise ConfigError(f"{context} latitude must be between -90 and 90")
    return first, second


def _inverse_web_mercator(x: float, y: float, context: str) -> tuple[float, float]:
    if abs(x) > WEB_MERCATOR_LIMIT_M or abs(y) > WEB_MERCATOR_LIMIT_M:
        raise ConfigError(f"{context} EPSG:3857 coordinates exceed Web Mercator bounds")
    if not math.isfinite(x) or not math.isfinite(y):
        raise ConfigError(f"{context} EPSG:3857 coordinates do not transform to finite longitude/latitude")
    lon, lat = web_mercator_to_lonlat(x, y)
    if not math.isfinite(lon) or not math.isfinite(lat):
        raise ConfigError(f"{context} EPSG:3857 coordinates do not transform to finite longitude/latitude")
    return lon, lat


def _model_names(path: Path | None, label: str) -> frozenset[str] | None:
    if path is None:
        return None
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read {label} model library {path}: {error}") from error
    raw_models = payload.get("models") if _is_object_dict(payload) else None
    if not _is_object_list(raw_models):
        raise ConfigError(f"{label} model library must contain a models array: {path}")
    if not raw_models:
        raise ConfigError(f"{label} model library must contain at least one model: {path}")
    names: set[str] = set()
    for index, model in enumerate(raw_models, start=1):
        if not _is_object_dict(model):
            raise ConfigError(f"{label} model library entry {index} must be an object: {path}")
        raw_name = model.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ConfigError(f"{label} model library entry {index} must have a non-empty name: {path}")
        name = raw_name.strip()
        if name in names:
            raise ConfigError(f"{label} model library contains duplicate name '{name}': {path}")
        names.add(name)
    return frozenset(names)


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    """Narrow a JSON object without propagating the decoder's implicit ``Any``."""

    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    """Narrow a JSON array without propagating the decoder's implicit ``Any``."""

    return isinstance(value, list)


def _is_number(value: object) -> TypeGuard[int | float]:
    """Narrow JSON numbers while retaining the established Boolean rejection."""

    return not isinstance(value, bool) and isinstance(value, int | float)


def _roi_zone(distance_m: float, config: AppConfig) -> str:
    if distance_m > config.region.outer_diameter_m / 2.0:
        return "outside"
    if config.region.inner_diameter_m is None:
        return "full"
    return "inner" if distance_m <= config.region.inner_diameter_m / 2.0 else "annular"


def _distance_m(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    phi_a = math.radians(lat_a)
    phi_b = math.radians(lat_b)
    delta_phi = math.radians(lat_b - lat_a)
    delta_lambda = math.radians(lon_b - lon_a)
    haversine = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.atan2(math.sqrt(haversine), math.sqrt(1.0 - haversine))
