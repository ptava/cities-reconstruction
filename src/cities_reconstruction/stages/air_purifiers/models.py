"""Stage-local records for catalogued air-purifier models and placements."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cities_reconstruction.geometry.stl_regions import RegionMesh


@dataclass(frozen=True)
class AirPurifierModel:
    name: str
    kind: str
    source_path: Path
    native_width_m: float
    native_depth_m: float
    native_height_m: float
    linear_tolerance_m: float
    mesh: RegionMesh


@dataclass(frozen=True)
class AirPurifierInstance:
    purifier_id: str
    model_name: str
    source_lon: float
    source_lat: float
    projected_x: float
    projected_y: float
    local_x: float
    local_y: float
    base_z: float
    target_width_m: float
    target_depth_m: float
    target_height_m: float
    native_width_m: float
    native_depth_m: float
    native_height_m: float
    scale_x: float
    scale_y: float
    scale_z: float
    rotation_deg: float
    width_source: str
    depth_source: str
    height_source: str
    rotation_source: str
    terrain_source: str
    input_id: str
    source: str
    source_crs: str
    source_feature_index: int
    roi_zone: str
    source_properties: dict[str, Any]
