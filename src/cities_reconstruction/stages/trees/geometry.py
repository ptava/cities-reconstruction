"""Tree placement resolution and pure parametric mesh construction."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from shapely.geometry import MultiPoint, Point, shape

from cities_reconstruction.config import AppConfig, ConfigError
from cities_reconstruction.stages.trees.inputs import (
    TreeSpeciesModel,
)
from cities_reconstruction.stages.trees.inputs import (
    configured_species_models as _configured_species_models,
)
from cities_reconstruction.stages.trees.inputs import (
    match_category as _match_category,
)
from cities_reconstruction.stages.trees.inputs import (
    normalize_species_name as _normalize_species_name,
)
from cities_reconstruction.stages.trees.inputs import (
    species_category_mapping as _species_category_mapping,
)
from cities_reconstruction.stages.trees.models import TreeInstance

TREE_TERRAIN_CLEARANCE_M = 0.05
CROWN_SEGMENTS = 16
CROWN_RINGS = 8
TRUNK_SEGMENTS = 14
Point3 = tuple[float, float, float]
Triangle = tuple[str, Point3, Point3, Point3]


def build_tree_instances(
    features: list[dict[str, Any]],
    config: AppConfig,
    surface_origin_x: float,
    surface_origin_y: float,
    terrain_sampler: Callable[[float, float], float] | None,
) -> list[TreeInstance]:
    configured_models = _configured_species_models(config)
    category_mapping = _species_category_mapping(config)
    fallback = (
        _fallback_species_model(config, category_mapping, configured_models)
        if any(
            isinstance(feature.get("properties"), dict)
            and feature["properties"].get("direct_model_category") is None
            for feature in features
        )
        else None
    )
    instances: list[TreeInstance] = []
    for index, feature in enumerate(features, start=1):
        point = _feature_reference_point(feature)
        if point is None:
            continue
        properties = feature.get("properties", {})
        tags = properties.get("tags", {})
        if not isinstance(tags, dict):
            tags = {}
        direct_model = properties.get("direct_model_category")
        source_species_key: str | None = None
        source_species: str | None = None
        if direct_model is not None:
            model = _required_category_model(str(direct_model), configured_models)
            model_source = "urban_planning:model"
            species_name = str(direct_model)
        else:
            source_species_key, source_species = _source_species(tags)
        if direct_model is None and source_species:
            mapped_category = category_mapping.get(_normalize_species_name(source_species))
            if mapped_category is None:
                raise ConfigError(
                    f"tree species {source_species!r} is not present in configured species_category_mapping.json"
                )
            matched_model = _match_category(mapped_category, configured_models)
            if matched_model is None:
                raise ConfigError(f"tree species {source_species!r} maps to unavailable category {mapped_category!r}")
            model = matched_model
            model_source = f"tag:{source_species_key}:species_category_mapping"
            species_name = source_species
        elif direct_model is None:
            if fallback is None:
                fallback = _fallback_species_model(config, category_mapping, configured_models)
            default_species, fallback_model = fallback
            model = fallback_model
            model_source = f"default:{default_species}:species_category_mapping"
            species_name = default_species
        x, y = lonlat_to_epsg25832(float(point.x), float(point.y))
        terrain_z = 0.0
        if direct_model is not None:
            def fallback_height(
                tags: dict[str, Any] = tags,
                model: TreeSpeciesModel = model,
            ) -> tuple[float, str]:
                return _tree_height(tags, model)

            height, height_source = _planned_dimension_or(
                properties,
                "height_m",
                fallback_height,
            )

            def fallback_crown_radius(
                tags: dict[str, Any] = tags,
                model: TreeSpeciesModel = model,
                height: float = height,
            ) -> tuple[float, str]:
                return _crown_radius(tags, model, height)

            crown_radius, crown_radius_source = _planned_radius_or(
                properties,
                "crown_diameter_m",
                fallback_crown_radius,
            )

            def fallback_trunk_radius(
                tags: dict[str, Any] = tags,
                model: TreeSpeciesModel = model,
                height: float = height,
            ) -> tuple[float, str]:
                return _trunk_radius(tags, model, height)

            trunk_radius, trunk_radius_source = _planned_radius_or(
                properties,
                "trunk_diameter_m",
                fallback_trunk_radius,
            )
        else:
            height, height_source = _tree_height(tags, model)
            crown_radius, crown_radius_source = _crown_radius(tags, model, height)
            trunk_radius, trunk_radius_source = _trunk_radius(tags, model, height)
        trunk_height = max(1.8, min(height * model.crown_base_fraction, height - 1.0))
        if terrain_sampler is not None:
            terrain_local_x = x - surface_origin_x
            terrain_local_y = y - surface_origin_y
            terrain_z = terrain_sampler(terrain_local_x, terrain_local_y) - TREE_TERRAIN_CLEARANCE_M
        used_tags = tuple(source for source in (model_source, height_source, crown_radius_source, trunk_radius_source) if source.startswith("tag:"))
        defaulted_fields = tuple(
            field
            for field, source in (
                ("species_model", model_source),
                ("height_m", height_source),
                ("crown_radius_m", crown_radius_source),
                ("trunk_radius_m", trunk_radius_source),
            )
            if source.startswith("default")
        )
        instances.append(
            TreeInstance(
                tree_id=_planned_tree_id(properties) if direct_model is not None else f"tree_{index:04d}",
                species=species_name,
                source_species=source_species,
                model_category=model.name,
                crown_shape=model.crown_shape,
                x=x,
                y=y,
                z=terrain_z,
                height_m=height,
                crown_radius_m=crown_radius,
                trunk_radius_m=trunk_radius,
                trunk_height_m=trunk_height,
                roi_zone=str(properties.get("roi_zone", "unknown")),
                osm_id=properties.get("osm_id"),
                model_source=model_source,
                height_source=height_source,
                crown_radius_source=crown_radius_source,
                trunk_radius_source=trunk_radius_source,
                used_tags=used_tags,
                defaulted_fields=defaulted_fields,
            )
        )
    return instances


def _fallback_species_model(
    config: AppConfig,
    category_mapping: dict[str, str],
    models: list[TreeSpeciesModel],
) -> tuple[str, TreeSpeciesModel]:
    default_species = config.trees.default.strip()
    mapped_category = category_mapping.get(_normalize_species_name(default_species))
    if mapped_category is None:
        raise ConfigError(
            f"trees.default {default_species!r} is not present in configured species_category_mapping.json"
        )
    model = _match_category(mapped_category, models)
    if model is None:
        raise ConfigError(f"trees.default {default_species!r} maps to unavailable category {mapped_category!r}")
    return default_species, model


def _feature_reference_point(feature: dict[str, Any]) -> Point | None:
    try:
        geometry = shape(feature["geometry"])
    except (KeyError, TypeError, ValueError):
        return None
    if geometry.is_empty:
        return None
    if isinstance(geometry, Point):
        return geometry
    if isinstance(geometry, MultiPoint):
        return geometry.centroid
    return geometry.representative_point()


def _source_species(tags: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in ("species", "genus", "taxon", "name"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()
    return None, None


def _required_category_model(category: str, models: list[TreeSpeciesModel]) -> TreeSpeciesModel:
    model = _match_category(category, models)
    if model is None:
        raise ConfigError(f"planned tree model category {category!r} is unavailable")
    return model


def _planned_tree_id(properties: dict[str, Any]) -> str:
    value = properties.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("planned tree requires a non-empty id")
    return value.strip()


def _planned_dimension_or(
    properties: dict[str, Any],
    key: str,
    fallback: Callable[[], tuple[float, str]],
) -> tuple[float, str]:
    value = properties.get(key)
    if value is None:
        return fallback()
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)) or value <= 0:
        raise ConfigError(f"planned tree {key} must be a finite positive number")
    return float(value), f"urban_planning:{key}"


def _planned_radius_or(
    properties: dict[str, Any],
    key: str,
    fallback: Callable[[], tuple[float, str]],
) -> tuple[float, str]:
    diameter, source = _planned_dimension_or(properties, key, fallback)
    if source == f"urban_planning:{key}":
        return diameter / 2.0, source
    return diameter, source


def _tree_height(tags: dict[str, Any], model: TreeSpeciesModel) -> tuple[float, str]:
    tag_key, tagged_height = _first_float_tag(tags, ("height", "tree:height"))
    if tagged_height is not None and tagged_height > 1.5:
        return min(tagged_height, 35.0), f"tag:{tag_key}"
    _circumference_key, circumference = _first_float_tag(tags, ("circumference",))
    if circumference is not None and circumference > 0.0:
        return max(4.0, min(model.default_height_m, 6.0 + 2.2 * math.sqrt(circumference))), "allometry:circumference"
    return model.default_height_m, f"default:{model.name}.height_m"


def _crown_radius(tags: dict[str, Any], model: TreeSpeciesModel, height: float) -> tuple[float, str]:
    tag_key, crown_diameter = _first_float_tag(tags, ("crown:diameter", "diameter_crown"))
    if crown_diameter is not None and crown_diameter > 0.5:
        return min(crown_diameter / 2.0, height * 0.48), f"tag:{tag_key}"
    return min(model.default_crown_radius_m, height * 0.42), f"default:{model.name}.crown_radius_m"


def _trunk_radius(tags: dict[str, Any], model: TreeSpeciesModel, height: float) -> tuple[float, str]:
    tag_key, diameter = _first_float_tag(tags, ("diameter", "trunk:diameter"))
    if diameter is not None and diameter > 0.05:
        return diameter / 2.0, f"tag:{tag_key}"
    _circumference_key, circumference = _first_float_tag(tags, ("circumference",))
    if circumference is not None and circumference > 0.1:
        return circumference / (2.0 * math.pi), "allometry:circumference"
    return min(model.default_trunk_radius_m, height * 0.035), f"default:{model.name}.trunk_radius_m"


def _first_float_tag(tags: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, float | None]:
    for key in keys:
        value = _float_tag(tags.get(key))
        if value is not None:
            return key, value
    return None, None


def _float_tag(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = value.lower().replace("metres", "").replace("meters", "").replace("meter", "")
    cleaned = cleaned.replace("m", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def trunk_triangles(instance: TreeInstance) -> list[Triangle]:
    bottom = _circle_points(instance.x, instance.y, instance.z, instance.trunk_radius_m, TRUNK_SEGMENTS)
    top = _circle_points(
        instance.x,
        instance.y,
        instance.z + instance.trunk_height_m,
        instance.trunk_radius_m * 0.72,
        TRUNK_SEGMENTS,
    )
    triangles: list[Triangle] = []
    center_bottom = (instance.x, instance.y, instance.z)
    center_top = (instance.x, instance.y, instance.z + instance.trunk_height_m)
    for index in range(TRUNK_SEGMENTS):
        next_index = (index + 1) % TRUNK_SEGMENTS
        triangles.extend(
            _quad_triangles(f"{instance.tree_id}_trunk", bottom[index], bottom[next_index], top[next_index], top[index])
        )
        triangles.append((f"{instance.tree_id}_trunk_cap", center_bottom, bottom[next_index], bottom[index]))
        triangles.append((f"{instance.tree_id}_trunk_cap", center_top, top[index], top[next_index]))
    return triangles


def crown_triangles(instance: TreeInstance) -> list[Triangle]:
    if instance.crown_shape == "conical":
        return _conical_crown_triangles(instance)
    center_z = instance.z + (instance.trunk_height_m + instance.height_m) / 2.0
    radius_z = max(0.8, (instance.height_m - instance.trunk_height_m) / 2.0)
    crown_radius = instance.crown_radius_m
    if instance.crown_shape == "umbrella":
        crown_radius *= 1.08
    elif instance.crown_shape == "columnar":
        crown_radius *= 0.72
    elif instance.crown_shape == "tuft":
        crown_radius *= 0.78
    rings: list[list[Point3]] = []
    for ring_index in range(CROWN_RINGS + 1):
        phi = -math.pi / 2.0 + math.pi * ring_index / CROWN_RINGS
        rings.append(
            _circle_points(
                instance.x,
                instance.y,
                center_z + radius_z * math.sin(phi),
                crown_radius * math.cos(phi),
                CROWN_SEGMENTS,
            )
        )

    triangles: list[Triangle] = []
    for ring_index in range(CROWN_RINGS):
        lower = rings[ring_index]
        upper = rings[ring_index + 1]
        for segment in range(CROWN_SEGMENTS):
            next_segment = (segment + 1) % CROWN_SEGMENTS
            triangles.extend(
                _quad_triangles(
                    f"{instance.tree_id}_crown",
                    lower[segment],
                    lower[next_segment],
                    upper[next_segment],
                    upper[segment],
                )
            )
    return triangles


def _conical_crown_triangles(instance: TreeInstance) -> list[Triangle]:
    base_z = instance.z + instance.trunk_height_m * 0.82
    apex = (instance.x, instance.y, instance.z + instance.height_m)
    base_points = _circle_points(instance.x, instance.y, base_z, instance.crown_radius_m, CROWN_SEGMENTS)
    base_center = (instance.x, instance.y, base_z)
    triangles: list[Triangle] = []
    for segment in range(CROWN_SEGMENTS):
        next_segment = (segment + 1) % CROWN_SEGMENTS
        triangles.append((f"{instance.tree_id}_crown", base_points[segment], base_points[next_segment], apex))
        triangles.append((f"{instance.tree_id}_crown", base_center, base_points[next_segment], base_points[segment]))
    return triangles


def _circle_points(
    x: float,
    y: float,
    z: float,
    radius: float,
    segments: int,
) -> list[Point3]:
    return [
        (
            x + radius * math.cos(2.0 * math.pi * index / segments),
            y + radius * math.sin(2.0 * math.pi * index / segments),
            z,
        )
        for index in range(segments)
    ]


def _quad_triangles(
    label: str,
    a: Point3,
    b: Point3,
    c: Point3,
    d: Point3,
) -> list[Triangle]:
    return [(label, a, b, c), (label, a, c, d)]


def lonlat_to_epsg25832(lon: float, lat: float) -> tuple[float, float]:
    semi_major = 6378137.0
    flattening = 1 / 298.257223563
    eccentricity_sq = flattening * (2 - flattening)
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    lon0 = math.radians(9.0)
    k0 = 0.9996
    false_easting = 500000.0
    n = semi_major / math.sqrt(1 - eccentricity_sq * math.sin(lat_rad) ** 2)
    t = math.tan(lat_rad) ** 2
    c = (eccentricity_sq / (1 - eccentricity_sq)) * math.cos(lat_rad) ** 2
    a = (lon_rad - lon0) * math.cos(lat_rad)
    m = semi_major * (
        (1 - eccentricity_sq / 4 - 3 * eccentricity_sq**2 / 64 - 5 * eccentricity_sq**3 / 256) * lat_rad
        - (3 * eccentricity_sq / 8 + 3 * eccentricity_sq**2 / 32 + 45 * eccentricity_sq**3 / 1024)
        * math.sin(2 * lat_rad)
        + (15 * eccentricity_sq**2 / 256 + 45 * eccentricity_sq**3 / 1024) * math.sin(4 * lat_rad)
        - (35 * eccentricity_sq**3 / 3072) * math.sin(6 * lat_rad)
    )
    easting = false_easting + k0 * n * (
        a + (1 - t + c) * a**3 / 6 + (5 - 18 * t + t**2 + 72 * c - 58 * eccentricity_sq) * a**5 / 120
    )
    northing = k0 * (
        m
        + n
        * math.tan(lat_rad)
        * (
            a**2 / 2
            + (5 - t + 9 * c + 4 * c**2) * a**4 / 24
            + (61 - 58 * t + t**2 + 600 * c - 330 * eccentricity_sq) * a**6 / 720
        )
    )
    return easting, northing


def translate_triangles(triangles: list[Triangle], dx: float, dy: float, dz: float) -> list[Triangle]:
    return [
        (
            label,
            (a[0] + dx, a[1] + dy, a[2] + dz),
            (b[0] + dx, b[1] + dy, b[2] + dz),
            (c[0] + dx, c[1] + dy, c[2] + dz),
        )
        for label, a, b, c in triangles
    ]
