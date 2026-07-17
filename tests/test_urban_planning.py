from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.urban_planning import load_inputs
from tests.config_helpers import ROOT, write_complete_config


def test_loads_mixed_4326_and_3857_inputs_with_global_ids(tmp_path: Path) -> None:
    tree_path = _write_plan(
        tmp_path / "trees.geojson",
        [_feature([11.2535, 43.7767], id="TREE-001", kind="tree", model="large_round_broadleaf", STREET="Via Roma")],
    )
    x, y = _web_mercator(11.2535, 43.7767)
    purifier_path = _write_plan(
        tmp_path / "purifiers.geojson",
        [_feature([x, y], ID="AP-001", KIND="air_purifier", MODEL="compact_octagonal_tower", HEIGHT_M=4.2)],
    )

    result = load_inputs(_planning_config(tmp_path, [("trees", tree_path, "EPSG:4326", True), ("purifiers", purifier_path, "EPSG:3857", True)]))

    assert [feature["properties"]["id"] for feature in result.accepted_features] == ["TREE-001", "AP-001"]
    assert all(feature["properties"]["urban_planning_input_id"] for feature in result.accepted_features)
    assert result.accepted_features[1]["geometry"]["coordinates"] == pytest.approx([11.2535, 43.7767])
    assert result.accepted_features[0]["properties"]["source_properties"] == {"STREET": "Via Roma"}
    assert result.accepted_features[1]["properties"]["height_m"] == 4.2
    assert result.per_input == {
        "trees": {"source_features": 1, "accepted_features": 1, "outside_roi": 0},
        "purifiers": {"source_features": 1, "accepted_features": 1, "outside_roi": 0},
    }


def test_accepts_dotted_id_from_global_safe_id_contract(tmp_path: Path) -> None:
    path = _write_plan(
        tmp_path / "plan.geojson",
        [_feature([11.2535, 43.7767], id="TREE.1", kind="tree", model="large_round_broadleaf")],
    )

    result = load_inputs(_planning_config(tmp_path, [("plan", path, "EPSG:4326", True)]))

    assert result.accepted_features[0]["properties"]["id"] == "TREE.1"


def test_reports_outside_roi_and_does_not_open_disabled_inputs(tmp_path: Path) -> None:
    outside_path = _write_plan(
        tmp_path / "outside.geojson",
        [_feature([12.0, 44.0], id="TREE-OUT", kind="tree", model="large_round_broadleaf")],
    )
    config = _planning_config(
        tmp_path,
        [("outside", outside_path, "EPSG:4326", True), ("disabled", tmp_path / "missing.geojson", "EPSG:4326", False)],
    )

    result = load_inputs(config)

    assert result.accepted_features == ()
    assert [feature["properties"]["id"] for feature in result.outside_roi_features] == ["TREE-OUT"]
    assert result.outside_roi == result.outside_roi_features
    assert result.per_input["disabled"] == {"source_features": 0, "accepted_features": 0, "outside_roi": 0}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"type": "Feature", "features": []}, r"plan.*FeatureCollection"),
        ({"type": "FeatureCollection", "features": {}}, r"plan.*features.*array"),
        ({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": []}, "properties": {"id": "TREE-1", "kind": "tree", "model": "large_round_broadleaf"}}]}, r"plan.*feature 0.*Point"),
        ({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.25, 43.77, 2.0]}, "properties": {"id": "TREE-1", "kind": "tree", "model": "large_round_broadleaf"}}]}, r"plan.*feature 0.*exactly two"),
        ({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [181.0, 43.77]}, "properties": {"id": "TREE-1", "kind": "tree", "model": "large_round_broadleaf"}}]}, r"plan.*feature 0.*longitude"),
    ],
)
def test_rejects_malformed_collection_or_geometry(tmp_path: Path, payload: dict[str, object], message: str) -> None:
    path = tmp_path / "plan.geojson"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_inputs(_planning_config(tmp_path, [("plan", path, "EPSG:4326", True)]))


@pytest.mark.parametrize(
    ("properties", "message"),
    [
        ({"id": "bad/id", "kind": "tree", "model": "large_round_broadleaf"}, r"plan.*feature 0 \(bad/id\).*unsafe id"),
        ({"id": "TREE-1", "model": "large_round_broadleaf"}, r"plan.*feature 0 \(TREE-1\).*requires kind"),
        ({"id": "TREE-1", "kind": "bench", "model": "large_round_broadleaf"}, r"plan.*feature 0 \(TREE-1\).*unknown kind"),
        ({"id": "TREE-1", "kind": "tree"}, r"plan.*feature 0 \(TREE-1\).*requires model"),
        ({"id": "TREE-1", "kind": "tree", "model": "not_a_tree"}, r"plan.*feature 0 \(TREE-1\).*unknown model"),
        ({"id": "AP-1", "kind": "air_purifier", "model": "not_a_purifier"}, r"plan.*feature 0 \(AP-1\).*unknown model"),
        ({"id": "TREE-1", "kind": "tree", "model": "large_round_broadleaf", "height_m": 0}, r"plan.*feature 0 \(TREE-1\).*height_m.*positive"),
        ({"id": "AP-1", "kind": "air_purifier", "model": "compact_octagonal_tower", "rotation_deg": math.inf}, r"plan.*feature 0 \(AP-1\).*rotation_deg.*finite"),
        ({"id": "TREE-1", "kind": "tree", "model": "large_round_broadleaf", "heigth_m": 8}, r"plan.*feature 0 \(TREE-1\).*unknown modelling property.*heigth_m"),
        ({"id": "TREE-1", "kind": "Tree", "model": "large_round_broadleaf"}, r"plan.*feature 0 \(TREE-1\).*unknown kind.*Tree"),
        ({"id": "TREE-1", "kind": "tree", "model": "large_round_broadleaf", "width_m": 2}, r"plan.*feature 0 \(TREE-1\).*property 'width_m'.*kind 'tree'"),
        ({"id": "TREE-1", "kind": "tree", "model": "large_round_broadleaf", "depth_m": 2}, r"plan.*feature 0 \(TREE-1\).*property 'depth_m'.*kind 'tree'"),
        ({"id": "TREE-1", "kind": "tree", "model": "large_round_broadleaf", "rotation_deg": 2}, r"plan.*feature 0 \(TREE-1\).*property 'rotation_deg'.*kind 'tree'"),
        ({"id": "AP-1", "kind": "air_purifier", "model": "compact_octagonal_tower", "crown_diameter_m": 2}, r"plan.*feature 0 \(AP-1\).*property 'crown_diameter_m'.*kind 'air_purifier'"),
        ({"id": "AP-1", "kind": "air_purifier", "model": "compact_octagonal_tower", "trunk_diameter_m": 0.2}, r"plan.*feature 0 \(AP-1\).*property 'trunk_diameter_m'.*kind 'air_purifier'"),
        ({"id": "TREE-1", "kind": "tree", "model": "large_round_broadleaf", "diameter_m": 2}, r"plan.*feature 0 \(TREE-1\).*unknown modelling property.*diameter_m"),
        ({"id": "TREE-1", "kind": "tree", "model": "large_round_broadleaf", "crown_radius_m": 2}, r"plan.*feature 0 \(TREE-1\).*unknown modelling property.*crown_radius_m"),
        ({"id": "TREE-1", "kind": "tree", "model": "large_round_broadleaf", "trunk_radius_m": 0.1}, r"plan.*feature 0 \(TREE-1\).*unknown modelling property.*trunk_radius_m"),
        ({"id": "AP-1", "kind": "air_purifier", "model": "compact_octagonal_tower", "rotation_degrees": 15}, r"plan.*feature 0 \(AP-1\).*unknown modelling property.*rotation_degrees"),
        ({"id": "AP-1", "kind": "air_purifier", "model": "compact_octagonal_tower", "rotation_axis": "z"}, r"plan.*feature 0 \(AP-1\).*unknown modelling property.*rotation_axis"),
    ],
)
def test_rejects_invalid_planning_properties(tmp_path: Path, properties: dict[str, object], message: str) -> None:
    path = _write_plan(tmp_path / "plan.geojson", [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.2535, 43.7767]}, "properties": properties}])

    with pytest.raises(ConfigError, match=message):
        load_inputs(_planning_config(tmp_path, [("plan", path, "EPSG:4326", True)]))


def test_rejects_duplicate_ids_globally_with_both_locations(tmp_path: Path) -> None:
    first = _write_plan(tmp_path / "first.geojson", [_feature([11.2535, 43.7767], id="SHARED-1", kind="tree", model="large_round_broadleaf")])
    second = _write_plan(tmp_path / "second.geojson", [_feature([11.2536, 43.7767], id="SHARED-1", kind="air_purifier", model="compact_octagonal_tower")])

    with pytest.raises(ConfigError, match=r"second.*feature 0 \(SHARED-1\).*duplicate id.*first.*feature 0"):
        load_inputs(_planning_config(tmp_path, [("first", first, "EPSG:4326", True), ("second", second, "EPSG:4326", True)]))


def test_preserves_nonempty_model_when_optional_catalog_is_not_configured(tmp_path: Path) -> None:
    path = _write_plan(
        tmp_path / "plan.geojson",
        [_feature([11.2535, 43.7767], id="AP-EXTERNAL", kind="air_purifier", model="external_catalog_model")],
    )

    result = load_inputs(
        _planning_config(tmp_path, [("plan", path, "EPSG:4326", True)], with_air_catalog=False)
    )

    assert result.accepted_features[0]["properties"]["model"] == "external_catalog_model"


@pytest.mark.parametrize(
    ("catalog_payload", "message"),
    [
        ({"models": []}, r"tree model library must contain at least one model"),
        ({"models": ["large_round_broadleaf"]}, r"tree model library entry 1 must be an object"),
        ({"models": [{}]}, r"tree model library entry 1 must have a non-empty name"),
        ({"models": [{"name": "  "}]}, r"tree model library entry 1 must have a non-empty name"),
        (
            {"models": [{"name": "large_round_broadleaf"}, {"name": "large_round_broadleaf"}]},
            r"tree model library contains duplicate name 'large_round_broadleaf'",
        ),
    ],
    ids=("empty", "non-object-entry", "missing-name", "blank-name", "duplicate-name"),
)
def test_rejects_invalid_configured_model_catalog(
    tmp_path: Path,
    catalog_payload: dict[str, object],
    message: str,
) -> None:
    path = _write_plan(
        tmp_path / "plan.geojson",
        [_feature([11.2535, 43.7767], id="TREE-1", kind="tree", model="large_round_broadleaf")],
    )

    with pytest.raises(ConfigError, match=message):
        load_inputs(
            _planning_config(
                tmp_path,
                [("plan", path, "EPSG:4326", True)],
                tree_catalog_payload=catalog_payload,
            )
        )


def _planning_config(
    tmp_path: Path,
    inputs: list[tuple[str, Path, str, bool]],
    *,
    with_air_catalog: bool = True,
    tree_catalog_payload: dict[str, object] | None = None,
):
    tree_catalog_path = None
    if tree_catalog_payload is not None:
        tree_catalog_path = tmp_path / "tree-catalog.json"
        tree_catalog_path.write_text(json.dumps(tree_catalog_payload), encoding="utf-8")
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        center_lat=43.7767,
        center_lon=11.2535,
        inner_diameter_m=200.0,
        outer_diameter_m=1000.0,
        model_library_path=tree_catalog_path,
        air_purifiers_block=(
            f'''[air_purifiers]
model_library_path = "{(ROOT / "docs/assets/air_purifier_towers/parameters.json").as_posix()}"'''
            if with_air_catalog
            else ""
        ),
    )
    entries = "\n".join(
        f'''[[urban_planning.inputs]]
name = "{name}"
path = "{path.as_posix()}"
crs = "{crs}"
enabled = {str(enabled).lower()}'''
        for name, path, crs, enabled in inputs
    )
    config_path.write_text(config_path.read_text(encoding="utf-8") + "\n\n" + entries + "\n", encoding="utf-8")
    return load_config(config_path)


def _write_plan(path: Path, features: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    return path


def _feature(coordinates: list[float], **properties: object) -> dict[str, object]:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": coordinates}, "properties": properties}


def _web_mercator(lon: float, lat: float) -> tuple[float, float]:
    radius = 6_378_137.0
    return radius * math.radians(lon), radius * math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))
