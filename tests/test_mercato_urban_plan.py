from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from cities_reconstruction.config import load_config
from cities_reconstruction.stages import shapefiles
from tests.config_helpers import write_complete_config


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = (
    ROOT / "docs/assets/data/urban_planning/mercato_centrale/urban_plan.geojson"
)

EXPECTED_PURIFIERS = [
    ("AP-001", "compact_octagonal_tower", 4.0, 1.5, 1.5, 0.0),
    ("AP-002", "compact_four_side_tower", 4.0, 1.5, 1.5, 0.0),
    ("AP-003", "compact_octagonal_tower", 3.6, 1.35, 1.35, 0.0),
    ("AP-004", "compact_four_side_tower", 3.8, 1.4, 1.3, 0.0),
    ("AP-005", "compact_octagonal_tower", 4.4, 1.6, 1.6, 0.0),
    ("AP-006", "compact_four_side_tower", 4.2, 1.55, 1.45, 0.0),
]


def test_mercato_urban_plan_is_mixed_portable_geojson() -> None:
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    features = plan["features"]

    assert plan["type"] == "FeatureCollection"
    assert "crs" not in plan
    assert Counter(feature["properties"]["kind"] for feature in features) == {
        "tree": 35,
        "air_purifier": 6,
    }
    ids = [feature["properties"]["id"] for feature in features]
    assert ids == sorted(ids)
    assert len(set(ids)) == 41
    assert all(feature["geometry"]["type"] == "Point" for feature in features)
    assert all(len(feature["geometry"]["coordinates"]) == 2 for feature in features)
    assert all(
        -180.0 <= feature["geometry"]["coordinates"][0] <= 180.0
        and -90.0 <= feature["geometry"]["coordinates"][1] <= 90.0
        for feature in features
    )

    trees = [feature["properties"] for feature in features if feature["properties"]["kind"] == "tree"]
    assert [tree["id"] for tree in trees] == [f"MC-{index:03d}" for index in range(1, 36)]
    assert {tree["model"] for tree in trees} == {"large_round_broadleaf"}
    assert {tree["trunk_diameter_m"] for tree in trees} == {0.12}

    purifiers = [
        (
            properties["id"],
            properties["model"],
            properties["height_m"],
            properties["width_m"],
            properties["depth_m"],
            properties["rotation_deg"],
        )
        for properties in (
            feature["properties"]
            for feature in features
            if feature["properties"]["kind"] == "air_purifier"
        )
    ]
    assert purifiers == EXPECTED_PURIFIERS


def test_mercato_urban_plan_stage1_accepts_both_asset_kinds(tmp_path: Path) -> None:
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        center_lat=43.77677036533063,
        center_lon=11.253741873814542,
        inner_diameter_m=100.0,
        outer_diameter_m=400.0,
    )
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f'''\n\n[[urban_planning.inputs]]
name = "mercato_centrale"
path = "{PLAN_PATH.as_posix()}"
''',
        encoding="utf-8",
    )
    cached_overpass = tmp_path / "overpass.json"
    cached_overpass.write_text('{"elements": []}', encoding="utf-8")

    result = shapefiles.run(
        load_config(config_path),
        overpass_json_path=cached_overpass,
    )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["urban_planning"]["accepted_by_kind"] == {
        "tree": 35,
        "air_purifier": 6,
    }
    assert summary["urban_planning"]["outside_by_kind"] == {
        "tree": 0,
        "air_purifier": 0,
    }
    planning = json.loads(result.urban_planning_path.read_text(encoding="utf-8"))
    assert len(planning["features"]) == 41
    trees = json.loads(result.category_paths["trees"].read_text(encoding="utf-8"))
    purifiers = json.loads(result.air_purifiers_path.read_text(encoding="utf-8"))
    assert len(trees["features"]) == 35
    assert len(purifiers["features"]) == 6
