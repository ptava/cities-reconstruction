"""Domain geometry transformations and validation for City4CFD reconstruction."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from pathlib import Path
from statistics import median
from typing import Any

from shapely.geometry import MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import triangulate
from shapely.validation import make_valid

from cities_reconstruction.adapters.city4cfd import City4CFDExecutionResult
from cities_reconstruction.config import ConfigError

DEFAULT_BUILDING_HEIGHT_M = 9.0
DEFAULT_ROOF_RAISE_M = 1.5

Point3 = tuple[float, float, float]
Triangle = tuple[str, Point3, Point3, Point3]


def project_surface_layer_feature(
    feature: dict[str, Any],
    *,
    target_crs: str,
    source_path: Path,
) -> dict[str, Any]:
    """Project one stage-1 EPSG:4326 polygon into EPSG:25832."""

    projected = dict(feature)
    geometry = dict(feature["geometry"])

    def project_ring(ring: list[list[float]]) -> list[list[float]]:
        projected_ring: list[list[float]] = []
        for coordinate in ring:
            lon = float(coordinate[0])
            lat = float(coordinate[1])
            if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                raise ConfigError(
                    f"stage-1 surface coordinates must be EPSG:4326 lon/lat before projection: {source_path}"
                )
            x, y = lonlat_to_epsg25832(lon, lat)
            projected_ring.append([x, y])
        return projected_ring

    if geometry["type"] == "Polygon":
        geometry["coordinates"] = [project_ring(ring) for ring in geometry["coordinates"]]
    else:
        geometry["coordinates"] = [[project_ring(ring) for ring in polygon] for polygon in geometry["coordinates"]]
    properties = dict(feature.get("properties", {}))
    properties["source_crs"] = "EPSG:4326"
    properties["projected_crs"] = target_crs
    projected["geometry"] = geometry
    projected["properties"] = properties
    return projected


def clip_surface_layer_features(
    features: list[dict[str, Any]],
    *,
    center_xy: tuple[float, float],
    radius_m: float,
) -> list[dict[str, Any]]:
    """Clip projected surface polygons to the configured circular outer region."""

    region = Point(*center_xy).buffer(radius_m, quad_segs=48)
    clipped_features: list[dict[str, Any]] = []
    for feature in features:
        clipped_geometry = make_valid(shape(feature["geometry"])).intersection(region)
        polygons: list[Polygon] = []
        if isinstance(clipped_geometry, Polygon):
            polygons = [clipped_geometry]
        elif isinstance(clipped_geometry, MultiPolygon):
            polygons = list(clipped_geometry.geoms)
        else:
            polygons = [
                geometry for geometry in getattr(clipped_geometry, "geoms", ()) if isinstance(geometry, Polygon)
            ]
        polygons = [polygon for polygon in polygons if not polygon.is_empty and polygon.area > 0.0]
        if not polygons:
            continue
        clipped = dict(feature)
        clipped["geometry"] = mapping(polygons[0] if len(polygons) == 1 else MultiPolygon(polygons))
        properties = dict(feature.get("properties", {}))
        properties["clipped_to_outer_region"] = True
        clipped["properties"] = properties
        clipped_features.append(clipped)
    return clipped_features


def building_preview_triangles(
    features: list[dict[str, Any]],
    ground_elevation_index: dict[tuple[int, int], float],
    building_roof_index: dict[tuple[int, int], float],
) -> list[Triangle]:
    """Build deterministic fallback building triangles from handoff geometry."""

    triangles: list[Triangle] = []
    for index, feature in enumerate(features, start=1):
        for polygon in _feature_polygons(feature):
            if polygon.is_empty:
                continue
            base_z, top_z = _feature_preview_elevation(
                polygon,
                feature,
                ground_elevation_index,
                building_roof_index,
            )
            roof_raise = DEFAULT_ROOF_RAISE_M if _has_lod22_roof_shape(feature) else 0.0
            centroid = (float(polygon.centroid.x), float(polygon.centroid.y))
            roof_peak = (centroid[0], centroid[1], top_z + roof_raise)
            for ring in _polygon_rings(polygon):
                base_points = [(x, y, base_z) for x, y in ring[:-1]]
                top_points = [(x, y, top_z) for x, y in ring[:-1]]
                for start, end in zip(
                    range(len(base_points)),
                    range(1, len(base_points) + 1),
                    strict=True,
                ):
                    next_index = end % len(base_points)
                    triangles.extend(
                        _quad_triangles(
                            f"building_{index}_wall",
                            base_points[start],
                            base_points[next_index],
                            top_points[next_index],
                            top_points[start],
                        )
                    )
            for triangle in _polygon_top_triangles(polygon, top_z):
                triangles.append((f"building_{index}_roof", *triangle))
            if roof_raise > 0.0:
                for ring in _polygon_rings(polygon):
                    top_points = [(x, y, top_z) for x, y in ring[:-1]]
                    for start, end in zip(
                        range(len(top_points)),
                        range(1, len(top_points) + 1),
                        strict=True,
                    ):
                        triangles.append(
                            (
                                f"building_{index}_lod22_roof",
                                top_points[start],
                                top_points[end % len(top_points)],
                                roof_peak,
                            )
                        )
    return triangles


def terrain_preview_triangles(
    *,
    region_bbox: tuple[float, float, float, float],
    features: list[dict[str, Any]],
    ground_elevation_index: dict[tuple[int, int], float],
) -> list[Triangle]:
    """Build deterministic fallback terrain triangles around the configured region."""

    min_x, min_y, max_x, max_y = _terrain_surface_bbox(region_bbox, features)
    terrain_z = _terrain_preview_elevation(ground_elevation_index)
    return [
        ("terrain", (min_x, min_y, terrain_z), (max_x, min_y, terrain_z), (max_x, max_y, terrain_z)),
        ("terrain", (min_x, min_y, terrain_z), (max_x, max_y, terrain_z), (min_x, max_y, terrain_z)),
    ]


def validate_successful_city4cfd_geometry(
    execution: City4CFDExecutionResult,
    required_paths: tuple[Path | None, ...],
) -> None:
    """Require non-empty core geometry whenever City4CFD reports success."""

    if not execution.succeeded:
        return
    missing = [path for path in required_paths if path is None or not path.is_file() or path.stat().st_size == 0]
    if missing:
        rendered_paths = ", ".join("<unresolved>" if path is None else str(path) for path in missing)
        raise ConfigError(
            f"City4CFD {execution.status} reported success but required generated geometry "
            f"is missing or empty: {rendered_paths}"
        )


def lonlat_to_epsg25832(lon: float, lat: float) -> tuple[float, float]:
    """Convert WGS84 longitude/latitude to the stage-local EPSG:25832 plane."""

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


def _feature_polygons(feature: dict[str, Any]) -> list[Polygon]:
    geometry = make_valid(shape(feature["geometry"]))
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    return []


def _polygon_rings(polygon: Polygon) -> list[list[tuple[float, float]]]:
    rings = [_ring_xy(list(polygon.exterior.coords))]
    rings.extend(_ring_xy(list(interior.coords)) for interior in polygon.interiors)
    return rings


def _polygon_top_triangles(polygon: Polygon, z: float) -> list[tuple[Point3, Point3, Point3]]:
    triangles: list[tuple[Point3, Point3, Point3]] = []
    for triangle in triangulate(polygon):
        clipped = triangle.intersection(polygon)
        for piece in _geometry_polygons(clipped):
            if piece.area <= 0.001 or not polygon.covers(piece.representative_point()):
                continue
            coords = list(piece.exterior.coords)
            if len(coords) < 4:
                continue
            anchor = (float(coords[0][0]), float(coords[0][1]), z)
            for index in range(1, len(coords) - 2):
                triangles.append(
                    (
                        anchor,
                        (float(coords[index][0]), float(coords[index][1]), z),
                        (float(coords[index + 1][0]), float(coords[index + 1][1]), z),
                    )
                )
    return triangles


def _geometry_polygons(geometry: Any) -> list[Polygon]:
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    return []


def _terrain_surface_bbox(
    region_bbox: tuple[float, float, float, float],
    features: list[dict[str, Any]],
) -> tuple[float, float, float, float]:
    region_min_x, region_min_y, region_max_x, region_max_y = region_bbox
    if not features:
        return region_bbox
    footprint_min_x, footprint_min_y, footprint_max_x, footprint_max_y = _features_bbox(features)
    return (
        min(region_min_x, footprint_min_x),
        min(region_min_y, footprint_min_y),
        max(region_max_x, footprint_max_x),
        max(region_max_y, footprint_max_y),
    )


def _feature_preview_elevation(
    polygon: Polygon,
    feature: dict[str, Any],
    ground_elevation_index: dict[tuple[int, int], float],
    building_roof_index: dict[tuple[int, int], float],
) -> tuple[float, float]:
    min_x, min_y, max_x, max_y = polygon.bounds
    cell_size = 2.0
    min_cell_x = math.floor(min_x / cell_size) - 1
    max_cell_x = math.floor(max_x / cell_size) + 1
    min_cell_y = math.floor(min_y / cell_size) - 1
    max_cell_y = math.floor(max_y / cell_size) + 1
    ground_samples: list[float] = []
    roof_samples: list[float] = []
    for cell_x in range(min_cell_x, max_cell_x + 1):
        for cell_y in range(min_cell_y, max_cell_y + 1):
            center_x = (cell_x + 0.5) * cell_size
            center_y = (cell_y + 0.5) * cell_size
            if not polygon.covers(Point(center_x, center_y)):
                continue
            ground_z = ground_elevation_index.get((cell_x, cell_y))
            if ground_z is not None:
                ground_samples.append(ground_z)
            roof_z = building_roof_index.get((cell_x, cell_y))
            if roof_z is not None:
                roof_samples.append(roof_z)
    base_z = median(ground_samples) if ground_samples else 0.0
    if roof_samples:
        top_z = max(roof_samples)
        if top_z <= base_z:
            top_z = base_z + _feature_height(feature)
    else:
        top_z = base_z + _feature_height(feature)
    return base_z, top_z


def _terrain_preview_elevation(ground_elevation_index: dict[tuple[int, int], float]) -> float:
    if not ground_elevation_index:
        return 0.0
    return float(median(ground_elevation_index.values()))


def _ring_xy(ring: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
    return [(float(point[0]), float(point[1])) for point in ring]


def _feature_height(feature: dict[str, Any]) -> float:
    properties = feature.get("properties", {})
    for key in ("height_m", "estimated_height_m"):
        value = properties.get(key)
        if isinstance(value, int | float) and value > 0:
            return float(value)
    tags = properties.get("tags", {})
    if isinstance(tags, dict):
        height = _float_tag(tags.get("height") or tags.get("building:height"))
        if height is not None:
            return height
        levels = _float_tag(tags.get("building:levels"))
        if levels is not None:
            return levels * 3.0
    return DEFAULT_BUILDING_HEIGHT_M


def _has_lod22_roof_shape(feature: dict[str, Any]) -> bool:
    properties = feature.get("properties", {})
    roof_shape = properties.get("roof_shape")
    if isinstance(roof_shape, str) and roof_shape:
        return True
    tags = properties.get("tags", {})
    return isinstance(tags, dict) and isinstance(tags.get("roof:shape"), str)


def _float_tag(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = value.lower().replace("m", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _quad_triangles(label: str, a: Point3, b: Point3, c: Point3, d: Point3) -> list[Triangle]:
    return [(label, a, b, c), (label, a, c, d)]


def _outer_rings(geometry: dict[str, Any]) -> list[list[tuple[float, float]]]:
    if geometry["type"] == "Polygon":
        return [_ring_xy(geometry["coordinates"][0])]
    return [_ring_xy(polygon[0]) for polygon in geometry["coordinates"] if polygon]


def _features_bbox(features: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    points = [point for feature in features for ring in _outer_rings(feature["geometry"]) for point in ring]
    if not points:
        return 0.0, 0.0, 1.0, 1.0
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_y = max(point[1] for point in points)
    if min_x == max_x:
        max_x += 1.0
    if min_y == max_y:
        max_y += 1.0
    padding = max(max_x - min_x, max_y - min_y) * 0.05
    return min_x - padding, min_y - padding, max_x + padding, max_y + padding
