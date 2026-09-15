"""Horizontal CRS validation and transformations shared by pipeline stages."""

from __future__ import annotations

import math
from functools import lru_cache

from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError

from cities_reconstruction.errors import ConfigError

WGS84 = "EPSG:4326"
WEB_MERCATOR = "EPSG:3857"
EPSG_25832 = "EPSG:25832"
EPSG_3003 = "EPSG:3003"

EARTH_RADIUS_M = 6_371_000.0
WEB_MERCATOR_RADIUS_M = 6_378_137.0
WEB_MERCATOR_LIMIT_M = math.pi * WEB_MERCATOR_RADIUS_M


@lru_cache(maxsize=128)
def _horizontal_crs(value: str) -> CRS:
    normalized = value.strip()
    if normalized.lower().startswith("epsg::"):
        normalized = f"EPSG:{normalized[6:]}"
    elif normalized.lower().startswith("epsg:"):
        normalized = f"EPSG:{normalized[5:]}"
    try:
        parsed = CRS.from_user_input(normalized)
    except CRSError as exc:
        raise ConfigError(f"invalid horizontal CRS {value!r}: {exc}") from exc
    if len(parsed.axis_info) != 2 or not (parsed.is_projected or parsed.is_geographic):
        raise ConfigError(f"CRS {value!r} must describe two horizontal coordinates")
    return parsed


def canonical_crs(value: str) -> str:
    """Validate a horizontal CRS and return its authority name or full WKT."""
    return _horizontal_crs(value.strip()).to_string()


def crs_label(value: str) -> str:
    """Short source label; the full definition remains the transformation authority."""
    parsed = _horizontal_crs(value)
    base = (parsed.source_crs or parsed) if parsed.is_bound else parsed
    authority = base.to_authority()
    return f"{authority[0]}:{authority[1]}" if authority is not None else base.name


def crs_equal(first: str, second: str) -> bool:
    left, right = _horizontal_crs(first), _horizontal_crs(second)
    if left.is_bound != right.is_bound:
        left = (left.source_crs or left) if left.is_bound else left
        right = (right.source_crs or right) if right.is_bound else right
    return bool(left.equals(right, ignore_axis_order=True))


def validate_working_crs(value: str) -> str:
    """Require a projected metre plane suitable for reconstruction distances."""
    parsed = _horizontal_crs(value)
    if (
        not parsed.is_projected
        or parsed.equals(CRS.from_epsg(3857))
        or any(abs(axis.unit_conversion_factor - 1.0) > 1e-12 for axis in parsed.axis_info)
    ):
        raise ConfigError(
            f"working CRS {value!r} must be a projected CRS in metres; "
            "geographic coordinates and Web Mercator are not reconstruction coordinates"
        )
    return parsed.to_string()


@lru_cache(maxsize=128)
def _transformer(source_crs: str, target_crs: str) -> Transformer:
    try:
        return Transformer.from_crs(
            _horizontal_crs(source_crs),
            _horizontal_crs(target_crs),
            always_xy=True,
            allow_ballpark=False,
        )
    except ProjError as exc:
        raise ConfigError(f"cannot transform CRS {source_crs!r} to {target_crs!r}: {exc}") from exc


def transform_xy(x: float, y: float, source_crs: str, target_crs: str) -> tuple[float, float]:
    """Transform horizontal GIS X/Y coordinates, without changing height."""
    if not math.isfinite(x) or not math.isfinite(y):
        raise ConfigError("CRS transformation requires finite coordinates")
    try:
        result_x, result_y = _transformer(source_crs, target_crs).transform(x, y, errcheck=True)
    except ProjError as exc:
        raise ConfigError(f"cannot transform coordinates from {source_crs} to {target_crs}: {exc}") from exc
    if not math.isfinite(result_x) or not math.isfinite(result_y):
        raise ConfigError("CRS transformation did not produce finite coordinates")
    return float(result_x), float(result_y)


def project_lonlat(lon: float, lat: float, target_crs: str) -> tuple[float, float]:
    return transform_xy(lon, lat, WGS84, target_crs)


def automatic_utm_crs(lon: float, lat: float) -> str:
    """Choose the UTM zone covering a WGS84 ROI center."""
    from pyproj.aoi import AreaOfInterest
    from pyproj.database import query_utm_crs_info

    choices = query_utm_crs_info(datum_name="WGS 84", area_of_interest=AreaOfInterest(lon, lat, lon, lat))
    if not choices:
        raise ConfigError("cannot select a UTM working CRS at this ROI; set reconstruction.working_crs explicitly")
    return f"{choices[0].auth_name}:{choices[0].code}"


def normalize_crs_name(value: str) -> str:
    """Return a canonical uppercase EPSG name while accepting ``EPSG::`` spelling."""

    return value.strip().upper().replace("::", ":")


def lonlat_to_epsg25832(lon: float, lat: float) -> tuple[float, float]:
    """Compatibility entry point for the Florence projected CRS."""
    return project_lonlat(lon, lat, EPSG_25832)


def epsg25832_to_lonlat(easting: float, northing: float) -> tuple[float, float]:
    return transform_xy(easting, northing, EPSG_25832, WGS84)


def web_mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    return transform_xy(x, y, WEB_MERCATOR, WGS84)


def lonlat_to_local_xy(
    lon: float,
    lat: float,
    *,
    center_lon: float,
    center_lat: float,
) -> tuple[float, float]:
    """Project longitude/latitude to the established small-ROI local metre plane."""

    x_m = math.radians(lon - center_lon) * EARTH_RADIUS_M * math.cos(math.radians(center_lat))
    y_m = math.radians(lat - center_lat) * EARTH_RADIUS_M
    return x_m, y_m


def local_xy_to_lonlat(
    x_m: float,
    y_m: float,
    *,
    center_lon: float,
    center_lat: float,
) -> tuple[float, float]:
    """Invert the established small-ROI local metre projection."""

    lon = center_lon + math.degrees(x_m / (EARTH_RADIUS_M * math.cos(math.radians(center_lat))))
    lat = center_lat + math.degrees(y_m / EARTH_RADIUS_M)
    return lon, lat


def lonlat_bbox_from_radius(
    *,
    center_lon: float,
    center_lat: float,
    radius_m: float,
) -> tuple[float, float, float, float]:
    """Return the established lon/lat bounding box around a small metric ROI."""

    lat_delta = math.degrees(radius_m / EARTH_RADIUS_M)
    lon_delta = math.degrees(radius_m / (EARTH_RADIUS_M * math.cos(math.radians(center_lat))))
    return (
        center_lon - lon_delta,
        center_lat - lat_delta,
        center_lon + lon_delta,
        center_lat + lat_delta,
    )
