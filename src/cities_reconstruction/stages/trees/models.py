"""Stage-local domain records for parametric tree reconstruction."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TreeInstance:
    tree_id: str
    species: str
    source_species: str | None
    model_category: str
    crown_shape: str
    x: float
    y: float
    z: float
    height_m: float
    crown_radius_m: float
    trunk_radius_m: float
    trunk_height_m: float
    roi_zone: str
    osm_id: object | None
    model_source: str
    height_source: str
    crown_radius_source: str
    trunk_radius_source: str
    used_tags: tuple[str, ...]
    defaulted_fields: tuple[str, ...]
