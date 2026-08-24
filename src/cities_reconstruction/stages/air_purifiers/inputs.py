"""Catalog and normalized-feature input parsing for air-purifier placement."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from cities_reconstruction.config import ConfigError
from cities_reconstruction.geometry.stl_regions import mesh_bounds, read_region_stl
from cities_reconstruction.stages.air_purifiers.models import AirPurifierModel


def load_model_library(path: Path) -> dict[str, AirPurifierModel]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid air-purifier model catalog: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ConfigError(f"air-purifier model catalog schema_version must be 1: {path}")
    raw_models = payload.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ConfigError(f"air-purifier model catalog must contain non-empty models: {path}")
    models: dict[str, AirPurifierModel] = {}
    for index, raw in enumerate(raw_models, start=1):
        if not isinstance(raw, dict):
            raise ConfigError(f"invalid model entry {index} in {path}")
        name = required_text(raw, "name", f"model entry {index}")
        if name in models:
            raise ConfigError(f"duplicate air-purifier model name {name!r} in {path}")
        kind = required_text(raw, "kind", f"model {name!r}")
        height = positive_number(raw.get("height_m"), f"model {name!r} height_m")
        tolerance = positive_number(raw.get("linear_tolerance_m"), f"model {name!r} linear_tolerance_m")
        if kind == "octagonal":
            width = depth = positive_number(raw.get("base_width_m"), f"model {name!r} base_width_m")
        elif kind == "four_side":
            width = positive_number(raw.get("width_m"), f"model {name!r} width_m")
            depth = positive_number(raw.get("depth_m"), f"model {name!r} depth_m")
        else:
            raise ConfigError(f"unknown air-purifier catalog kind {kind!r} for model {name!r}")
        output_path = Path(required_text(raw, "output_path", f"model {name!r}"))
        if output_path.is_absolute():
            raise ConfigError(
                f"air-purifier model {name!r} requires a relative output_path in catalog {path}"
            )
        source_path = (path.parent / output_path).resolve()
        mesh = read_region_stl(source_path)
        bounds = mesh_bounds(mesh)
        actual = (bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4])
        expected = (width, depth, height)
        if abs(bounds[4]) > tolerance or any(
            abs(left - right) > tolerance
            for left, right in zip(actual, expected, strict=True)
        ):
            raise ConfigError(
                f"air-purifier model {name!r} bounds {actual!r} and base z={bounds[4]} "
                f"do not match catalog dimensions {expected!r} within {tolerance} m: {source_path}"
            )
        models[name] = AirPurifierModel(name, kind, source_path, width, depth, height, tolerance, mesh)
    return models


def load_features(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid air-purifier GeoJSON: {path}") from exc
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise ConfigError(f"air-purifier GeoJSON must be a FeatureCollection: {path}")
    if not payload["features"]:
        raise ConfigError("no air-purifier features to generate")
    return payload["features"]


def target_dimension(properties: dict[str, Any], key: str, field: str, default: float, model_name: str) -> tuple[float, str]:
    value = properties.get(key)
    if value is None:
        return default, f"default:{model_name}"
    return positive_number(value, key), f"attribute:{field}"


def required_text(payload: dict[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} requires non-empty {key}")
    return value.strip()


def required_choice(
    payload: dict[str, Any],
    key: str,
    allowed: tuple[str, ...],
    context: str,
) -> str:
    value = required_text(payload, key, context)
    if value not in allowed:
        choices = ", ".join(allowed)
        raise ConfigError(f"{context} {key} must be one of {choices}; got {value!r}")
    return value


def non_negative_integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{context} must be a non-negative integer")
    return value


def finite_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{context} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{context} must be a finite number")
    return result


def positive_number(value: Any, context: str) -> float:
    result = finite_number(value, context)
    if result <= 0.0:
        raise ConfigError(f"{context} must be positive")
    return result
