"""Supplemental ESRI record validation and GeoJSON transformation."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from shapely.geometry import Polygon, mapping
from shapely.validation import make_valid

from cities_reconstruction.config import AppConfig, ConfigError, SupplementalShapefileConfig
from cities_reconstruction.stages.shapefiles_inputs import (
    read_dbf_attributes,
    read_point_records,
    read_polygon_records,
)
from cities_reconstruction.stages.shapefiles_transformation import (
    _centroid,
    _circle_polygon_m,
    _coordinates_to_polygon_m,
    _distance_m,
    _extract_polygons,
    _geometry_distance_to_region_center_m,
    _geometry_role,
    _include_in_building_lod22_reconstruction,
    _polygon_m_to_lonlat_coordinates,
    _reconstruction_scope,
    _roi_zone,
)

MetricUnit = Literal["mm", "cm", "m"]
MetricConverter = Callable[[Any, str, MetricUnit], float | None]


@dataclass(frozen=True)
class TreeAttributeMapping:
    """Map one alias group to normalized tree tags."""

    tag: str
    aliases: tuple[str, ...]
    metric_tag: str | None = None
    numeric_converter: MetricConverter | None = None
    default_unit: MetricUnit = "m"


def load_supplemental_tree_features(
    config: AppConfig,
    tree_input: SupplementalShapefileConfig,
) -> list[dict[str, Any]]:
    path = tree_input.path
    if path.suffix.lower() != ".shp":
        raise ConfigError(
            f"shapefiles.supplemental[{tree_input.name}].path must point to an ESRI .shp file: {path}"
        )
    if not path.exists():
        raise ConfigError(f"supplemental input '{tree_input.name}' shapefile does not exist: {path}")

    records = read_point_records(path, tree_input.name)
    attributes = read_dbf_attributes(path.with_suffix(".dbf"))
    features: list[dict[str, Any]] = []
    record_index = 0
    for shape_index, (record_number, points, _is_null) in enumerate(records):
        record_attributes = attributes[shape_index] if shape_index < len(attributes) else {}
        if record_attributes is None:
            continue
        for point_index, (x, y) in enumerate(points, start=1):
            lon, lat = _shapefile_xy_to_lonlat(
                x,
                y,
                tree_input.crs,
                f"shapefiles.supplemental[{tree_input.name}].crs",
            )
            feature = _tree_shapefile_feature(
                lon=lon,
                lat=lat,
                config=config,
                tree_input=tree_input,
                path=path,
                attributes=record_attributes,
                record_number=record_number,
                point_index=point_index,
                sequence_index=record_index + 1,
            )
            if feature is not None:
                features.append(feature)
            record_index += 1
    return features


def load_supplemental_surface_features(
    config: AppConfig,
    surface: SupplementalShapefileConfig,
) -> list[dict[str, Any]]:
    path = surface.path
    if path.suffix.lower() != ".shp":
        raise ConfigError(f"shapefiles.supplemental[{surface.name}].path must point to an ESRI .shp file: {path}")
    if not path.exists():
        raise ConfigError(f"supplemental input '{surface.name}' shapefile does not exist: {path}")

    records = read_polygon_records(path, surface.name)
    attributes = read_dbf_attributes(path.with_suffix(".dbf"))
    features: list[dict[str, Any]] = []
    for shape_index, (record_number, polygons) in enumerate(records):
        record_attributes = attributes[shape_index] if shape_index < len(attributes) else {}
        if record_attributes is None:
            continue
        for polygon_index, polygon in enumerate(polygons, start=1):
            transformed = Polygon(
                [
                    _shapefile_xy_to_lonlat(x, y, surface.crs, f"shapefiles.supplemental[{surface.name}].crs")
                    for x, y in polygon.exterior.coords
                ],
                [
                    [
                        _shapefile_xy_to_lonlat(x, y, surface.crs, f"shapefiles.supplemental[{surface.name}].crs")
                        for x, y in ring.coords
                    ]
                    for ring in polygon.interiors
                ],
            )
            feature = _surface_shapefile_feature(
                polygon=transformed,
                config=config,
                path=path,
                source_crs=surface.crs,
                attributes=record_attributes,
                record_number=record_number,
                polygon_index=polygon_index,
                category=surface.category,
                group_tag=surface.group_tag or "",
                source_name=surface.name,
            )
            if feature is not None:
                features.append(feature)
    return features


def _surface_shapefile_feature(
    *,
    polygon: Polygon,
    config: AppConfig,
    path: Path,
    source_crs: str,
    attributes: dict[str, Any],
    record_number: int,
    polygon_index: int,
    category: str,
    group_tag: str,
    source_name: str,
) -> dict[str, Any] | None:
    local_polygon = _coordinates_to_polygon_m(mapping(polygon)["coordinates"], config)
    clipped = make_valid(local_polygon).intersection(_circle_polygon_m(config.region.outer_diameter_m / 2.0))
    polygons = [item for item in _extract_polygons(clipped) if item.area > 0.01]
    if not polygons:
        return None
    geometry: dict[str, Any]
    coordinates = [_polygon_m_to_lonlat_coordinates(item, config) for item in polygons]
    if len(coordinates) == 1:
        geometry = {"type": "Polygon", "coordinates": coordinates[0]}
    else:
        geometry = {"type": "MultiPolygon", "coordinates": coordinates}
    roi_distance_m = _geometry_distance_to_region_center_m(geometry, config)
    roi_zone = _roi_zone(roi_distance_m, config)
    if roi_zone is None:
        return None
    centroid = _centroid(geometry)
    centroid_distance_m = _distance_m(
        config.region.center_lat,
        config.region.center_lon,
        centroid[1],
        centroid[0],
    )
    feature_id = f"{path.stem}_{record_number}"
    if polygon_index > 1:
        feature_id = f"{feature_id}_{polygon_index}"
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "osm_type": "supplemental",
            "osm_id": feature_id,
            "category": category,
            "group_tag": group_tag,
            "source_tag": f"supplemental={source_name}",
            "source_type": "supplemental",
            "supplemental_input_id": source_name,
            "source": str(path),
            "source_crs": source_crs,
            "source_attributes": attributes,
            "record_number": record_number,
            "contributes_to_geometry": True,
            "geometry_role": "polygon_surface",
            "roi_zone": roi_zone,
            "reconstruction_scope": _reconstruction_scope(roi_zone),
            "include_in_building_lod22_reconstruction": _include_in_building_lod22_reconstruction(category, roi_zone),
            "centroid_distance_m": round(centroid_distance_m, 3),
            "roi_distance_m": round(roi_distance_m, 3),
            "area_m2": round(sum(item.area for item in polygons), 3),
            "tags": {},
        },
    }


def _shapefile_xy_to_lonlat(
    x: float,
    y: float,
    source_crs: str,
    config_key: str,
) -> tuple[float, float]:
    crs = _normalized_crs(source_crs)
    if crs == "EPSG:4326":
        return x, y
    if crs == "EPSG:25832":
        return _transverse_mercator_to_lonlat(
            x,
            y,
            semi_major=6378137.0,
            inverse_flattening=298.257223563,
            central_meridian_deg=9.0,
            scale=0.9996,
            false_easting=500000.0,
        )
    if crs == "EPSG:3003":
        lon, lat = _transverse_mercator_to_lonlat(
            x,
            y,
            semi_major=6378388.0,
            inverse_flattening=297.0,
            central_meridian_deg=9.0,
            scale=0.9996,
            false_easting=1500000.0,
        )
        return _helmert_to_wgs84_lonlat(
            lon,
            lat,
            semi_major=6378388.0,
            inverse_flattening=297.0,
            tx=-104.1,
            ty=-49.1,
            tz=-9.9,
            rx_arcsec=0.971,
            ry_arcsec=-2.917,
            rz_arcsec=0.714,
            scale_ppm=-11.68,
        )
    raise ConfigError(
        f"{config_key} currently supports EPSG:4326, EPSG:25832, and EPSG:3003"
    )


def _normalized_crs(value: str) -> str:
    return value.strip().upper().replace("::", ":")


def _transverse_mercator_to_lonlat(
    easting: float,
    northing: float,
    *,
    semi_major: float,
    inverse_flattening: float,
    central_meridian_deg: float,
    scale: float,
    false_easting: float,
) -> tuple[float, float]:
    flattening = 1.0 / inverse_flattening
    eccentricity_sq = flattening * (2.0 - flattening)
    second_eccentricity_sq = eccentricity_sq / (1.0 - eccentricity_sq)
    x = easting - false_easting
    meridional_arc = northing / scale
    mu = meridional_arc / (
        semi_major
        * (
            1.0
            - eccentricity_sq / 4.0
            - 3.0 * eccentricity_sq**2 / 64.0
            - 5.0 * eccentricity_sq**3 / 256.0
        )
    )
    e1 = (1.0 - math.sqrt(1.0 - eccentricity_sq)) / (1.0 + math.sqrt(1.0 - eccentricity_sq))
    footpoint_lat = (
        mu
        + (3.0 * e1 / 2.0 - 27.0 * e1**3 / 32.0) * math.sin(2.0 * mu)
        + (21.0 * e1**2 / 16.0 - 55.0 * e1**4 / 32.0) * math.sin(4.0 * mu)
        + (151.0 * e1**3 / 96.0) * math.sin(6.0 * mu)
        + (1097.0 * e1**4 / 512.0) * math.sin(8.0 * mu)
    )
    sin_lat = math.sin(footpoint_lat)
    cos_lat = math.cos(footpoint_lat)
    tan_lat = math.tan(footpoint_lat)
    n1 = semi_major / math.sqrt(1.0 - eccentricity_sq * sin_lat**2)
    r1 = semi_major * (1.0 - eccentricity_sq) / (1.0 - eccentricity_sq * sin_lat**2) ** 1.5
    t1 = tan_lat**2
    c1 = second_eccentricity_sq * cos_lat**2
    d = x / (n1 * scale)
    lat = footpoint_lat - (n1 * tan_lat / r1) * (
        d**2 / 2.0
        - (5.0 + 3.0 * t1 + 10.0 * c1 - 4.0 * c1**2 - 9.0 * second_eccentricity_sq) * d**4 / 24.0
        + (
            61.0
            + 90.0 * t1
            + 298.0 * c1
            + 45.0 * t1**2
            - 252.0 * second_eccentricity_sq
            - 3.0 * c1**2
        )
        * d**6
        / 720.0
    )
    lon = math.radians(central_meridian_deg) + (
        d
        - (1.0 + 2.0 * t1 + c1) * d**3 / 6.0
        + (5.0 - 2.0 * c1 + 28.0 * t1 - 3.0 * c1**2 + 8.0 * second_eccentricity_sq + 24.0 * t1**2)
        * d**5
        / 120.0
    ) / cos_lat
    return math.degrees(lon), math.degrees(lat)


def _helmert_to_wgs84_lonlat(
    lon_deg: float,
    lat_deg: float,
    *,
    semi_major: float,
    inverse_flattening: float,
    tx: float,
    ty: float,
    tz: float,
    rx_arcsec: float,
    ry_arcsec: float,
    rz_arcsec: float,
    scale_ppm: float,
) -> tuple[float, float]:
    x, y, z = _geodetic_to_cartesian(
        lon_deg,
        lat_deg,
        semi_major=semi_major,
        inverse_flattening=inverse_flattening,
    )
    rotation_scale = math.pi / (180.0 * 3600.0)
    rx = rx_arcsec * rotation_scale
    ry = ry_arcsec * rotation_scale
    rz = rz_arcsec * rotation_scale
    scale = 1.0 + scale_ppm * 1.0e-6
    wgs84_x = tx + scale * x - rz * y + ry * z
    wgs84_y = ty + rz * x + scale * y - rx * z
    wgs84_z = tz - ry * x + rx * y + scale * z
    return _cartesian_to_geodetic(
        wgs84_x,
        wgs84_y,
        wgs84_z,
        semi_major=6378137.0,
        inverse_flattening=298.257223563,
    )


def _geodetic_to_cartesian(
    lon_deg: float,
    lat_deg: float,
    *,
    semi_major: float,
    inverse_flattening: float,
) -> tuple[float, float, float]:
    lon = math.radians(lon_deg)
    lat = math.radians(lat_deg)
    flattening = 1.0 / inverse_flattening
    eccentricity_sq = flattening * (2.0 - flattening)
    prime_vertical_radius = semi_major / math.sqrt(1.0 - eccentricity_sq * math.sin(lat) ** 2)
    x = prime_vertical_radius * math.cos(lat) * math.cos(lon)
    y = prime_vertical_radius * math.cos(lat) * math.sin(lon)
    z = prime_vertical_radius * (1.0 - eccentricity_sq) * math.sin(lat)
    return x, y, z


def _cartesian_to_geodetic(
    x: float,
    y: float,
    z: float,
    *,
    semi_major: float,
    inverse_flattening: float,
) -> tuple[float, float]:
    flattening = 1.0 / inverse_flattening
    semi_minor = semi_major * (1.0 - flattening)
    eccentricity_sq = flattening * (2.0 - flattening)
    second_eccentricity_sq = (semi_major**2 - semi_minor**2) / semi_minor**2
    horizontal_radius = math.hypot(x, y)
    theta = math.atan2(z * semi_major, horizontal_radius * semi_minor)
    lon = math.atan2(y, x)
    lat = math.atan2(
        z + second_eccentricity_sq * semi_minor * math.sin(theta) ** 3,
        horizontal_radius - eccentricity_sq * semi_major * math.cos(theta) ** 3,
    )
    return math.degrees(lon), math.degrees(lat)


def _tree_shapefile_feature(
    *,
    lon: float,
    lat: float,
    config: AppConfig,
    tree_input: SupplementalShapefileConfig,
    path: Path,
    attributes: dict[str, Any],
    record_number: int,
    point_index: int,
    sequence_index: int,
) -> dict[str, Any] | None:
    geometry = {"type": "Point", "coordinates": [lon, lat]}
    centroid_distance_m = _distance_m(
        config.region.center_lat,
        config.region.center_lon,
        lat,
        lon,
    )
    roi_distance_m = _geometry_distance_to_region_center_m(geometry, config)
    roi_zone = _roi_zone(roi_distance_m, config)
    if roi_zone is None:
        return None
    tree_id = f"{path.stem}_{record_number}"
    if point_index > 1:
        tree_id = f"{tree_id}_{point_index}"
    tags = tree_tags_from_attributes(attributes)
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "osm_type": "supplemental",
            "osm_id": tree_id,
            "category": "trees",
            "group_tag": "tree",
            "source_tag": f"supplemental={tree_input.name}",
            "source_type": "supplemental",
            "supplemental_input_id": tree_input.name,
            "source": str(path),
            "source_crs": tree_input.crs,
            "source_attributes": attributes,
            "record_number": record_number,
            "sequence_index": sequence_index,
            "contributes_to_geometry": False,
            "geometry_role": _geometry_role(geometry),
            "roi_zone": roi_zone,
            "reconstruction_scope": _reconstruction_scope(roi_zone),
            "include_in_building_lod22_reconstruction": False,
            "centroid_distance_m": round(centroid_distance_m, 3),
            "roi_distance_m": round(roi_distance_m, 3),
            "tags": tags,
        },
    }

def _normalize_attribute_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_").replace("-", "_")


def _first_attribute_value(
    attributes: dict[str, Any],
    keys: tuple[str, ...],
) -> tuple[str, Any] | None:
    for key in keys:
        value = attributes.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or stripped in {"-", "--"} or stripped.lower() in {
                "unknown",
                "sconosciuto",
                "non noto",
                "n/a",
            }:
                continue
        return key, value
    return None


def _dbh_to_diameter_m(
    value: Any,
    matched_alias: str,
    default_unit: MetricUnit,
) -> float | None:
    return _length_to_m(value, matched_alias, default_unit)


def _circumference_to_m(
    value: Any,
    matched_alias: str,
    default_unit: MetricUnit,
) -> float | None:
    return _length_to_m(value, matched_alias, default_unit)


def _length_to_m(
    value: Any,
    matched_alias: str,
    default_unit: MetricUnit,
) -> float | None:
    parsed = _numeric_attribute_with_unit(value)
    if parsed is None:
        return None
    numeric, value_unit = parsed
    if not math.isfinite(numeric) or numeric <= 0.0:
        return None
    alias_unit = _unit_from_attribute_alias(matched_alias)
    if value_unit is not None and alias_unit is not None and value_unit != alias_unit:
        return None
    source_unit = value_unit or alias_unit or default_unit
    return numeric * {"mm": 0.001, "cm": 0.01, "m": 1.0}[source_unit]


_METRIC_VALUE_PATTERN = re.compile(
    r"^\s*([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+))\s*([a-zA-Z]+)?\s*$"
)
_METRIC_UNIT_ALIASES: dict[str, MetricUnit] = {
    "mm": "mm",
    "millimeter": "mm",
    "millimeters": "mm",
    "millimetre": "mm",
    "millimetres": "mm",
    "millimetro": "mm",
    "millimetri": "mm",
    "cm": "cm",
    "centimeter": "cm",
    "centimeters": "cm",
    "centimetre": "cm",
    "centimetres": "cm",
    "centimetro": "cm",
    "centimetri": "cm",
    "m": "m",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "metro": "m",
    "metri": "m",
}


def _numeric_attribute_with_unit(value: Any) -> tuple[float, MetricUnit | None] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value), None
    if not isinstance(value, str):
        return None
    match = _METRIC_VALUE_PATTERN.fullmatch(value)
    if match is None:
        return None
    unit_text = match.group(2)
    unit = _METRIC_UNIT_ALIASES.get(unit_text.lower()) if unit_text is not None else None
    if unit_text is not None and unit is None:
        return None
    try:
        return float(match.group(1).replace(",", ".")), unit
    except ValueError:
        return None


def _unit_from_attribute_alias(alias: str) -> MetricUnit | None:
    for unit in ("mm", "cm", "m"):
        if alias.endswith(f"_{unit}"):
            return unit
    return None


def _metric_aliases(*stems: str) -> tuple[str, ...]:
    return tuple(
        alias
        for stem in stems
        for alias in (stem, f"{stem}_mm", f"{stem}_cm", f"{stem}_m")
    )


TREE_ATTRIBUTE_MAPPINGS = (
    TreeAttributeMapping(
        tag="species",
        aliases=("species", "specie", "genus", "taxon", "nome_specie"),
    ),
    TreeAttributeMapping(
        tag="genus",
        aliases=("genus",),
    ),
    TreeAttributeMapping(
        tag="dbh",
        aliases=_metric_aliases(
            "dbh",
            "diameter_breast_height",
            "diametro",
            "diameter",
            "diam",
            "trunk_diameter",
        ),
        metric_tag="diameter",
        numeric_converter=_dbh_to_diameter_m,
        default_unit="cm",
    ),
    TreeAttributeMapping(
        tag="source_circumference",
        aliases=_metric_aliases(
            "circumference",
            "circonf",
            "circonferenza",
        ),
        metric_tag="circumference",
        numeric_converter=_circumference_to_m,
    ),
)


def tree_tags_from_attributes(
    attributes: dict[str, Any],
    *,
    mappings: Sequence[TreeAttributeMapping] = TREE_ATTRIBUTE_MAPPINGS,
) -> dict[str, Any]:
    """Normalize configured attribute aliases while preserving source data elsewhere."""
    tags: dict[str, Any] = {"natural": "tree"}
    normalized = {_normalize_attribute_key(key): value for key, value in attributes.items()}
    for attribute_mapping in mappings:
        matched_attribute = _first_attribute_value(normalized, attribute_mapping.aliases)
        if matched_attribute is None:
            continue
        matched_alias, value = matched_attribute
        tags[attribute_mapping.tag] = value
        if attribute_mapping.metric_tag is None or attribute_mapping.numeric_converter is None:
            continue
        metric_value = attribute_mapping.numeric_converter(
            value,
            matched_alias,
            attribute_mapping.default_unit,
        )
        if metric_value is not None:
            tags[attribute_mapping.metric_tag] = round(metric_value, 4)
    return tags
