from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest

from cities_reconstruction.config import load_config
from tests.config_helpers import write_complete_config


def _transformation_module() -> ModuleType:
    try:
        return import_module("cities_reconstruction.stages.shapefiles.transformation")
    except ModuleNotFoundError:
        pytest.fail("the focused shapefiles transformation module is missing")


def test_ordered_rules_classify_overpass_payload_into_features(tmp_path: Path) -> None:
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        shapefiles_block="""
[shapefiles]
surface_precedence = ["green_areas", "roads"]
[[shapefiles.classification_rules]]
category = "green_areas"
group_tag = "pedestrian_green"
match_any = ["highway=pedestrian"]
[[shapefiles.classification_rules]]
category = "roads"
group_tag = "road"
match_any = ["highway"]
""".strip(),
    )
    raw = {
        "elements": [
            {
                "type": "way",
                "id": 7,
                "tags": {"highway": "pedestrian"},
                "geometry": [
                    {"lat": 43.7695, "lon": 11.2557},
                    {"lat": 43.7695, "lon": 11.2559},
                    {"lat": 43.7697, "lon": 11.2559},
                    {"lat": 43.7695, "lon": 11.2557},
                ],
            }
        ]
    }

    features, skipped_count, skipped_by_reason = _transformation_module().overpass_to_features(
        raw,
        load_config(config_path),
    )

    assert skipped_count == 0
    assert skipped_by_reason == {}
    assert features[0]["properties"]["category"] == "green_areas"
    assert features[0]["properties"]["group_tag"] == "pedestrian_green"
    assert features[0]["properties"]["source_tag"] == "highway=pedestrian"


def test_tag_inventory_separates_classified_and_unclassified_feature_tags(tmp_path: Path) -> None:
    config = load_config(write_complete_config(tmp_path / "config.toml"))
    raw = {
        "elements": [
            {"type": "way", "id": 1, "tags": {"building": "yes", "name": "Palazzo"}},
            {"type": "node", "id": 2, "tags": {"shop": "books", "name": "Libreria"}},
            {"type": "node", "id": 3},
        ]
    }

    inventory = _transformation_module().build_tag_inventory(raw, "cached fixture", config)

    assert inventory["source"] == "cached fixture"
    assert inventory["raw_elements"] == 3
    assert inventory["tagged_elements"] == 2
    assert inventory["classified_source_tag_counts"] == {"building=yes": 1}
    assert inventory["unclassified_feature_like_tag_value_counts"] == {"shop=books": 1}
    assert inventory["unclassified_tag_value_counts"] == {
        "name=Libreria": 1,
        "shop=books": 1,
    }
