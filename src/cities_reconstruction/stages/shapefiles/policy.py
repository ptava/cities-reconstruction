"""Cross-source deduplication and surface-precedence policy for Stage 1."""

from __future__ import annotations

from typing import Any

from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.validation import make_valid

from cities_reconstruction.config import AppConfig, ConfigError

from .transformation import (
    _distance_m,
    _extract_polygons,
    _feature_to_shapely_polygons,
    _polygon_m_to_lonlat_coordinates,
)


def remove_overpass_trees_overlapping_supplemental_trees(
    features: list[dict[str, Any]],
    supplemental_tree_features: list[dict[str, Any]],
    tolerance_m: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Remove Overpass trees superseded by nearby supplemental tree records."""
    diagnostics = {
        "enabled": bool(supplemental_tree_features) and tolerance_m > 0.0,
        "tolerance_m": tolerance_m,
        "supplemental_tree_count": len(supplemental_tree_features),
        "overpass_tree_count": sum(1 for feature in features if _is_overpass_tree_feature(feature)),
        "removed_overpass_tree_count": 0,
        "removed_overpass_tree_ids": [],
        "removed_overpass_tree_markers": [],
    }
    if not diagnostics["enabled"]:
        return features, diagnostics

    supplemental_points = [
        (
            float(coordinates[1]),
            float(coordinates[0]),
            feature.get("properties", {}).get("osm_id"),
        )
        for feature in supplemental_tree_features
        if (coordinates := _point_coordinates(feature)) is not None
    ]
    if not supplemental_points:
        diagnostics["enabled"] = False
        return features, diagnostics

    filtered_features: list[dict[str, Any]] = []
    removed_ids: list[Any] = []
    removed_markers: list[dict[str, Any]] = []
    for feature in features:
        if not _is_overpass_tree_feature(feature):
            filtered_features.append(feature)
            continue
        coordinates = _point_coordinates(feature)
        if coordinates is None:
            filtered_features.append(feature)
            continue
        lon = float(coordinates[0])
        lat = float(coordinates[1])
        duplicate_match = _nearest_supplemental_tree_within_tolerance(
            lat,
            lon,
            supplemental_points,
            tolerance_m,
        )
        if duplicate_match is not None:
            nearest_distance, nearest_supplemental_tree_id = duplicate_match
            osm_id = feature.get("properties", {}).get("osm_id")
            removed_ids.append(osm_id)
            if len(removed_markers) < 200:
                removed_markers.append(
                    {
                        "osm_id": osm_id,
                        "coordinates": [lon, lat],
                        "nearest_supplemental_tree_distance_m": round(nearest_distance, 3),
                        "nearest_supplemental_tree_id": nearest_supplemental_tree_id,
                    }
                )
            continue
        filtered_features.append(feature)

    diagnostics["removed_overpass_tree_count"] = len(removed_ids)
    diagnostics["removed_overpass_tree_ids"] = removed_ids[:200]
    diagnostics["removed_overpass_tree_markers"] = removed_markers
    return filtered_features, diagnostics


def resolve_surface_overlaps(
    features: list[dict[str, Any]],
    config: AppConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply configured surface precedence and return disjoint polygons."""
    candidates = [
        (index, feature)
        for index, feature in enumerate(features)
        if feature.get("properties", {}).get("contributes_to_geometry")
    ]
    candidates.sort(key=lambda item: (surface_precedence_rank(item[1], config), item[0]))

    occupied: Any = Polygon()
    resolved_by_index: dict[int, dict[str, Any]] = {}
    by_category: dict[str, dict[str, float | int]] = {}
    by_supplemental: dict[str, dict[str, float | int]] = {}
    clipped_count = 0
    removed_count = 0
    removed_area = 0.0
    for index, feature in candidates:
        category = str(feature.get("properties", {}).get("category"))
        stats = by_category.setdefault(category, _empty_overlap_stats())
        surface_id = feature.get("properties", {}).get("supplemental_input_id")
        surface_stats = None
        if isinstance(surface_id, str):
            surface_stats = by_supplemental.setdefault(surface_id, _empty_overlap_stats())
            surface_stats["input_features"] = int(surface_stats["input_features"]) + 1
        stats["input_features"] = int(stats["input_features"]) + 1
        clipped, feature_removed_area = _difference_surface_feature(feature, occupied, config)
        removed_area += feature_removed_area
        stats["removed_area_m2"] = float(stats["removed_area_m2"]) + feature_removed_area
        if surface_stats is not None:
            surface_stats["removed_area_m2"] = float(surface_stats["removed_area_m2"]) + feature_removed_area
        if clipped is None:
            removed_count += 1
            stats["removed_features"] = int(stats["removed_features"]) + 1
            if surface_stats is not None:
                surface_stats["removed_features"] = int(surface_stats["removed_features"]) + 1
            continue
        resolved_by_index[index] = clipped
        stats["accepted_features"] = int(stats["accepted_features"]) + 1
        if surface_stats is not None:
            surface_stats["accepted_features"] = int(surface_stats["accepted_features"]) + 1
        if feature_removed_area > 0.01:
            clipped_count += 1
            stats["clipped_features"] = int(stats["clipped_features"]) + 1
            if surface_stats is not None:
                surface_stats["clipped_features"] = int(surface_stats["clipped_features"]) + 1
        clipped_geometry = feature_union_m([clipped], config)
        occupied = clipped_geometry if occupied.is_empty else make_valid(occupied.union(clipped_geometry))

    resolved = [
        resolved_by_index[index] if index in resolved_by_index else feature
        for index, feature in enumerate(features)
        if not feature.get("properties", {}).get("contributes_to_geometry") or index in resolved_by_index
    ]
    for stats in by_category.values():
        stats["removed_area_m2"] = round(float(stats["removed_area_m2"]), 3)
    for stats in by_supplemental.values():
        stats["removed_area_m2"] = round(float(stats["removed_area_m2"]), 3)
    return resolved, {
        "precedence": list(config.shapefiles.surface_precedence),
        "input_polygon_features": len(candidates),
        "accepted_polygon_features": len(resolved_by_index),
        "clipped_polygon_features": clipped_count,
        "removed_polygon_features": removed_count,
        "removed_overlap_area_m2": round(removed_area, 3),
        "by_category": dict(sorted(by_category.items())),
        "by_supplemental": dict(sorted(by_supplemental.items())),
        "policy": "Contributing polygons are processed in configured precedence order. Each polygon is clipped against all previously accepted higher- or equal-priority surface coverage, producing mutually disjoint Stage 1 surfaces.",
    }


def surface_precedence_rank(feature: dict[str, Any], config: AppConfig) -> int:
    """Return the configured precedence rank for one contributing surface."""
    properties = feature.get("properties", {})
    category = str(properties.get("category", ""))
    group_tag = str(properties.get("group_tag", ""))
    surface_id = str(properties.get("supplemental_input_id", ""))
    selectors = (
        f"supplemental:{surface_id}" if surface_id else "",
        f"{category}:{group_tag}" if group_tag else "",
        category,
    )
    for selector in selectors:
        if selector in config.shapefiles.surface_precedence:
            return config.shapefiles.surface_precedence.index(selector)
    raise ConfigError(f"no shapefiles.surface_precedence entry matches {category}:{group_tag}")


def feature_union_m(features: list[dict[str, Any]], config: AppConfig) -> Any:
    """Return the union of feature polygons in the stage-local metric frame."""
    polygons = [
        polygon
        for feature in features
        for polygon in _feature_to_shapely_polygons(feature, config)
    ]
    return unary_union(polygons) if polygons else Polygon()


def _difference_surface_feature(
    feature: dict[str, Any],
    mask: Any,
    config: AppConfig,
) -> tuple[dict[str, Any] | None, float]:
    source_polygons = _feature_to_shapely_polygons(feature, config)
    if not source_polygons:
        return feature, 0.0
    source_geometry = unary_union(source_polygons)
    result = source_geometry if mask.is_empty else make_valid(source_geometry.difference(mask))
    polygons = [polygon for polygon in _extract_polygons(result) if polygon.area > 0.01]
    remaining_area = sum(polygon.area for polygon in polygons)
    removed_area = max(0.0, source_geometry.area - remaining_area)
    if not polygons:
        return None, removed_area
    updated = dict(feature)
    updated["geometry"] = _local_polygons_geojson_geometry(polygons, config)
    properties = dict(feature.get("properties", {}))
    properties["area_m2"] = round(remaining_area, 3)
    if removed_area > 0.01:
        properties["overlap_clipped"] = True
        properties["overlap_removed_area_m2"] = round(
            float(properties.get("overlap_removed_area_m2", 0.0)) + removed_area,
            3,
        )
    updated["properties"] = properties
    return updated, removed_area


def _local_polygons_geojson_geometry(
    polygons: list[Polygon],
    config: AppConfig,
) -> dict[str, Any]:
    coordinates = [_polygon_m_to_lonlat_coordinates(polygon, config) for polygon in polygons]
    if len(coordinates) == 1:
        return {"type": "Polygon", "coordinates": coordinates[0]}
    return {"type": "MultiPolygon", "coordinates": coordinates}


def _nearest_supplemental_tree_within_tolerance(
    overpass_lat: float,
    overpass_lon: float,
    supplemental_points: list[tuple[float, float, Any]],
    tolerance_m: float,
) -> tuple[float, Any] | None:
    nearest_match: tuple[float, Any] | None = None
    for supplemental_lat, supplemental_lon, supplemental_tree_id in supplemental_points:
        distance_m = _distance_m(overpass_lat, overpass_lon, supplemental_lat, supplemental_lon)
        if distance_m <= tolerance_m and (nearest_match is None or distance_m < nearest_match[0]):
            nearest_match = (distance_m, supplemental_tree_id)
    return nearest_match


def _is_overpass_tree_feature(feature: dict[str, Any]) -> bool:
    properties = feature.get("properties", {})
    return (
        properties.get("category") == "trees"
        and properties.get("source_tag") == "natural=tree"
        and properties.get("source_type") != "supplemental"
    )


def _point_coordinates(feature: dict[str, Any]) -> list[Any] | tuple[Any, ...] | None:
    geometry = feature.get("geometry", {})
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list | tuple) or len(coordinates) < 2:
        return None
    return coordinates


def _empty_overlap_stats() -> dict[str, float | int]:
    return {
        "input_features": 0,
        "accepted_features": 0,
        "clipped_features": 0,
        "removed_features": 0,
        "removed_area_m2": 0.0,
    }
