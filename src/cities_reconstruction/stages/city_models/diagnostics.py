"""Footprint diagnostics for the City4CFD stage."""

from __future__ import annotations

from typing import Any

from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.validation import make_valid


def build_footprint_diagnostics(features: list[dict[str, Any]]) -> dict[str, Any]:
    """Build overlap and inner-ring diagnostics for City4CFD footprints."""

    polygon_items: list[tuple[int, dict[str, Any], Polygon]] = []
    hole_count = 0
    invalid_count = 0
    for index, feature in enumerate(features):
        polygons = _feature_polygons(feature)
        if not polygons:
            invalid_count += 1
            continue
        for polygon in polygons:
            hole_count += len(polygon.interiors)
            polygon_items.append((index, feature, polygon))

    overlaps: list[dict[str, Any]] = []
    for first_index, first_feature, first_polygon in polygon_items:
        first_bounds = first_polygon.bounds
        for second_index, second_feature, second_polygon in polygon_items:
            if second_index <= first_index:
                continue
            if not _bounds_intersect(first_bounds, second_polygon.bounds):
                continue
            intersection = first_polygon.intersection(second_polygon)
            if intersection.is_empty:
                continue
            area = float(intersection.area)
            if area <= 0.05:
                continue
            smaller_area = max(min(first_polygon.area, second_polygon.area), 1e-9)
            overlaps.append(
                {
                    "first_feature_index": first_index,
                    "second_feature_index": second_index,
                    "first_source_tag": first_feature.get("properties", {}).get("source_tag"),
                    "second_source_tag": second_feature.get("properties", {}).get("source_tag"),
                    "intersection_area_m2": round(area, 3),
                    "overlap_ratio_of_smaller": round(area / smaller_area, 4),
                }
            )
    overlaps.sort(key=lambda item: item["intersection_area_m2"], reverse=True)
    status = "warning" if overlaps else "passed"
    return {
        "overlap_status": status,
        "feature_count": len(features),
        "polygon_count": len(polygon_items),
        "invalid_or_empty_geometry_count": invalid_count,
        "inner_ring_count": hole_count,
        "overlap_pair_count": len(overlaps),
        "largest_overlap_area_m2": overlaps[0]["intersection_area_m2"] if overlaps else 0.0,
        "overlaps": overlaps[:100],
        "assumptions": [
            "City4CFD receives the projected GeoJSON footprint file with full polygon coordinates, including inner rings.",
            "Overlaps are reported as QA diagnostics because overlapping footprints can create duplicated or superposed reconstructed building surfaces.",
            "The offline STL preview preserves polygon holes, but it is a QA fallback rather than City4CFD output.",
        ],
    }


def _bounds_intersect(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (first[2] < second[0] or first[0] > second[2] or first[3] < second[1] or first[1] > second[3])


def _feature_polygons(feature: dict[str, Any]) -> list[Polygon]:
    geometry = make_valid(shape(feature["geometry"]))
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    return []
