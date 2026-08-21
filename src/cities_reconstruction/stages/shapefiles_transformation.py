"""Transform raw Overpass payloads into classified GeoJSON features."""

from __future__ import annotations

import math
from typing import Any

from shapely.geometry import MultiPolygon, Polygon
from shapely.validation import make_valid

from cities_reconstruction.config import AppConfig

EARTH_RADIUS_M = 6_371_000.0
ROI_FILL_SEGMENTS = 256

FEATURE_LIKE_INVENTORY_KEYS = frozenset(
    {
        "aeroway",
        "amenity",
        "barrier",
        "building",
        "craft",
        "emergency",
        "geological",
        "healthcare",
        "highway",
        "historic",
        "landuse",
        "leisure",
        "man_made",
        "natural",
        "office",
        "place",
        "power",
        "public_transport",
        "railway",
        "shop",
        "sport",
        "surface",
        "tourism",
        "water",
        "waterway",
    }
)


def build_tag_inventory(raw_data: dict[str, Any], source: str, config: AppConfig) -> dict[str, Any]:
    """Summarize classified and unclassified tags in an Overpass payload."""
    elements = raw_data.get("elements")
    if not isinstance(elements, list):
        raise ValueError("Overpass JSON must contain an 'elements' list")

    tag_key_counts: dict[str, int] = {}
    tag_value_counts: dict[str, int] = {}
    element_type_counts: dict[str, int] = {}
    classified_source_tag_counts: dict[str, int] = {}
    unclassified_tag_value_counts: dict[str, int] = {}
    unclassified_feature_like_tag_value_counts: dict[str, int] = {}
    tagged_element_count = 0

    for element in elements:
        if not isinstance(element, dict):
            continue
        element_type = str(element.get("type", "unknown"))
        element_type_counts[element_type] = element_type_counts.get(element_type, 0) + 1
        tags = element.get("tags")
        if not isinstance(tags, dict) or not tags:
            continue
        tagged_element_count += 1
        classification = _classify_tags(tags, config)
        if classification is None:
            for tag_value in _tag_values(tags):
                _increment(unclassified_tag_value_counts, tag_value)
                if _is_feature_like_tag_value(tag_value):
                    _increment(unclassified_feature_like_tag_value_counts, tag_value)
        else:
            _increment(classified_source_tag_counts, classification[2])
        for key, value in tags.items():
            _increment(tag_key_counts, str(key))
            _increment(tag_value_counts, f"{key}={value}")

    return {
        "source": source,
        "raw_elements": len(elements),
        "tagged_elements": tagged_element_count,
        "element_type_counts": dict(sorted(element_type_counts.items())),
        "tag_key_counts": dict(sorted(tag_key_counts.items())),
        "tag_value_counts": dict(sorted(tag_value_counts.items())),
        "classified_source_tag_counts": dict(sorted(classified_source_tag_counts.items())),
        "unclassified_tag_value_counts": dict(sorted(unclassified_tag_value_counts.items())),
        "unclassified_feature_like_tag_value_counts": dict(sorted(unclassified_feature_like_tag_value_counts.items())),
    }


def overpass_to_features(
    raw_data: dict[str, Any],
    config: AppConfig,
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    """Classify and transform one raw Overpass payload into GeoJSON features."""
    elements = raw_data.get("elements")
    if not isinstance(elements, list):
        raise ValueError("Overpass JSON must contain an 'elements' list")

    nodes = _node_lookup(elements)
    features: list[dict[str, Any]] = []
    skipped_by_reason: dict[str, int] = {}
    for element in elements:
        feature, skipped_reason = _element_to_feature(element, nodes, config)
        if feature is None:
            _increment(skipped_by_reason, skipped_reason or "unknown")
            continue
        features.append(feature)

    features.sort(
        key=lambda item: (
            item["properties"]["category"],
            item["properties"]["group_tag"],
            item["properties"]["osm_type"],
            item["properties"]["osm_id"],
        )
    )
    return features, sum(skipped_by_reason.values()), skipped_by_reason


def _node_lookup(elements: list[Any]) -> dict[int, tuple[float, float]]:
    nodes: dict[int, tuple[float, float]] = {}
    for element in elements:
        if not isinstance(element, dict) or element.get("type") != "node":
            continue
        if isinstance(element.get("id"), int) and _has_lat_lon(element):
            nodes[element["id"]] = (float(element["lon"]), float(element["lat"]))
    return nodes


def _element_to_feature(
    element: Any,
    nodes: dict[int, tuple[float, float]],
    config: AppConfig,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(element, dict):
        return None, "invalid_element"
    tags = element.get("tags")
    if not isinstance(tags, dict):
        return None, "missing_tags"
    classification = _classify_tags(tags, config)
    if classification is None:
        return None, "unsupported_tags"
    category, group_tag, source_tag = classification

    geometry = _geometry_from_element(element, nodes, category)
    if geometry is None:
        return None, "unsupported_or_incomplete_geometry"

    centroid = _centroid(geometry)
    centroid_distance_m = _distance_m(
        config.region.center_lat,
        config.region.center_lon,
        centroid[1],
        centroid[0],
    )
    roi_distance_m = _geometry_distance_to_region_center_m(geometry, config)
    roi_zone = _roi_zone(roi_distance_m, config)
    if roi_zone is None:
        return None, "outside_roi_policy"
    reconstruction_scope = _reconstruction_scope(roi_zone)
    building_properties = (
        {
            "building_base_height_m": _building_base_height_m(
                tags,
                config.city_models.building_roof_default_base_height_m,
            )
        }
        if category == "buildings"
        else {}
    )

    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "osm_type": element.get("type"),
            "osm_id": element.get("id"),
            "category": category,
            "group_tag": group_tag,
            "source_tag": source_tag,
            "contributes_to_geometry": _contributes_to_geometry(geometry),
            "geometry_role": _geometry_role(geometry),
            "roi_zone": roi_zone,
            "reconstruction_scope": reconstruction_scope,
            "include_in_building_lod22_reconstruction": _include_in_building_lod22_reconstruction(
                category,
                roi_zone,
            ),
            "centroid_distance_m": round(centroid_distance_m, 3),
            "roi_distance_m": round(roi_distance_m, 3),
            "tags": tags,
            **building_properties,
        },
    }, None


def _building_base_height_m(tags: dict[str, Any], roof_default_m: float) -> float:
    if tags.get("building") != "roof":
        return 0.0

    raw_height = tags.get("min_height")
    if raw_height is None:
        return roof_default_m
    try:
        height = float(raw_height)
    except (TypeError, ValueError):
        return roof_default_m
    return height if math.isfinite(height) and height >= 0 else roof_default_m


def _classify_tags(tags: dict[str, Any], config: AppConfig) -> tuple[str, str, str] | None:
    for rule in config.shapefiles.classification_rules:
        for expression in rule.match_any:
            key, separator, expected_value = expression.partition("=")
            if key not in tags:
                continue
            if separator and str(tags[key]) != expected_value:
                continue
            return rule.category, rule.group_tag, _source_tag(tags, key)
    return None


def _source_tag(tags: dict[str, Any], key: str) -> str:
    return f"{key}={tags[key]}"


def _tag_values(tags: dict[str, Any]) -> list[str]:
    return [f"{key}={value}" for key, value in tags.items()]


def _is_feature_like_tag_value(tag_value: str) -> bool:
    key = tag_value.split("=", 1)[0]
    return key in FEATURE_LIKE_INVENTORY_KEYS


def _geometry_from_element(
    element: dict[str, Any],
    nodes: dict[int, tuple[float, float]],
    category: str,
) -> dict[str, Any] | None:
    element_type = element.get("type")
    if element_type == "node" and _has_lat_lon(element):
        return {"type": "Point", "coordinates": [float(element["lon"]), float(element["lat"])]}
    if element_type == "way":
        coordinates = _way_coordinates(element, nodes)
        if len(coordinates) < 2:
            return None
        is_closed = coordinates[0] == coordinates[-1] and len(coordinates) >= 4
        if is_closed and category != "roads":
            return {"type": "Polygon", "coordinates": [coordinates]}
        return {"type": "LineString", "coordinates": coordinates}
    if element_type == "relation":
        return _relation_geometry(element, category)
    return None


def _relation_geometry(element: dict[str, Any], category: str) -> dict[str, Any] | None:
    members = element.get("members")
    if not isinstance(members, list):
        return None
    outer_segments: list[list[list[float]]] = []
    inner_segments: list[list[list[float]]] = []
    for member in members:
        if not isinstance(member, dict):
            continue
        segment = _member_geometry(member)
        if len(segment) < 2:
            continue
        if member.get("role") == "outer":
            outer_segments.append(segment)
        elif member.get("role") == "inner":
            inner_segments.append(segment)
    if not outer_segments:
        return None

    if category == "roads" and len(outer_segments) == 1 and not inner_segments:
        return {"type": "LineString", "coordinates": outer_segments[0]}

    outer_rings = _assemble_rings(outer_segments)
    if not outer_rings:
        return None
    polygons = _assign_inner_rings_to_outer_rings(outer_rings, _assemble_rings(inner_segments))
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def _member_geometry(member: dict[str, Any]) -> list[list[float]]:
    geometry = member.get("geometry")
    if not isinstance(geometry, list):
        return []
    coordinates = []
    for point in geometry:
        if isinstance(point, dict) and _has_lat_lon(point):
            coordinates.append([float(point["lon"]), float(point["lat"])])
    return coordinates


def _assemble_rings(segments: list[list[list[float]]]) -> list[list[list[float]]]:
    unused = [list(segment) for segment in segments]
    rings: list[list[list[float]]] = []
    while unused:
        ring = unused.pop(0)
        changed = True
        while changed and ring[0] != ring[-1]:
            changed = False
            for index, segment in enumerate(unused):
                if ring[-1] == segment[0]:
                    ring.extend(segment[1:])
                elif ring[-1] == segment[-1]:
                    ring.extend(reversed(segment[:-1]))
                elif ring[0] == segment[-1]:
                    ring = segment[:-1] + ring
                elif ring[0] == segment[0]:
                    ring = list(reversed(segment[1:])) + ring
                else:
                    continue
                unused.pop(index)
                changed = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def _assign_inner_rings_to_outer_rings(
    outer_rings: list[list[list[float]]],
    inner_rings: list[list[list[float]]],
) -> list[list[list[list[float]]]]:
    polygons = [[outer_ring] for outer_ring in outer_rings]
    for inner_ring in inner_rings:
        container_index = _outer_ring_index_for_inner_ring(outer_rings, inner_ring)
        if container_index is not None:
            polygons[container_index].append(inner_ring)
    return polygons


def _outer_ring_index_for_inner_ring(
    outer_rings: list[list[list[float]]],
    inner_ring: list[list[float]],
) -> int | None:
    if not inner_ring:
        return None
    sample = (inner_ring[0][0], inner_ring[0][1])
    for index, outer_ring in enumerate(outer_rings):
        outer_points = [(point[0], point[1]) for point in outer_ring]
        if _point_in_ring(sample, outer_points):
            return index
    return None


def _way_coordinates(
    element: dict[str, Any],
    nodes: dict[int, tuple[float, float]],
) -> list[list[float]]:
    geometry = element.get("geometry")
    if isinstance(geometry, list):
        coordinates = []
        for point in geometry:
            if isinstance(point, dict) and _has_lat_lon(point):
                coordinates.append([float(point["lon"]), float(point["lat"])])
        return coordinates

    node_ids = element.get("nodes")
    if isinstance(node_ids, list):
        coordinates = []
        for node_id in node_ids:
            if isinstance(node_id, int) and node_id in nodes:
                lon, lat = nodes[node_id]
                coordinates.append([lon, lat])
        return coordinates
    return []


def _has_lat_lon(value: dict[str, Any]) -> bool:
    return isinstance(value.get("lat"), int | float) and isinstance(value.get("lon"), int | float)


def _contributes_to_geometry(geometry: dict[str, Any]) -> bool:
    return geometry["type"] in {"Polygon", "MultiPolygon"}


def _geometry_role(geometry: dict[str, Any]) -> str:
    if _contributes_to_geometry(geometry):
        return "contributing_polygon"
    return "reference_only_non_contributing"


def _roi_zone(roi_distance_m: float, config: AppConfig) -> str | None:
    outer_radius = config.region.outer_diameter_m / 2.0
    if config.region.inner_diameter_m is None:
        return "full" if roi_distance_m <= outer_radius else None
    inner_radius = config.region.inner_diameter_m / 2.0
    if roi_distance_m <= inner_radius:
        return "inner"
    if roi_distance_m <= outer_radius:
        return "annular"
    return None


def _reconstruction_scope(roi_zone: str) -> str:
    if roi_zone in {"inner", "full"}:
        return "primary_roi"
    return "annular_context"


def _include_in_building_lod22_reconstruction(category: str, roi_zone: str) -> bool:
    return category == "buildings" and roi_zone in {"inner", "full"}


def _centroid(geometry: dict[str, Any]) -> tuple[float, float]:
    coordinates = geometry["coordinates"]
    if geometry["type"] == "Point":
        return float(coordinates[0]), float(coordinates[1])
    if geometry["type"] == "LineString":
        return _average_coordinate(coordinates)
    if geometry["type"] == "Polygon":
        return _average_coordinate(coordinates[0])
    if geometry["type"] == "MultiPolygon":
        points = [point for polygon in coordinates for ring in polygon for point in ring]
        return _average_coordinate(points)
    raise ValueError(f"unsupported geometry type: {geometry['type']}")


def _average_coordinate(coordinates: list[list[float]]) -> tuple[float, float]:
    lon_sum = sum(point[0] for point in coordinates)
    lat_sum = sum(point[1] for point in coordinates)
    count = len(coordinates)
    return lon_sum / count, lat_sum / count


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


def _geometry_distance_to_region_center_m(geometry: dict[str, Any], config: AppConfig) -> float:
    if geometry["type"] == "Point":
        return _point_norm_m(_project_coordinate_m(geometry["coordinates"], config))
    if geometry["type"] == "LineString":
        return _line_distance_to_center_m(geometry["coordinates"], config)
    if geometry["type"] == "Polygon":
        return _polygon_distance_to_center_m(geometry["coordinates"], config)
    if geometry["type"] == "MultiPolygon":
        return min(_polygon_distance_to_center_m(polygon, config) for polygon in geometry["coordinates"])
    raise ValueError(f"unsupported geometry type: {geometry['type']}")


def _polygon_distance_to_center_m(
    polygon: list[list[list[float]]],
    config: AppConfig,
) -> float:
    outer_ring = polygon[0] if polygon else []
    projected_outer = [_project_coordinate_m(point, config) for point in outer_ring]
    if projected_outer and _point_in_ring((0.0, 0.0), projected_outer):
        return 0.0
    distances = [_line_distance_to_center_m(ring, config) for ring in polygon if ring]
    return min(distances) if distances else math.inf


def _line_distance_to_center_m(coordinates: list[list[float]], config: AppConfig) -> float:
    projected = [_project_coordinate_m(point, config) for point in coordinates]
    if not projected:
        return math.inf
    point_distances = [_point_norm_m(point) for point in projected]
    if len(projected) == 1:
        return point_distances[0]
    segment_distances = [
        _segment_distance_to_origin_m(start, end)
        for start, end in zip(projected, projected[1:], strict=False)
    ]
    return min([*point_distances, *segment_distances])


def _project_coordinate_m(coordinate: list[float], config: AppConfig) -> tuple[float, float]:
    lon, lat = coordinate
    x_m = (
        math.radians(lon - config.region.center_lon)
        * EARTH_RADIUS_M
        * math.cos(math.radians(config.region.center_lat))
    )
    y_m = math.radians(lat - config.region.center_lat) * EARTH_RADIUS_M
    return x_m, y_m


def _point_norm_m(point: tuple[float, float]) -> float:
    return math.hypot(point[0], point[1])


def _segment_distance_to_origin_m(start: tuple[float, float], end: tuple[float, float]) -> float:
    start_x, start_y = start
    end_x, end_y = end
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared == 0.0:
        return _point_norm_m(start)
    t = -((start_x * delta_x) + (start_y * delta_y)) / length_squared
    t = max(0.0, min(1.0, t))
    closest = (start_x + t * delta_x, start_y + t * delta_y)
    return _point_norm_m(closest)


def _point_in_ring(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    if len(ring) < 3:
        return inside
    previous_x, previous_y = ring[-1]
    for current_x, current_y in ring:
        intersects = (current_y > y) != (previous_y > y)
        if intersects:
            x_at_y = (previous_x - current_x) * (y - current_y) / (previous_y - current_y) + current_x
            if x < x_at_y:
                inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _circle_polygon_m(radius: float) -> Polygon:
    points = [
        (
            math.cos(2.0 * math.pi * index / ROI_FILL_SEGMENTS) * radius,
            math.sin(2.0 * math.pi * index / ROI_FILL_SEGMENTS) * radius,
        )
        for index in range(ROI_FILL_SEGMENTS)
    ]
    return Polygon(points)


def _coordinates_to_polygon_m(coordinates: list[Any], config: AppConfig) -> Polygon:
    shell = [_project_coordinate_m(point, config) for point in coordinates[0]]
    holes = [
        [_project_coordinate_m(point, config) for point in ring]
        for ring in coordinates[1:]
        if len(ring) >= 4
    ]
    return Polygon(shell, holes)


def _feature_to_shapely_polygons(
    feature: dict[str, Any],
    config: AppConfig,
) -> list[Polygon]:
    geometry = feature["geometry"]
    if geometry["type"] == "Polygon":
        polygon = _coordinates_to_polygon_m(geometry["coordinates"], config)
        return _extract_polygons(make_valid(polygon))
    if geometry["type"] == "MultiPolygon":
        polygons = []
        for coordinates in geometry["coordinates"]:
            polygon = _coordinates_to_polygon_m(coordinates, config)
            polygons.extend(_extract_polygons(make_valid(polygon)))
        return polygons
    return []


def _extract_polygons(geometry: Any) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if hasattr(geometry, "geoms"):
        return [
            polygon
            for item in geometry.geoms
            for polygon in _extract_polygons(item)
        ]
    return []


def _polygon_m_to_lonlat_coordinates(
    polygon: Polygon,
    config: AppConfig,
) -> list[list[list[float]]]:
    rings = [
        [_local_m_to_lonlat(x, y, config) for x, y in polygon.exterior.coords],
    ]
    rings.extend(
        [_local_m_to_lonlat(x, y, config) for x, y in interior.coords]
        for interior in polygon.interiors
    )
    return rings


def _local_m_to_lonlat(x_m: float, y_m: float, config: AppConfig) -> list[float]:
    lon = config.region.center_lon + math.degrees(
        x_m / (EARTH_RADIUS_M * math.cos(math.radians(config.region.center_lat)))
    )
    lat = config.region.center_lat + math.degrees(y_m / EARTH_RADIUS_M)
    return [lon, lat]
