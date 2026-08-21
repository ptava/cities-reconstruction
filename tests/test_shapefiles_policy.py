from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cities_reconstruction.config import load_config
from tests.config_helpers import write_complete_config


def _policy_module() -> ModuleType:
    try:
        return import_module("cities_reconstruction.stages.shapefiles_policy")
    except ModuleNotFoundError:
        pytest.fail("the focused shapefiles policy module is missing")


def test_tree_deduplication_removes_only_overpass_tree_nearest_supplement() -> None:
    policy = _policy_module()
    overpass_tree = _point_feature(101, 11.2558, 43.7696, source_type="overpass")
    distant_tree = _point_feature(102, 11.2568, 43.7696, source_type="overpass")
    unrelated_feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [11.2558, 43.7696]},
        "properties": {
            "osm_id": 201,
            "category": "other_terrain",
            "source_tag": "natural=tree",
            "source_type": "overpass",
        },
    }
    supplemental_trees = [
        _point_feature("near", 11.2558, 43.7696, source_type="supplemental"),
        _point_feature("farther", 11.25581, 43.7696, source_type="supplemental"),
    ]

    filtered, diagnostics = policy.remove_overpass_trees_overlapping_supplemental_trees(
        [overpass_tree, distant_tree, unrelated_feature],
        supplemental_trees,
        tolerance_m=2.0,
    )

    assert [feature["properties"]["osm_id"] for feature in filtered] == [102, 201]
    assert diagnostics["enabled"] is True
    assert diagnostics["overpass_tree_count"] == 2
    assert diagnostics["removed_overpass_tree_count"] == 1
    assert diagnostics["removed_overpass_tree_ids"] == [101]
    assert diagnostics["removed_overpass_tree_markers"] == [
        {
            "osm_id": 101,
            "coordinates": [11.2558, 43.7696],
            "nearest_supplemental_tree_distance_m": 0.0,
            "nearest_supplemental_tree_id": "near",
        }
    ]


def test_surface_resolution_applies_precedence_independently_of_input_order(tmp_path: Path) -> None:
    policy = _policy_module()
    config = load_config(write_complete_config(tmp_path / "config.toml"))
    coordinates = [[
        [11.2557, 43.7695],
        [11.2559, 43.7695],
        [11.2559, 43.7697],
        [11.2557, 43.7697],
        [11.2557, 43.7695],
    ]]
    road = _surface_feature(301, "roads", "road", coordinates)
    building = _surface_feature(302, "buildings", "building", coordinates)

    resolved, diagnostics = policy.resolve_surface_overlaps([road, building], config)

    assert [feature["properties"]["osm_id"] for feature in resolved] == [302]
    assert diagnostics["input_polygon_features"] == 2
    assert diagnostics["accepted_polygon_features"] == 1
    assert diagnostics["removed_polygon_features"] == 1
    assert diagnostics["clipped_polygon_features"] == 0
    assert diagnostics["by_category"]["roads"]["removed_features"] == 1
    assert diagnostics["by_category"]["buildings"]["accepted_features"] == 1


def _point_feature(
    feature_id: int | str,
    lon: float,
    lat: float,
    *,
    source_type: str,
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "osm_id": feature_id,
            "category": "trees",
            "source_tag": "natural=tree",
            "source_type": source_type,
        },
    }


def _surface_feature(
    feature_id: int,
    category: str,
    group_tag: str,
    coordinates: list[list[list[float]]],
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coordinates},
        "properties": {
            "osm_id": feature_id,
            "category": category,
            "group_tag": group_tag,
            "contributes_to_geometry": True,
        },
    }
