"""Placement validation and coordinate resolution for air-purifier instances."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from cities_reconstruction.config import ConfigError
from cities_reconstruction.geometry.terrain import TerrainSampler
from cities_reconstruction.stages.air_purifiers.inputs import (
    finite_number,
    non_negative_integer,
    required_choice,
    required_text,
    target_dimension,
)
from cities_reconstruction.stages.air_purifiers.models import AirPurifierInstance, AirPurifierModel

PURIFIER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
TERRAIN_CLEARANCE_M = 0.05


def resolve_instances(
    features: list[Any], models: dict[str, AirPurifierModel], *, origin_x: float, origin_y: float,
    terrain_path: Path | None, terrain_sampler: TerrainSampler | None,
) -> list[AirPurifierInstance]:
    instances: list[AirPurifierInstance] = []
    seen: set[str] = set()
    for index, feature in enumerate(features, start=1):
        if not isinstance(feature, dict):
            raise ConfigError(f"air-purifier feature {index} must be an object")
        geometry = feature.get("geometry")
        properties = feature.get("properties")
        if not isinstance(geometry, dict) or geometry.get("type") != "Point":
            raise ConfigError(f"air-purifier feature {index} must have Point geometry")
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            raise ConfigError(f"air-purifier feature {index} has invalid coordinates")
        lon = finite_number(coordinates[0], f"air-purifier feature {index} longitude")
        lat = finite_number(coordinates[1], f"air-purifier feature {index} latitude")
        if not isinstance(properties, dict):
            raise ConfigError(f"air-purifier feature {index} properties must be an object")
        purifier_id = required_text(properties, "purifier_id", f"air-purifier feature {index}")
        if not PURIFIER_ID_PATTERN.fullmatch(purifier_id):
            raise ConfigError(f"unsafe air-purifier ID {purifier_id!r} in feature {index}")
        if purifier_id in seen:
            raise ConfigError(f"duplicate air-purifier ID {purifier_id!r}")
        seen.add(purifier_id)
        model_name = required_text(properties, "model", f"air-purifier feature {purifier_id!r}")
        if model_name not in models:
            raise ConfigError(f"unknown air-purifier model {model_name!r} for {purifier_id}")
        model = models[model_name]
        height, height_source = target_dimension(properties, "height_m", "HEIGHT_M", model.native_height_m, model.name)
        width, width_source = target_dimension(properties, "width_m", "WIDTH_M", model.native_width_m, model.name)
        depth, depth_source = target_dimension(properties, "depth_m", "DEPTH_M", model.native_depth_m, model.name)
        raw_rotation = properties.get("rotation_deg")
        if raw_rotation is None:
            rotation, rotation_source = 0.0, f"default:{model.name}"
        else:
            rotation, rotation_source = finite_number(raw_rotation, f"rotation_deg for {purifier_id}") % 360.0, "attribute:ROTATION_D"
        projected_x, projected_y = lonlat_to_epsg25832(lon, lat)
        local_x, local_y = projected_x - origin_x, projected_y - origin_y
        metadata_context = f"air-purifier feature {purifier_id!r}"
        input_id = required_text(properties, "urban_planning_input_id", metadata_context)
        roi_zone = required_choice(
            properties,
            "roi_zone",
            ("inner", "annular", "full"),
            metadata_context,
        )
        source = required_text(properties, "source", metadata_context)
        source_crs = required_choice(
            properties,
            "source_crs",
            ("EPSG:4326", "EPSG:3857"),
            metadata_context,
        )
        source_feature_index = non_negative_integer(
            properties.get("source_feature_index"),
            f"{metadata_context} source_feature_index",
        )
        source_properties = properties.get("source_properties")
        if not isinstance(source_properties, dict):
            raise ConfigError(f"{metadata_context} source_properties must be an object")
        base_z = 0.0
        terrain_source = "z=0 fallback"
        if terrain_sampler is not None and terrain_path is not None:
            radians = math.radians(rotation)
            cosine, sine = math.cos(radians), math.sin(radians)
            for x_offset, y_offset in ((-width / 2, -depth / 2), (-width / 2, depth / 2), (width / 2, -depth / 2), (width / 2, depth / 2)):
                corner_x = local_x + x_offset * cosine - y_offset * sine
                corner_y = local_y + x_offset * sine + y_offset * cosine
                try:
                    terrain_sampler(corner_x, corner_y)
                except ConfigError as exc:
                    raise ConfigError(
                        f"air-purifier footprint for {purifier_id!r} could not be projected onto terrain: {exc}"
                    ) from exc
            base_z = terrain_sampler(local_x, local_y) - TERRAIN_CLEARANCE_M
            terrain_source = str(terrain_path)
        instances.append(AirPurifierInstance(
            purifier_id=purifier_id, model_name=model_name, source_lon=lon, source_lat=lat,
            projected_x=projected_x, projected_y=projected_y,
            local_x=local_x, local_y=local_y, base_z=base_z,
            target_width_m=width, target_depth_m=depth, target_height_m=height,
            native_width_m=model.native_width_m, native_depth_m=model.native_depth_m, native_height_m=model.native_height_m,
            scale_x=width / model.native_width_m, scale_y=depth / model.native_depth_m, scale_z=height / model.native_height_m,
            rotation_deg=rotation, width_source=width_source, depth_source=depth_source, height_source=height_source,
            rotation_source=rotation_source, terrain_source=terrain_source,
            input_id=input_id, source=source, source_crs=source_crs,
            source_feature_index=source_feature_index, roi_zone=roi_zone,
            source_properties=dict(source_properties),
        ))
    return instances


def lonlat_to_epsg25832(lon: float, lat: float) -> tuple[float, float]:
    semi_major = 6378137.0
    flattening = 1 / 298.257223563
    eccentricity_sq = flattening * (2 - flattening)
    lat_rad, lon_rad = math.radians(lat), math.radians(lon)
    lon0, k0, false_easting = math.radians(9.0), 0.9996, 500000.0
    n = semi_major / math.sqrt(1 - eccentricity_sq * math.sin(lat_rad) ** 2)
    t = math.tan(lat_rad) ** 2
    c = (eccentricity_sq / (1 - eccentricity_sq)) * math.cos(lat_rad) ** 2
    a = (lon_rad - lon0) * math.cos(lat_rad)
    m = semi_major * (
        (1 - eccentricity_sq / 4 - 3 * eccentricity_sq**2 / 64 - 5 * eccentricity_sq**3 / 256) * lat_rad
        - (3 * eccentricity_sq / 8 + 3 * eccentricity_sq**2 / 32 + 45 * eccentricity_sq**3 / 1024) * math.sin(2 * lat_rad)
        + (15 * eccentricity_sq**2 / 256 + 45 * eccentricity_sq**3 / 1024) * math.sin(4 * lat_rad)
        - (35 * eccentricity_sq**3 / 3072) * math.sin(6 * lat_rad)
    )
    easting = false_easting + k0 * n * (a + (1 - t + c) * a**3 / 6 + (5 - 18*t + t*t + 72*c - 58*eccentricity_sq) * a**5 / 120)
    northing = k0 * (m + n * math.tan(lat_rad) * (a*a/2 + (5 - t + 9*c + 4*c*c) * a**4/24 + (61 - 58*t + t*t + 600*c - 330*eccentricity_sq) * a**6/720))
    return easting, northing
