from __future__ import annotations

import json
import os
import struct
from dataclasses import replace
from pathlib import Path

import pytest
from shapely.geometry import Point

from cities_reconstruction import artifacts
from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.stage_contract import StageOutput
from cities_reconstruction.stages.shapefiles import inputs as shapefiles_inputs
from cities_reconstruction.stages.shapefiles import publication as shapefiles_publication
from cities_reconstruction.stages.shapefiles import stage as shapefiles
from tests.config_helpers import DEFAULT_SHAPEFILES_BLOCK, write_complete_config

ROOT = Path(__file__).resolve().parents[3]


def _config_with_supplements(
    tmp_path: Path,
    *,
    tree_path: Path,
    surface_path: Path,
) -> Path:
    return write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        shapefiles_block=f'''[shapefiles]
surface_precedence = [
    "buildings", "water", "green_areas", "supplemental:municipal_streets",
    "roads", "concrete", "other_terrain",
]

[[shapefiles.supplemental]]
name = "municipal_trees"
path = "{tree_path.as_posix()}"
crs = "EPSG:4326"
category = "trees"

[[shapefiles.supplemental]]
name = "municipal_streets"
path = "{surface_path.as_posix()}"
crs = "EPSG:4326"
category = "roads"
group_tag = "municipal_streets"

[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building"
match_any = ["building"]

[[shapefiles.classification_rules]]
category = "roads"
group_tag = "road"
match_any = ["highway"]''',
    )


def test_builds_query_with_outer_radius(tmp_path: Path) -> None:
    config_path = write_complete_config(
        tmp_path / "config.toml",
        outer_diameter_m=500.0,
        overpass_timeout_s=180.0,
    )
    config = load_config(config_path)

    inventory_query = shapefiles.build_tag_inventory_query(config)
    query = shapefiles.build_overpass_query(config)
    query_batches = shapefiles.build_overpass_query_batches(config)

    assert "nwr(around:250.0,43.76960000,11.25580000)" in inventory_query
    assert "out tags center" in inventory_query
    assert "around:250.0,43.76960000,11.25580000" in query
    assert 'way(around:250.0,43.76960000,11.25580000)[~"^(' in query
    assert 'relation(around:250.0,43.76960000,11.25580000)[~"^(' in query
    assert "building|building:part|highway|landuse" in query
    assert "surface|amenity|tourism|historic|man_made" in query
    assert 'way(around:250.0,43.76960000,11.25580000)["area"="yes"]' in query
    assert 'node(around:250.0,43.76960000,11.25580000)["natural"="tree"]' in query
    assert 'nwr(around' not in query
    assert len(query_batches) == 1
    assert all('out body geom;' in batch for batch in query_batches)
    assert sum(batch.count('(around:') for batch in query_batches) == 7


def test_uses_ordered_toml_rules_to_classify_overpass_features(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        shapefiles_block="""
[shapefiles]
surface_precedence = ["buildings", "water", "green_areas", "roads", "concrete", "other_terrain"]
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
                "id": 1,
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

    features, skipped_count, skipped_by_reason = shapefiles.overpass_to_features(raw, load_config(config_path))

    assert skipped_count == 0
    assert skipped_by_reason == {}
    assert features[0]["properties"]["category"] == "green_areas"
    assert features[0]["properties"]["group_tag"] == "pedestrian_green"
    assert features[0]["properties"]["source_tag"] == "highway=pedestrian"


def test_surface_precedence_uses_most_specific_selector_before_category_fallback(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        shapefiles_block="""
[shapefiles]
surface_precedence = ["green_areas", "concrete", "supplemental:municipal_green"]
[[shapefiles.supplemental]]
name = "municipal_green"
path = "green.shp"
crs = "EPSG:4326"
category = "green_areas"
group_tag = "green_area"
[[shapefiles.classification_rules]]
category = "green_areas"
group_tag = "green_area"
match_any = ["leisure=park"]
[[shapefiles.classification_rules]]
category = "concrete"
group_tag = "paved_ground"
match_any = ["surface=concrete"]
""".strip(),
    )
    config = load_config(config_path)
    overpass_green = {"properties": {"category": "green_areas", "group_tag": "green_area"}}
    concrete = {"properties": {"category": "concrete", "group_tag": "paved_ground"}}
    supplemental_green = {
        "properties": {
            "category": "green_areas",
            "group_tag": "green_area",
            "supplemental_input_id": "municipal_green",
        }
    }

    assert shapefiles._surface_precedence_rank(overpass_green, config) == 0
    assert shapefiles._surface_precedence_rank(concrete, config) == 1
    assert shapefiles._surface_precedence_rank(supplemental_green, config) == 2


def test_supplemental_tree_and_surface_inputs_complete_osm_data(tmp_path: Path) -> None:
    tree_path = tmp_path / "municipal_trees.shp"
    surface_path = tmp_path / "municipal_streets.shp"
    _write_point_shapefile(tree_path, [(11.2558, 43.7696)])
    _write_tree_dbf(
        tree_path.with_suffix(".dbf"),
        [{"SPECIE": "Tilia", "DBH": 32.0, "CIRCONF_CM": 100.5}],
    )
    _write_polygon_shapefile(
        surface_path,
        [[
            (11.25570, 43.76950),
            (11.25595, 43.76950),
            (11.25595, 43.76972),
            (11.25570, 43.76972),
            (11.25570, 43.76950),
        ]],
    )
    config_path = _config_with_supplements(
        tmp_path,
        tree_path=tree_path,
        surface_path=surface_path,
    )
    cached = tmp_path / "overpass.json"
    cached.write_text('{"elements": []}', encoding="utf-8")

    result = shapefiles.run(load_config(config_path), overpass_json_path=cached)

    trees = json.loads(result.category_paths["trees"].read_text(encoding="utf-8"))["features"]
    roads = json.loads(result.category_paths["roads"].read_text(encoding="utf-8"))["features"]
    assert {feature["properties"]["supplemental_input_id"] for feature in trees} == {"municipal_trees"}
    assert {feature["properties"]["supplemental_input_id"] for feature in roads} == {"municipal_streets"}
    assert trees[0]["properties"]["source_attributes"]["SPECIE"] == "Tilia"
    assert "planning_status" not in trees[0]["properties"]


def test_supplemental_surface_rejects_point_geometry_with_input_name(tmp_path: Path) -> None:
    point_path = tmp_path / "wrong_surface.shp"
    _write_point_shapefile(point_path, [(11.2558, 43.7696)])
    config_path = write_complete_config(
        tmp_path / "config.toml",
        shapefiles_extra=f'''[[shapefiles.supplemental]]
name = "municipal_streets"
path = "{point_path.as_posix()}"
crs = "EPSG:4326"
category = "roads"
group_tag = "municipal_streets"''',
    )
    cached = tmp_path / "overpass.json"
    cached.write_text('{"elements": []}', encoding="utf-8")

    with pytest.raises(ConfigError, match="municipal_streets.*Polygon"):
        shapefiles.run(load_config(config_path), overpass_json_path=cached)


def test_supplemental_tree_rejects_polygon_geometry_with_input_name(tmp_path: Path) -> None:
    polygon_path = tmp_path / "wrong_trees.shp"
    _write_polygon_shapefile(
        polygon_path,
        [[
            (11.25570, 43.76950),
            (11.25595, 43.76950),
            (11.25595, 43.76972),
            (11.25570, 43.76950),
        ]],
    )
    config_path = write_complete_config(
        tmp_path / "config.toml",
        shapefiles_extra=f'''[[shapefiles.supplemental]]
name = "municipal_trees"
path = "{polygon_path.as_posix()}"
crs = "EPSG:4326"
category = "trees"''',
    )
    cached = tmp_path / "overpass.json"
    cached.write_text('{"elements": []}', encoding="utf-8")

    with pytest.raises(ConfigError, match="municipal_trees.*Point"):
        shapefiles.run(load_config(config_path), overpass_json_path=cached)


def test_resolves_overpass_polygon_superposition_by_configured_precedence(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    config = load_config(config_path)
    raw = {
        "elements": [
            _polygon_way(1, {"building": "yes"}, 11.25560, 43.76940, 11.25600, 43.76980),
            _polygon_way(2, {"building:part": "yes"}, 11.25570, 43.76950, 11.25590, 43.76970),
            _polygon_way(3, {"leisure": "park"}, 11.25585, 43.76960, 11.25610, 43.76990),
        ]
    }
    features, _, _ = shapefiles.overpass_to_features(raw, config)

    resolved, diagnostics = shapefiles._resolve_surface_overlaps(features, config)

    contributing = [feature for feature in resolved if feature["properties"]["contributes_to_geometry"]]
    for first_index, first in enumerate(contributing):
        first_geometry = shapefiles._feature_union_m([first], config)
        for second in contributing[first_index + 1:]:
            second_geometry = shapefiles._feature_union_m([second], config)
            assert first_geometry.intersection(second_geometry).area == pytest.approx(0.0, abs=1.0e-6)
    building_part = next(feature for feature in resolved if feature["properties"]["group_tag"] == "building_part")
    parent_building = next(feature for feature in resolved if feature["properties"]["group_tag"] == "building")
    assert "overlap_clipped" not in building_part["properties"]
    assert parent_building["properties"]["overlap_clipped"] is True
    assert diagnostics["clipped_polygon_features"] == 2
    assert diagnostics["removed_overlap_area_m2"] > 0.0


def test_run_with_cached_overpass_json_writes_expected_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(config_path, output_root=tmp_path / "outputs", name="Fixture")
    raw_path.write_text(json.dumps(_sample_overpass_payload()), encoding="utf-8")

    config = load_config(config_path)
    published: list[Path] = []
    original_publish = shapefiles_publication.publish_stage_manifest

    def observe_publication(**kwargs):
        assert kwargs["report_path"].is_file()
        assert kwargs["preview_path"].is_file()
        published.append(kwargs["output_directory"] / "manifest.json")
        return original_publish(**kwargs)

    monkeypatch.setattr(shapefiles_publication, "publish_stage_manifest", observe_publication)
    result = shapefiles.run(config, overpass_json_path=raw_path)

    assert isinstance(result, StageOutput)
    assert result.accepted_feature_count >= 6
    assert result.tag_inventory_path.exists()
    assert result.diagnostics_path.exists()
    assert result.diagnostics_geojson_path.exists()
    assert result.imagery_diagnostics_path.exists()
    assert result.imagery_overlay_path.exists()
    assert result.all_features_path.exists()
    assert result.region_paths["inner_region"].exists()
    assert result.region_paths["annular_region"].exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["stage"] == "shapefiles"
    assert manifest["status"] == "completed"
    artifacts = {artifact["name"]: artifact for artifact in manifest["artifacts"]}
    assert all(artifact["required"] is True for artifact in artifacts.values())
    assert artifacts["all-features"]["kind"] == "handoff"
    assert artifacts["category-buildings"]["path"] == str(result.category_paths["buildings"])
    assert artifacts["region-inner-region"]["path"] == str(result.region_paths["inner_region"])
    assert artifacts["overpass-raw"]["kind"] == "diagnostic"
    assert artifacts["imagery-overlay"]["kind"] == "diagnostic"
    assert manifest["metrics"] == {
        "raw_element_count": result.raw_element_count,
        "accepted_feature_count": result.accepted_feature_count,
        "skipped_feature_count": result.skipped_feature_count,
    }
    assert result.to_dict() == manifest
    assert published == [result.manifest_path]


    assert result.summary_path.exists()
    assert result.report_path.exists()
    assert result.preview_path.exists()

    all_features = json.loads(result.all_features_path.read_text(encoding="utf-8"))
    categories = [feature["properties"]["category"] for feature in all_features["features"]]
    zones = [feature["properties"]["roi_zone"] for feature in all_features["features"]]
    assert categories[:4] == ["buildings", "buildings", "roads", "trees"]
    assert zones[:4] == ["inner", "annular", "annular", "annular"]
    assert "gap_fill" in categories
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["classification_rules"][0] == {
        "category": "buildings",
        "group_tag": "building",
        "match_any": ["building"],
    }
    assert summary["surface_overlap_diagnostics"]["removed_polygon_features"] == 1
    assert summary["surface_overlap_diagnostics"]["precedence"][0] == "buildings:building_part"
    gap_features = [
        feature
        for feature in all_features["features"]
        if feature["properties"]["category"] == "gap_fill"
    ]
    assert {feature["properties"]["roi_zone"] for feature in gap_features} == {"inner", "annular"}
    assert all(feature["properties"]["source_tag"] == "generated=roi_difference" for feature in gap_features)
    assert all(feature["properties"]["contributes_to_geometry"] is True for feature in gap_features)
    assert all(feature["properties"]["geometry_role"] == "generated_gap_fill_contributing_polygon" for feature in gap_features)
    building_reconstruction_flags = [
        feature["properties"]["include_in_building_lod22_reconstruction"]
        for feature in all_features["features"]
        if feature["properties"]["category"] == "buildings"
    ]
    assert building_reconstruction_flags == [True, False]

    inner_region = json.loads(result.region_paths["inner_region"].read_text(encoding="utf-8"))
    annular_region = json.loads(result.region_paths["annular_region"].read_text(encoding="utf-8"))
    assert len(inner_region["features"]) == 2
    assert len(annular_region["features"]) == 4

    other_terrain = json.loads(result.category_paths["other_terrain"].read_text(encoding="utf-8"))
    assert other_terrain["features"] == []

    tag_inventory = json.loads(result.tag_inventory_path.read_text(encoding="utf-8"))
    assert tag_inventory["tag_value_counts"]["building=yes"] == 2
    assert tag_inventory["unclassified_tag_value_counts"]["shop=books"] == 1


def test_shapefiles_failure_invalidates_stale_completion_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_complete_config(tmp_path / "config.toml", output_root=tmp_path / "outputs")
    manifest_path = tmp_path / "outputs" / "01_shapefiles" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('{"stale": true}', encoding="utf-8")
    monkeypatch.setattr(
        shapefiles,
        "build_tag_inventory_query",
        lambda _config: (_ for _ in ()).throw(ConfigError("forced query failure")),
    )

    with pytest.raises(ConfigError, match="forced query failure"):
        shapefiles.run(load_config(config_path))

    assert not manifest_path.exists()


def test_shapefiles_rejects_concurrent_writer_without_invalidating_manifest(tmp_path: Path) -> None:
    config_path = write_complete_config(tmp_path / "config.toml", output_root=tmp_path / "outputs")
    cached = tmp_path / "overpass.json"
    cached.write_text(json.dumps(_sample_overpass_payload()), encoding="utf-8")
    output_dir = tmp_path / "outputs" / "01_shapefiles"
    output_dir.mkdir(parents=True)
    lock_path = output_dir / ".stage.lock"
    lock_path.write_text("owned by another runner\n", encoding="utf-8")
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text('{"completed": true}', encoding="utf-8")

    with pytest.raises(ConfigError, match="shapefiles output is locked"):
        shapefiles.run(load_config(config_path), overpass_json_path=cached)

    assert manifest_path.read_text(encoding="utf-8") == '{"completed": true}'
    assert lock_path.read_text(encoding="utf-8") == "owned by another runner\n"


def test_interrupted_shapefiles_artifact_publication_preserves_previous_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_complete_config(tmp_path / "config.toml", output_root=tmp_path / "outputs")
    cached = tmp_path / "overpass.json"
    cached.write_text(json.dumps(_sample_overpass_payload()), encoding="utf-8")
    output_dir = tmp_path / "outputs" / "01_shapefiles"
    output_dir.mkdir(parents=True)
    query_path = output_dir / "tag_inventory_query.txt"
    query_path.write_text("previous query\n", encoding="utf-8")
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text('{"completed": true}', encoding="utf-8")
    replace = artifacts.os.replace

    def interrupt_query_publication(source: Path, destination: Path) -> None:
        if Path(destination) == query_path:
            raise RuntimeError("interrupted query publication")
        replace(source, destination)

    monkeypatch.setattr(artifacts.os, "replace", interrupt_query_publication)

    with pytest.raises(RuntimeError, match="interrupted query publication"):
        shapefiles.run(load_config(config_path), overpass_json_path=cached)

    assert query_path.read_text(encoding="utf-8") == "previous query\n"
    assert not manifest_path.exists()
    assert not (output_dir / ".stage.lock").exists()
    assert not list(output_dir.glob(".tag_inventory_query.txt.*.tmp"))


def test_run_routes_urban_planning_points_to_tree_and_purifier_artifacts(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.geojson"
    plan_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [11.2558, 43.7696]},
                        "properties": {
                            "id": "TREE-PLAN-1",
                            "kind": "tree",
                            "model": "large_round_broadleaf",
                            "height_m": 12.0,
                            "street": "Via Roma",
                        },
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [11.2559, 43.7696]},
                        "properties": {
                            "id": "AP-PLAN-1",
                            "kind": "air_purifier",
                            "model": "compact_octagonal_tower",
                            "height_m": 4.2,
                            "width_m": 1.5,
                            "depth_m": 1.4,
                            "rotation_deg": 15.0,
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        air_purifiers_block=f'''[air_purifiers]
model_library_path = "{(ROOT / "docs/assets/air_purifier_towers/parameters.json").as_posix()}"''',
    )
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f'''\n\n[[urban_planning.inputs]]
name = "market_plan"
path = "{plan_path.as_posix()}"
crs = "EPSG:4326"
''',
        encoding="utf-8",
    )
    cached = tmp_path / "overpass.json"
    cached.write_text('{"elements": []}', encoding="utf-8")

    result = shapefiles.run(load_config(config_path), overpass_json_path=cached)

    planning = json.loads(result.urban_planning_path.read_text(encoding="utf-8"))["features"]
    assert [feature["properties"]["id"] for feature in planning] == ["TREE-PLAN-1", "AP-PLAN-1"]
    trees = json.loads(result.category_paths["trees"].read_text(encoding="utf-8"))["features"]
    planned_tree = next(feature for feature in trees if feature["properties"].get("id") == "TREE-PLAN-1")
    assert planned_tree["properties"]["category"] == "trees"
    assert planned_tree["properties"]["direct_model_category"] == "large_round_broadleaf"
    assert planned_tree["properties"]["height_m"] == 12.0
    assert planned_tree["properties"]["tags"] == {}
    purifiers = json.loads(result.air_purifiers_path.read_text(encoding="utf-8"))["features"]
    assert purifiers[0]["properties"]["purifier_id"] == "AP-PLAN-1"
    assert purifiers[0]["properties"]["urban_planning_input_id"] == "market_plan"
    assert "planning_status" not in purifiers[0]["properties"]
    references = json.loads(result.all_features_path.read_text(encoding="utf-8"))["features"]
    non_contributing = json.loads(result.diagnostics_geojson_path.read_text(encoding="utf-8"))["features"]
    assert {"TREE-PLAN-1", "AP-PLAN-1"} <= {feature["properties"].get("id") for feature in references}
    assert {"TREE-PLAN-1", "AP-PLAN-1"} <= {feature["properties"].get("id") for feature in non_contributing}
    assert all(not feature["properties"]["contributes_to_geometry"] for feature in planning)


def test_summary_and_report_identify_outside_roi_records_across_planning_inputs(tmp_path: Path) -> None:
    plan_paths = []
    for name, feature_id, kind, model, coordinates in (
        ("north_plan", "TREE-OUT", "tree", "large_round_broadleaf", [11.2558, 44.0]),
        ("east_plan", "AP-OUT", "air_purifier", "compact_octagonal_tower", [12.0, 43.7696]),
    ):
        path = tmp_path / f"{name}.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": coordinates},
             "properties": {"id": feature_id, "kind": kind, "model": model}}
        ]}), encoding="utf-8")
        plan_paths.append((name, path))
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        air_purifiers_block=f'''[air_purifiers]
model_library_path = "{(ROOT / "docs/assets/air_purifier_towers/parameters.json").as_posix()}"''',
    )
    config_path.write_text(config_path.read_text(encoding="utf-8") + "\n" + "\n".join(
        f'''[[urban_planning.inputs]]
name = "{name}"
path = "{path.as_posix()}"'''
        for name, path in plan_paths
    ), encoding="utf-8")
    cached = tmp_path / "overpass.json"
    cached.write_text('{"elements": []}', encoding="utf-8")

    result = shapefiles.run(load_config(config_path), overpass_json_path=cached)

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    records = summary["urban_planning"]["outside_records"]
    assert [(record["urban_planning_input_id"], record["source_feature_index"], record["id"], record["kind"])
            for record in records] == [
        ("north_plan", 0, "TREE-OUT", "tree"),
        ("east_plan", 0, "AP-OUT", "air_purifier"),
    ]
    assert records[0]["coordinates"] == [11.2558, 44.0]
    assert records[1]["coordinates"] == [12.0, 43.7696]
    assert all(record["roi_distance_m"] > 0 for record in records)
    report = result.report_path.read_text(encoding="utf-8")
    assert "`north_plan` feature 0 (`TREE-OUT`, `tree`)" in report
    assert "`east_plan` feature 0 (`AP-OUT`, `air_purifier`)" in report


def test_preview_report_and_diagnostics_use_normalized_planning_contract(tmp_path: Path) -> None:
    supplemental_trees_path = tmp_path / "municipal_trees.shp"
    _write_point_shapefile(supplemental_trees_path, [(11.2562, 43.7696)])
    plan_path = tmp_path / "mercato.geojson"
    planning_features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [11.2558, 43.7696]},
            "properties": {
                "id": f"TREE-PLAN-{index:02d}",
                "kind": "tree",
                "model": "large_round_broadleaf",
            },
        }
        for index in range(35)
    ]
    planning_features.extend(
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [11.2559, 43.7696]},
            "properties": {
                "id": f"AP-PLAN-{index:02d}",
                "kind": "air_purifier",
                "model": "compact_octagonal_tower",
            },
        }
        for index in range(6)
    )
    plan_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": planning_features}),
        encoding="utf-8",
    )
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        air_purifiers_block=f'''[air_purifiers]
model_library_path = "{(ROOT / "docs/assets/air_purifier_towers/parameters.json").as_posix()}"''',
        shapefiles_block=DEFAULT_SHAPEFILES_BLOCK
        + f'''\n\n[[shapefiles.supplemental]]
name = "municipal_trees"
path = "{supplemental_trees_path.as_posix()}"
crs = "EPSG:4326"
category = "trees"
''',
    )
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f'''\n\n[[urban_planning.inputs]]
name = "mercato_centrale"
path = "{plan_path.as_posix()}"
crs = "EPSG:4326"
''',
        encoding="utf-8",
    )
    cached = tmp_path / "overpass.json"
    cached.write_text(json.dumps(_sample_overpass_payload()), encoding="utf-8")

    result = shapefiles.run(load_config(config_path), overpass_json_path=cached)

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["urban_planning"]["accepted_by_kind"] == {"tree": 35, "air_purifier": 6}
    assert summary["urban_planning"]["inputs"]["mercato_centrale"]["accepted_by_kind"] == {
        "tree": 35,
        "air_purifier": 6,
    }

    report = result.report_path.read_text(encoding="utf-8")
    assert "Supplemental Tree Shapefiles" in report
    assert "Supplemental Surface Shapefiles" in report
    assert "Urban-Planning GeoJSON Inputs" in report
    assert "user shapefile" not in report.lower()

    preview = result.preview_path.read_text(encoding="utf-8")
    assert "Accepted planned trees" in preview
    assert "Accepted air purifiers" in preview
    assert "Accepted existing air purifiers" not in preview
    assert "Accepted planned air purifiers" not in preview
    assert 'data-planning-input="mercato_centrale"' in preview
    assert 'data-toggle-planning-input="mercato_centrale"' in preview
    assert 'data-feature-source="overpass_tree"' in preview
    assert 'data-feature-source="supplemental_tree"' in preview
    assert 'data-feature-source="planned_tree"' in preview
    assert 'data-feature-source="air_purifier"' in preview
    assert 'data-feature-source="existing_air_purifier"' not in preview
    assert 'data-feature-source="planned_air_purifier"' not in preview


def test_run_always_writes_empty_air_purifier_artifact(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    cached = tmp_path / "overpass.json"
    cached.write_text(json.dumps(_sample_overpass_payload()), encoding="utf-8")

    result = shapefiles.run(load_config(config_path), overpass_json_path=cached)

    assert result.air_purifiers_path.name == "air_purifiers.geojson"
    assert json.loads(result.air_purifiers_path.read_text(encoding="utf-8")) == {
        "type": "FeatureCollection",
        "features": [],
    }
    manifest = result.to_dict()
    air_purifiers = next(artifact for artifact in manifest["artifacts"] if artifact["name"] == "air-purifiers")
    assert air_purifiers["path"] == str(result.air_purifiers_path)

    diagnostics = json.loads(result.diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["contributing_feature_count"] == 4
    assert diagnostics["non_contributing_feature_count"] == 2
    assert diagnostics["non_contributing_by_geometry_type"]["LineString"] == 1
    assert diagnostics["non_contributing_by_geometry_type"]["Point"] == 1
    assert diagnostics["generated_gap_fill_feature_count"] == 2
    assert "gap_fill" in diagnostics["gap_fill_policy"]

    non_contributing = json.loads(result.diagnostics_geojson_path.read_text(encoding="utf-8"))
    assert len(non_contributing["features"]) == 2
    assert non_contributing["features"][0]["properties"]["category"] == "roads"

    tree_features = json.loads(result.category_paths["trees"].read_text(encoding="utf-8"))
    assert len(tree_features["features"]) == 1

    report = result.report_path.read_text(encoding="utf-8")
    assert "Feature Retrieval Report" in report
    assert "Available OSM Tag Inventory" in report
    assert "Available OSM Tags Not Classified as Features" in report
    assert "Configured Classification Rules" in report
    assert "Surface Superposition Resolution" in report
    assert "Geometry Contribution Diagnostic" in report
    assert "Counts by Category" in report
    assert "Available Terrain Tags Not Mapped to Core Groups" in report
    assert "Fully covered polygons removed: 1" in report
    assert "Graphical preview" in report
    assert "Imagery diagnostics" in report
    assert "Imagery overlay preview" in report
    assert "annular_region GeoJSON" in report
    assert "Generated gap-fill features" in report

    preview = result.preview_path.read_text(encoding="utf-8")
    assert "Legend and counts" in preview
    assert "Buildings" in preview
    assert "Generated gap-fill surfaces" in preview
    assert "No retrieved surface / possible gap" in preview
    assert "Lines are shown as dashed reference features" in preview
    assert "Zoom in" in preview
    assert "Reset zoom" in preview
    assert "mouse wheel or zoom buttons" in preview
    assert "Tree source QA" in preview
    assert "Surface overlap QA" in preview
    assert "Accepted disjoint polygons" in preview
    assert "Accepted Overpass trees" in preview
    assert "Accepted supplemental trees" in preview
    assert 'data-toggle-category="buildings"' in preview
    assert 'data-feature-category="buildings"' in preview
    assert 'data-toggle-source="overpass_tree"' in preview
    assert "hiddenFeatureCategories" in preview


def test_missing_inner_diameter_uses_uniform_full_region_treatment(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        name="Uniform ROI Fixture",
        inner_diameter_m=None,
    )
    raw_path.write_text(json.dumps(_sample_overpass_payload()), encoding="utf-8")

    result = shapefiles.run(load_config(config_path), overpass_json_path=raw_path)

    assert set(result.region_paths) == {"full_region"}
    assert result.region_paths["full_region"].exists()
    assert not (result.output_directory / "inner_region.geojson").exists()
    assert not (result.output_directory / "annular_region.geojson").exists()

    all_features = json.loads(result.all_features_path.read_text(encoding="utf-8"))
    assert {feature["properties"]["roi_zone"] for feature in all_features["features"]} == {"full"}
    buildings = [
        feature
        for feature in all_features["features"]
        if feature["properties"]["category"] == "buildings"
    ]
    assert len(buildings) == 2
    assert all(feature["properties"]["reconstruction_scope"] == "primary_roi" for feature in buildings)
    assert all(feature["properties"]["include_in_building_lod22_reconstruction"] is True for feature in buildings)

    report = result.report_path.read_text(encoding="utf-8")
    assert "Inner diameter: not set" in report
    assert "full_region GeoJSON" in report
    preview = result.preview_path.read_text(encoding="utf-8")
    assert preview.count("<circle") >= 2
    assert "Removed Overpass duplicates" in preview
    assert "Surface color legend" not in preview

    imagery_diagnostics = json.loads(result.imagery_diagnostics_path.read_text(encoding="utf-8"))
    assert imagery_diagnostics["sources"] == []
    assert "Imagery is diagnostic evidence only" in imagery_diagnostics["assumptions"][0]

    imagery_overlay = result.imagery_overlay_path.read_text(encoding="utf-8")
    assert "imagery diagnostic overlay" in imagery_overlay
    assert "No imagery sources are configured" in imagery_overlay
    assert "No geometry is generated from imagery" in imagery_overlay
    assert "Tree source QA" in imagery_overlay
    assert "mouse wheel or zoom buttons" in imagery_overlay
    assert 'data-toggle-category="green_areas"' in imagery_overlay
    assert "hiddenFeatureSources" in imagery_overlay


def test_live_run_reuses_existing_tag_inventory_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    outputs = tmp_path / "outputs"
    stage_dir = outputs / "01_shapefiles"
    stage_dir.mkdir(parents=True)
    payload = _sample_overpass_payload()
    inventory_cache = stage_dir / "tag_inventory_raw.json"
    inventory_cache.write_text(json.dumps(payload), encoding="utf-8")
    write_complete_config(config_path, output_root=outputs, name="Cached inventory fixture")
    calls: list[str] = []

    def fake_fetch(config, query, overpass_json_path, cached_source_label="cached file"):
        calls.append(query)
        return payload, "mock geometry response"

    monkeypatch.setattr(shapefiles_inputs, "load_or_fetch_overpass", fake_fetch)

    result = shapefiles.run(load_config(config_path))

    assert len(calls) == len(shapefiles.build_overpass_query_batches(load_config(config_path)))
    assert all('(around:' in query for query in calls)
    assert all('nwr(around:' not in query for query in calls)
    assert not list(stage_dir.glob("overpass_raw_batch_*.json"))
    inventory = json.loads(result.tag_inventory_path.read_text(encoding="utf-8"))
    assert inventory["source"] == f"existing tag-inventory cache: {inventory_cache}"


def test_cached_stage_geometry_rerun_preserves_existing_tag_inventory(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    outputs = tmp_path / "outputs"
    stage_dir = outputs / "01_shapefiles"
    stage_dir.mkdir(parents=True)
    geometry_cache = stage_dir / "overpass_raw.json"
    inventory_cache = stage_dir / "tag_inventory_raw.json"
    geometry_cache.write_text(json.dumps(_sample_overpass_payload()), encoding="utf-8")
    inventory_cache.write_text(
        json.dumps({"elements": [{"type": "node", "id": 999, "tags": {"shop": "books"}}]}),
        encoding="utf-8",
    )
    write_complete_config(config_path, output_root=outputs, name="Cached geometry fixture")

    result = shapefiles.run(load_config(config_path), overpass_json_path=geometry_cache)

    inventory = json.loads(result.tag_inventory_path.read_text(encoding="utf-8"))
    assert inventory["source"] == f"existing tag-inventory cache: {inventory_cache}"
    assert inventory["tag_value_counts"] == {"shop=books": 1}
    assert result.manifest.input_state_fingerprint["path_count"] == 1
    assert result.manifest.details["source"].endswith(str(geometry_cache.resolve()))

    rerun = shapefiles.run(load_config(config_path), overpass_json_path=inventory_cache)

    assert rerun.manifest.input_state_fingerprint == result.manifest.input_state_fingerprint
    assert rerun.manifest.details["source"].endswith(str(inventory_cache.resolve()))


def test_shapefiles_fingerprint_canonicalizes_external_overpass_cache_paths(tmp_path: Path) -> None:
    config_path = write_complete_config(tmp_path / "config.toml", output_root=tmp_path / "outputs")
    cached = tmp_path / "overpass.json"
    cached.write_text(json.dumps({"elements": []}), encoding="utf-8")
    relative_cached = Path(os.path.relpath(cached, Path.cwd()))

    absolute_result = shapefiles.run(load_config(config_path), overpass_json_path=cached.resolve())
    relative_result = shapefiles.run(load_config(config_path), overpass_json_path=relative_cached)

    assert relative_result.manifest.input_state_fingerprint == absolute_result.manifest.input_state_fingerprint
    assert relative_result.details["source"].endswith(str(cached.resolve()))


def test_shapefiles_fingerprint_changes_for_effective_supplemental_crs_override(
    tmp_path: Path,
) -> None:
    tree_path = tmp_path / "trees.shp"
    surface_path = tmp_path / "streets.shp"
    tree_path.write_bytes(b"tree fixture")
    surface_path.write_bytes(b"surface fixture")
    config = load_config(
        _config_with_supplements(
            tmp_path,
            tree_path=tree_path,
            surface_path=surface_path,
        )
    )
    overridden = replace(
        config,
        shapefiles=replace(
            config.shapefiles,
            supplemental=tuple(
                replace(item, crs="EPSG:25832") if item.name == "municipal_streets" else item
                for item in config.shapefiles.supplemental
            ),
        ),
    )

    original_fingerprint = shapefiles._shapefiles_input_fingerprint(config, None)
    overridden_fingerprint = shapefiles._shapefiles_input_fingerprint(overridden, None)

    assert overridden_fingerprint != original_fingerprint


def test_imagery_manifest_includes_success_and_wms_error_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        imagery_block='''[[imagery.sources]]
name = "Successful imagery"
type = "wms"
url = "https://example.test/wms"
layer = "ortho"
enabled = true
crs = "EPSG:4326"
format = "image/png"
width = 32
height = 24
transparent = false

[[imagery.sources]]
name = "Error imagery"
type = "wms"
url = "https://example.test/wms"
layer = "ortho"
enabled = true
crs = "EPSG:4326"
format = "image/png"
width = 32
height = 24
transparent = false''',
    )
    cached = tmp_path / "overpass.json"
    cached.write_text(json.dumps({"elements": []}), encoding="utf-8")

    class Response:
        def __init__(self, payload: bytes, content_type: str) -> None:
            self._payload = payload
            self.headers = {"Content-Type": content_type}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self._payload

    responses = iter(
        (
            Response(b"png-bytes", "image/png"),
            Response(b"<ServiceException>broken</ServiceException>", "text/xml"),
        )
    )
    monkeypatch.setattr(shapefiles_inputs.request, "urlopen", lambda *_args, **_kwargs: next(responses))

    result = shapefiles.run(load_config(config_path), overpass_json_path=cached)

    artifacts = {artifact.name: artifact for artifact in result.artifacts}
    imagery_dir = result.output_directory / "imagery"
    assert artifacts["imagery-successful_imagery-1-request"].path == imagery_dir / "successful_imagery_request.url"
    assert artifacts["imagery-successful_imagery-1-request"].kind.value == "diagnostic"
    assert artifacts["imagery-successful_imagery-1-image"].path == imagery_dir / "successful_imagery.png"
    assert artifacts["imagery-successful_imagery-1-image"].kind.value == "supporting"
    assert artifacts["imagery-error_imagery-2-request"].path == imagery_dir / "error_imagery_request.url"
    assert artifacts["imagery-error_imagery-2-error"].path == imagery_dir / "error_imagery_error.txt"
    assert artifacts["imagery-error_imagery-2-error"].kind.value == "diagnostic"
    assert all(artifact.required for artifact in artifacts.values())


def test_imagery_rerun_does_not_advertise_stale_fetched_image_after_wms_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        imagery_block='''[[imagery.sources]]
name = "Rerun imagery"
type = "wms"
url = "https://example.test/wms"
layer = "ortho"
enabled = true
crs = "EPSG:4326"
format = "image/png"
width = 32
height = 24
transparent = false''',
    )
    cached = tmp_path / "overpass.json"
    cached.write_text(json.dumps({"elements": []}), encoding="utf-8")

    class Response:
        def __init__(self, payload: bytes, content_type: str) -> None:
            self._payload = payload
            self.headers = {"Content-Type": content_type}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self._payload

    responses = iter(
        (
            Response(b"png-bytes", "image/png"),
            Response(b"<ServiceException>broken</ServiceException>", "text/xml"),
        )
    )
    monkeypatch.setattr(shapefiles_inputs.request, "urlopen", lambda *_args, **_kwargs: next(responses))

    first = shapefiles.run(load_config(config_path), overpass_json_path=cached)
    second = shapefiles.run(load_config(config_path), overpass_json_path=cached)

    stale_image_path = first.output_directory / "imagery" / "rerun_imagery.png"
    assert stale_image_path.is_file()
    artifacts = {artifact.name: artifact for artifact in second.artifacts}
    assert "imagery-rerun_imagery-1-image" not in artifacts
    assert artifacts["imagery-rerun_imagery-1-request"].path.is_file()
    assert artifacts["imagery-rerun_imagery-1-error"].path.is_file()
    diagnostics = json.loads(second.imagery_diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["sources"][0]["status"] == "error"


def test_run_imports_supplemental_tree_shapefile_points(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        name="Tree Shapefile Fixture",
        inner_diameter_m=300.0,
        outer_diameter_m=500.0,
        shapefiles_extra=f'''[[shapefiles.supplemental]]
name = "municipal_trees"
path = "{(ROOT / "docs/assets/data/florence_opendata/trees_diameter/trees.shp").as_posix()}"
crs = "EPSG:3003"
category = "trees"''',
    )
    raw_path.write_text(json.dumps({"elements": []}), encoding="utf-8")

    result = shapefiles.run(load_config(config_path), overpass_json_path=raw_path)

    tree_features = json.loads(result.category_paths["trees"].read_text(encoding="utf-8"))
    assert len(tree_features["features"]) == 5
    assert {feature["properties"]["source_tag"] for feature in tree_features["features"]} == {"supplemental=municipal_trees"}
    assert {feature["properties"]["source_crs"] for feature in tree_features["features"]} == {"EPSG:3003"}
    assert {feature["properties"]["tags"]["natural"] for feature in tree_features["features"]} == {"tree"}
    first_feature = tree_features["features"][0]
    assert first_feature["properties"]["roi_zone"] == "annular"
    assert first_feature["geometry"]["coordinates"] == pytest.approx([11.257206987, 43.77066914])
    assert "supplemental shapefile municipal_trees" in result.source
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["feature_counts"]["by_category"]["trees"] == 5
    report = result.report_path.read_text(encoding="utf-8")
    assert "supplemental shapefile municipal_trees" in report
    preview = result.preview_path.read_text(encoding="utf-8")
    assert "Individual trees" in preview


def test_run_imports_supplemental_tree_shapefile_dbf_attributes(tmp_path: Path) -> None:
    shapefile_path = tmp_path / "trees.shp"
    _write_point_shapefile(shapefile_path, [(11.2558, 43.7696)])
    _write_tree_dbf(shapefile_path.with_suffix(".dbf"), [{"SPECIE": "Celtis australis", "DBH": 35.0, "CIRCONF_CM": 125.6}])
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        name="Tree Attribute Fixture",
        inner_diameter_m=300.0,
        outer_diameter_m=500.0,
        shapefiles_extra=f'''[[shapefiles.supplemental]]
name = "municipal_trees"
path = "{shapefile_path.as_posix()}"
crs = "EPSG:4326"
category = "trees"''',
    )
    raw_path.write_text(json.dumps({"elements": []}), encoding="utf-8")

    result = shapefiles.run(load_config(config_path), overpass_json_path=raw_path)

    tree_features = json.loads(result.category_paths["trees"].read_text(encoding="utf-8"))
    tags = tree_features["features"][0]["properties"]["tags"]
    assert tags["species"] == "Celtis australis"
    assert tags["dbh"] == 35.0
    assert tags["diameter"] == 0.35
    assert tags["source_circumference"] == 125.6
    assert tags["circumference"] == 1.256
    assert tree_features["features"][0]["properties"]["source_attributes"]["SPECIE"] == "Celtis australis"


def test_run_merges_named_supplemental_tree_shapefiles(tmp_path: Path) -> None:
    existing_path = tmp_path / "existing.shp"
    planned_path = tmp_path / "planned.shp"
    _write_point_shapefile(existing_path, [(11.2558, 43.7696)])
    _write_point_shapefile(planned_path, [(11.2559, 43.7697), (11.2560, 43.7698)])
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    shapefiles_block = f'''[shapefiles]
surface_precedence = [
    "buildings", "water", "green_areas", "roads", "concrete", "other_terrain",
]
[[shapefiles.supplemental]]
name = "inventory"
path = "{existing_path.as_posix()}"
crs = "EPSG:4326"
category = "trees"

[[shapefiles.supplemental]]
name = "second_inventory"
path = "{planned_path.as_posix()}"
crs = "EPSG:4326"
category = "trees"

[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building"
match_any = ["building"]'''
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        shapefiles_block=shapefiles_block,
    )
    raw_path.write_text(json.dumps({"elements": []}), encoding="utf-8")

    result = shapefiles.run(load_config(config_path), overpass_json_path=raw_path)

    tree_features = json.loads(result.category_paths["trees"].read_text(encoding="utf-8"))["features"]
    assert len(tree_features) == 3
    assert [feature["properties"]["supplemental_input_id"] for feature in tree_features] == [
        "inventory",
        "second_inventory",
        "second_inventory",
    ]
    assert all("planning_status" not in feature["properties"] for feature in tree_features)
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["tree_input_diagnostics"]["loaded_features"] == 3
    assert summary["tree_input_diagnostics"]["inputs"]["second_inventory"]["loaded_features"] == 2
    preview = result.preview_path.read_text(encoding="utf-8")
    assert "Accepted supplemental trees</button></th><td>3</td>" in preview
    assert "Accepted planned trees</button></th><td>0</td>" in preview


def test_supplemental_tree_shapefile_removes_overlapping_overpass_trees(tmp_path: Path) -> None:
    shapefile_path = tmp_path / "trees.shp"
    _write_point_shapefile(shapefile_path, [(11.2558, 43.7696)])
    _write_tree_dbf(shapefile_path.with_suffix(".dbf"), [{"SPECIE": "Celtis australis", "DBH": 35.0, "CIRCONF_CM": 125.6}])
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        name="Tree Overlap Fixture",
        inner_diameter_m=300.0,
        outer_diameter_m=500.0,
        tree_overlap_tolerance_m=3.0,
        shapefiles_extra=f'''[[shapefiles.supplemental]]
name = "municipal_trees"
path = "{shapefile_path.as_posix()}"
crs = "EPSG:4326"
category = "trees"''',
    )
    raw_path.write_text(
        json.dumps(
            {
                "elements": [
                    {
                        "type": "node",
                        "id": 101,
                        "lat": 43.7696005,
                        "lon": 11.2558005,
                        "tags": {"natural": "tree"},
                    },
                    {
                        "type": "node",
                        "id": 102,
                        "lat": 43.7705,
                        "lon": 11.2558,
                        "tags": {"natural": "tree"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = shapefiles.run(load_config(config_path), overpass_json_path=raw_path)

    tree_features = json.loads(result.category_paths["trees"].read_text(encoding="utf-8"))
    tree_ids = {feature["properties"]["osm_id"] for feature in tree_features["features"]}
    assert tree_ids == {102, "trees_1"}
    assert {feature["properties"]["source_tag"] for feature in tree_features["features"]} == {
        "natural=tree",
        "supplemental=municipal_trees",
    }
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["tree_overlap_filter"]["removed_overpass_tree_count"] == 1
    assert summary["tree_overlap_filter"]["removed_overpass_tree_ids"] == [101]
    removed_marker = summary["tree_overlap_filter"]["removed_overpass_tree_markers"][0]
    assert removed_marker["osm_id"] == 101
    assert removed_marker["coordinates"] == pytest.approx([11.2558005, 43.7696005])
    assert removed_marker["nearest_supplemental_tree_distance_m"] == pytest.approx(0.069, abs=0.001)
    assert summary["feature_counts"]["skipped_by_reason"]["overpass_tree_overlaps_supplemental_tree"] == 1
    report = result.report_path.read_text(encoding="utf-8")
    assert "Overpass trees removed as supplemental-tree duplicates: 1" in report
    preview = result.preview_path.read_text(encoding="utf-8")
    assert "Accepted Overpass trees</button></th><td>1</td>" in preview
    assert "Accepted supplemental trees</button></th><td>1</td>" in preview
    assert 'data-feature-source="supplemental_tree"' in preview
    assert "Removed Overpass duplicates</button></th><td>1</td>" in preview


def test_supplemental_tree_overlap_check_uses_any_tree_within_radius(tmp_path: Path) -> None:
    shapefile_path = tmp_path / "trees.shp"
    _write_point_shapefile(
        shapefile_path,
        [
            (11.2558, 43.7696),
            (11.2565, 43.7702),
        ],
    )
    _write_tree_dbf(
        shapefile_path.with_suffix(".dbf"),
        [
            {"SPECIE": "Celtis australis", "DBH": 35.0, "CIRCONF_CM": 125.6},
            {"SPECIE": "Tilia", "DBH": 42.0, "CIRCONF_CM": 132.0},
        ],
    )
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        name="Tree Multi Overlap Fixture",
        inner_diameter_m=300.0,
        outer_diameter_m=500.0,
        tree_overlap_tolerance_m=3.0,
        shapefiles_extra=f'''[[shapefiles.supplemental]]
name = "municipal_trees"
path = "{shapefile_path.as_posix()}"
crs = "EPSG:4326"
category = "trees"''',
    )
    raw_path.write_text(
        json.dumps(
            {
                "elements": [
                    {
                        "type": "node",
                        "id": 201,
                        "lat": 43.7702004,
                        "lon": 11.2565004,
                        "tags": {"natural": "tree"},
                    },
                    {
                        "type": "node",
                        "id": 202,
                        "lat": 43.7708,
                        "lon": 11.2558,
                        "tags": {"natural": "tree"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = shapefiles.run(load_config(config_path), overpass_json_path=raw_path)

    tree_features = json.loads(result.category_paths["trees"].read_text(encoding="utf-8"))
    tree_ids = {feature["properties"]["osm_id"] for feature in tree_features["features"]}
    assert tree_ids == {202, "trees_1", "trees_2"}
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["tree_overlap_filter"]["removed_overpass_tree_ids"] == [201]
    removed_marker = summary["tree_overlap_filter"]["removed_overpass_tree_markers"][0]
    assert removed_marker["nearest_supplemental_tree_id"] == "trees_2"
    assert removed_marker["nearest_supplemental_tree_distance_m"] <= 3.0


def test_supplemental_surface_shapefiles_are_clipped_deduplicated_and_source_styled(tmp_path: Path) -> None:
    streets_path = tmp_path / "streets.shp"
    green_path = tmp_path / "green_areas.shp"
    _write_polygon_shapefile(
        streets_path,
        [[
            (11.25575, 43.76950),
            (11.25605, 43.76950),
            (11.25605, 43.76972),
            (11.25575, 43.76972),
            (11.25575, 43.76950),
        ]],
    )
    _write_polygon_shapefile(
        green_path,
        [[
            (11.25592, 43.76956),
            (11.25618, 43.76956),
            (11.25618, 43.76984),
            (11.25592, 43.76984),
            (11.25592, 43.76956),
        ]],
    )
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        name="User Surface Fixture",
        shapefiles_block=f"""
[shapefiles]
surface_precedence = [
    "buildings", "water", "supplemental:green", "green_areas",
    "supplemental:streets", "roads", "concrete", "other_terrain",
]

[[shapefiles.supplemental]]
name = "streets"
path = "{streets_path.as_posix()}"
crs = "EPSG:4326"
category = "roads"
group_tag = "municipal_streets"
enabled = true

[[shapefiles.supplemental]]
name = "green"
path = "{green_path.as_posix()}"
crs = "EPSG:4326"
category = "green_areas"
group_tag = "municipal_green"
enabled = true

[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building"
match_any = ["building"]

[[shapefiles.classification_rules]]
category = "green_areas"
group_tag = "green_area"
match_any = ["leisure=park"]
""".strip(),
    )
    raw_path.write_text(
        json.dumps(
            {
                "elements": [
                    {
                        "type": "way",
                        "id": 1,
                        "tags": {"building": "yes"},
                        "geometry": [
                            {"lon": 11.25570, "lat": 43.76954},
                            {"lon": 11.25584, "lat": 43.76954},
                            {"lon": 11.25584, "lat": 43.76968},
                            {"lon": 11.25570, "lat": 43.76968},
                            {"lon": 11.25570, "lat": 43.76954},
                        ],
                    },
                    {
                        "type": "way",
                        "id": 2,
                        "tags": {"leisure": "park"},
                        "geometry": [
                            {"lon": 11.25585, "lat": 43.76948},
                            {"lon": 11.25624, "lat": 43.76948},
                            {"lon": 11.25624, "lat": 43.76988},
                            {"lon": 11.25585, "lat": 43.76988},
                            {"lon": 11.25585, "lat": 43.76948},
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)
    result = shapefiles.run(config, overpass_json_path=raw_path)

    roads = json.loads(result.category_paths["roads"].read_text(encoding="utf-8"))["features"]
    greens = json.loads(result.category_paths["green_areas"].read_text(encoding="utf-8"))["features"]
    assert {feature["properties"]["source_tag"] for feature in roads} == {"supplemental=streets"}
    assert {feature["properties"]["source_tag"] for feature in greens} == {
        "leisure=park",
        "supplemental=green",
    }
    assert roads[0]["properties"]["overlap_clipped"] is True
    supplemental_green = next(
        feature for feature in greens if feature["properties"]["source_tag"] == "supplemental=green"
    )
    assert "overlap_clipped" not in supplemental_green["properties"]
    street_geometry = shapefiles._feature_union_m(roads, config)
    green_geometry = shapefiles._feature_union_m(greens, config)
    original_street_green_overlap = Point(
        *shapefiles._project_coordinate_m([11.25597, 43.76960], config)
    )
    assert green_geometry.covers(original_street_green_overlap)
    assert not street_geometry.covers(original_street_green_overlap)
    assert street_geometry.intersection(green_geometry).area == pytest.approx(0.0, abs=1.0e-6)

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    surface_diagnostics = summary["surface_input_diagnostics"]
    assert surface_diagnostics["surfaces"]["streets"]["loaded_features"] == 1
    assert surface_diagnostics["surfaces"]["green"]["loaded_features"] == 1
    overlap_diagnostics = summary["surface_overlap_diagnostics"]
    assert overlap_diagnostics["by_supplemental"]["streets"]["accepted_features"] == 1
    assert overlap_diagnostics["by_supplemental"]["green"]["accepted_features"] == 1
    preview = result.preview_path.read_text(encoding="utf-8")
    overlay = result.imagery_overlay_path.read_text(encoding="utf-8")
    assert "Green-area source QA" in preview
    assert "Overpass green areas" in preview
    assert "Supplemental green areas" in preview
    assert 'stroke-dasharray="5 3"' in preview
    assert 'data-toggle-source="overpass_green"' in preview
    assert 'data-toggle-source="supplemental_green"' in preview
    assert 'data-feature-source="supplemental_green"' in preview
    assert "Supplemental surfaces" in preview
    assert 'data-toggle-supplemental-input="streets"' in preview
    assert 'data-toggle-supplemental-input="green"' in preview
    assert 'data-supplemental-input="streets"' in preview
    assert "Green-area source QA" in overlay
    assert "Supplemental surfaces" in overlay
    assert 'data-toggle-supplemental-input="streets"' in overlay
    assert "supplemental shapefile streets" in result.source
    assert "supplemental shapefile green" in result.source


def test_converts_relation_outer_members_to_building_polygon(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(config_path, output_root=tmp_path / "outputs", name="Relation Fixture")
    raw_path.write_text(
        json.dumps(
            {
                "elements": [
                    {
                        "type": "relation",
                        "id": 10,
                        "tags": {"building": "yes", "type": "multipolygon"},
                        "members": [
                            {
                                "type": "way",
                                "ref": 11,
                                "role": "outer",
                                "geometry": [
                                    {"lat": 43.76955, "lon": 11.25575},
                                    {"lat": 43.76955, "lon": 11.25585},
                                    {"lat": 43.76965, "lon": 11.25585},
                                    {"lat": 43.76965, "lon": 11.25575},
                                    {"lat": 43.76955, "lon": 11.25575},
                                ],
                            },
                            {
                                "type": "way",
                                "ref": 12,
                                "role": "inner",
                                "geometry": [
                                    {"lat": 43.76958, "lon": 11.25578},
                                    {"lat": 43.76958, "lon": 11.25582},
                                    {"lat": 43.76962, "lon": 11.25582},
                                    {"lat": 43.76962, "lon": 11.25578},
                                    {"lat": 43.76958, "lon": 11.25578},
                                ],
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = shapefiles.run(load_config(config_path), overpass_json_path=raw_path)

    buildings = json.loads(result.category_paths["buildings"].read_text(encoding="utf-8"))
    assert result.accepted_feature_count >= 1
    assert len(buildings["features"]) == 1
    assert buildings["features"][0]["geometry"]["type"] == "Polygon"
    assert len(buildings["features"][0]["geometry"]["coordinates"]) == 2
    assert buildings["features"][0]["properties"]["osm_type"] == "relation"


def test_classifies_building_part_as_building_feature(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(config_path, output_root=tmp_path / "outputs", name="Building Part Fixture")
    raw_path.write_text(
        json.dumps(
            {
                "elements": [
                    {
                        "type": "way",
                        "id": 30,
                        "tags": {"building:part": "yes"},
                        "geometry": [
                            {"lat": 43.76955, "lon": 11.25575},
                            {"lat": 43.76955, "lon": 11.25585},
                            {"lat": 43.76965, "lon": 11.25585},
                            {"lat": 43.76965, "lon": 11.25575},
                            {"lat": 43.76955, "lon": 11.25575},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = shapefiles.run(load_config(config_path), overpass_json_path=raw_path)

    buildings = json.loads(result.category_paths["buildings"].read_text(encoding="utf-8"))
    assert len(buildings["features"]) == 1
    assert buildings["features"][0]["properties"]["group_tag"] == "building_part"
    assert buildings["features"][0]["properties"]["source_tag"] == "building:part=yes"
    assert buildings["features"][0]["properties"]["building_base_height_m"] == 0.0


def test_resolves_building_roof_base_height_during_shapefiles_stage(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(config_path, output_root=tmp_path / "outputs", name="Roof Base Fixture")
    raw_path.write_text(
        json.dumps(
            {
                "elements": [
                    _polygon_way(40, {"building": "yes"}, 11.25566, 43.76954, 11.25570, 43.76958),
                    _polygon_way(41, {"building": "roof"}, 11.25576, 43.76954, 11.25580, 43.76958),
                    _polygon_way(
                        42,
                        {"building": "roof", "min_height": "3.5"},
                        11.25586,
                        43.76954,
                        11.25590,
                        43.76958,
                    ),
                ]
            }
        ),
        encoding="utf-8",
    )

    result = shapefiles.run(load_config(config_path), overpass_json_path=raw_path)

    buildings = json.loads(result.category_paths["buildings"].read_text(encoding="utf-8"))
    base_heights = {
        feature["properties"]["osm_id"]: feature["properties"]["building_base_height_m"]
        for feature in buildings["features"]
    }
    assert base_heights == {40: 0.0, 41: 2.0, 42: 3.5}


def test_keeps_building_that_intersects_inner_roi_with_centroid_outside(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(config_path, output_root=tmp_path / "outputs", name="Intersecting Building Fixture")
    raw_path.write_text(
        json.dumps(
            {
                "elements": [
                    {
                        "type": "way",
                        "id": 20,
                        "tags": {"building": "yes"},
                        "geometry": [
                            {"lat": 43.770409, "lon": 11.255676},
                            {"lat": 43.770409, "lon": 11.255924},
                            {"lat": 43.770769, "lon": 11.255924},
                            {"lat": 43.770769, "lon": 11.255676},
                            {"lat": 43.770409, "lon": 11.255676},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = shapefiles.run(load_config(config_path), overpass_json_path=raw_path)

    buildings = json.loads(result.category_paths["buildings"].read_text(encoding="utf-8"))
    building = buildings["features"][0]
    assert result.accepted_feature_count >= 1
    assert len(buildings["features"]) == 1
    assert building["properties"]["roi_zone"] == "inner"
    assert building["properties"]["include_in_building_lod22_reconstruction"] is True
    assert building["properties"]["centroid_distance_m"] > 100.0
    assert building["properties"]["roi_distance_m"] < 100.0


def _polygon_way(
    osm_id: int,
    tags: dict[str, str],
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
) -> dict[str, object]:
    return {
        "type": "way",
        "id": osm_id,
        "tags": tags,
        "geometry": [
            {"lon": min_lon, "lat": min_lat},
            {"lon": max_lon, "lat": min_lat},
            {"lon": max_lon, "lat": max_lat},
            {"lon": min_lon, "lat": max_lat},
            {"lon": min_lon, "lat": min_lat},
        ],
    }


def _sample_overpass_payload() -> dict[str, object]:
    return {
        "elements": [
            {
                "type": "way",
                "id": 1,
                "tags": {"building": "yes"},
                "geometry": [
                    {"lat": 43.76955, "lon": 11.25575},
                    {"lat": 43.76955, "lon": 11.25585},
                    {"lat": 43.76965, "lon": 11.25585},
                    {"lat": 43.76965, "lon": 11.25575},
                    {"lat": 43.76955, "lon": 11.25575},
                ],
            },
            {
                "type": "way",
                "id": 2,
                "tags": {"highway": "residential"},
                "geometry": [
                    {"lat": 43.77075, "lon": 11.2557},
                    {"lat": 43.77075, "lon": 11.2559},
                ],
            },
            {
                "type": "node",
                "id": 3,
                "lat": 43.77075,
                "lon": 11.2558,
                "tags": {"natural": "tree"},
            },
            {
                "type": "way",
                "id": 5,
                "tags": {"tourism": "artwork"},
                "geometry": [
                    {"lat": 43.76956, "lon": 11.25576},
                    {"lat": 43.76956, "lon": 11.25578},
                    {"lat": 43.76958, "lon": 11.25578},
                    {"lat": 43.76958, "lon": 11.25576},
                    {"lat": 43.76956, "lon": 11.25576},
                ],
            },
            {
                "type": "way",
                "id": 6,
                "tags": {"shop": "books"},
                "geometry": [
                    {"lat": 43.7695, "lon": 11.2557},
                    {"lat": 43.7695, "lon": 11.2559},
                ],
            },
            {
                "type": "way",
                "id": 4,
                "tags": {"building": "yes"},
                "geometry": [
                    {"lat": 43.77075, "lon": 11.25575},
                    {"lat": 43.77075, "lon": 11.25585},
                    {"lat": 43.77085, "lon": 11.25585},
                    {"lat": 43.77085, "lon": 11.25575},
                    {"lat": 43.77075, "lon": 11.25575},
                ],
            },
        ]
    }


def _write_point_shapefile(path: Path, points: list[tuple[float, float]]) -> None:
    min_x = min(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_x = max(point[0] for point in points)
    max_y = max(point[1] for point in points)
    records = []
    for record_number, (x, y) in enumerate(points, start=1):
        content = struct.pack("<i2d", 1, x, y)
        records.append(struct.pack(">2i", record_number, len(content) // 2) + content)
    file_length_words = (100 + sum(len(record) for record in records)) // 2
    header = struct.pack(">6i", 9994, 0, 0, 0, 0, 0)
    header += struct.pack(">i", file_length_words)
    header += struct.pack("<2i4d4d", 1000, 1, min_x, min_y, max_x, max_y, 0.0, 0.0, 0.0, 0.0)
    path.write_bytes(header + b"".join(records))


def _write_null_then_point_shapefile(path: Path, point: tuple[float, float]) -> None:
    x, y = point
    null_content = struct.pack("<i", 0)
    point_content = struct.pack("<i2d", 1, x, y)
    records = [
        struct.pack(">2i", 1, len(null_content) // 2) + null_content,
        struct.pack(">2i", 2, len(point_content) // 2) + point_content,
    ]
    file_length_words = (100 + sum(len(record) for record in records)) // 2
    header = struct.pack(">6i", 9994, 0, 0, 0, 0, 0)
    header += struct.pack(">i", file_length_words)
    header += struct.pack("<2i4d4d", 1000, 1, x, y, x, y, 0.0, 0.0, 0.0, 0.0)
    path.write_bytes(header + b"".join(records))


def _write_multipoint_shapefile(path: Path, points: list[tuple[float, float]]) -> None:
    min_x = min(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_x = max(point[0] for point in points)
    max_y = max(point[1] for point in points)
    content = struct.pack("<i4di", 8, min_x, min_y, max_x, max_y, len(points))
    content += b"".join(struct.pack("<2d", x, y) for x, y in points)
    record = struct.pack(">2i", 1, len(content) // 2) + content
    file_length_words = (100 + len(record)) // 2
    header = struct.pack(">6i", 9994, 0, 0, 0, 0, 0)
    header += struct.pack(">i", file_length_words)
    header += struct.pack("<2i4d4d", 1000, 8, min_x, min_y, max_x, max_y, 0.0, 0.0, 0.0, 0.0)
    path.write_bytes(header + record)


def _write_polygon_shapefile(path: Path, rings: list[list[tuple[float, float]]]) -> None:
    points = [point for ring in rings for point in ring]
    min_x = min(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_x = max(point[0] for point in points)
    max_y = max(point[1] for point in points)
    part_starts: list[int] = []
    point_count = 0
    for ring in rings:
        part_starts.append(point_count)
        point_count += len(ring)
    content = struct.pack("<i4d2i", 5, min_x, min_y, max_x, max_y, len(rings), point_count)
    content += struct.pack(f"<{len(part_starts)}i", *part_starts)
    content += b"".join(struct.pack("<2d", x, y) for x, y in points)
    record = struct.pack(">2i", 1, len(content) // 2) + content
    file_length_words = (100 + len(record)) // 2
    header = struct.pack(">6i", 9994, 0, 0, 0, 0, 0)
    header += struct.pack(">i", file_length_words)
    header += struct.pack("<2i4d4d", 1000, 5, min_x, min_y, max_x, max_y, 0.0, 0.0, 0.0, 0.0)
    path.write_bytes(header + record)


def _write_tree_dbf(path: Path, rows: list[dict[str, object]]) -> None:
    fields = [("SPECIE", "C", 30, 0), ("DBH", "N", 6, 1), ("CIRCONF_CM", "N", 8, 1)]
    header_length = 32 + len(fields) * 32 + 1
    record_length = 1 + sum(field[2] for field in fields)
    header = bytearray()
    header.extend(bytes([0x03, 126, 7, 6]))
    header.extend(struct.pack("<IHH", len(rows), header_length, record_length))
    header.extend(b"\x00" * 20)
    for name, field_type, field_length, decimals in fields:
        descriptor = bytearray(32)
        descriptor[0:len(name)] = name.encode("ascii")
        descriptor[11] = ord(field_type)
        descriptor[16] = field_length
        descriptor[17] = decimals
        header.extend(descriptor)
    header.append(0x0D)
    records = bytearray()
    for row in rows:
        records.extend(b" ")
        specie = str(row["SPECIE"]).encode("ascii")[:30]
        records.extend(specie.ljust(30, b" "))
        records.extend(f"{float(row['DBH']):6.1f}".encode("ascii"))
        records.extend(f"{float(row['CIRCONF_CM']):8.1f}".encode("ascii"))
    path.write_bytes(bytes(header + records + b"\x1A"))


def _write_air_purifier_dbf(path: Path, rows: list[dict[str, object]]) -> None:
    fields = [
        ("PURIF_ID", "C", 24, 0),
        ("MODEL", "C", 32, 0),
        ("HEIGHT_M", "N", 10, 3),
        ("WIDTH_M", "N", 10, 3),
        ("DEPTH_M", "N", 10, 3),
        ("ROTATION_D", "N", 10, 3),
    ]
    header_length = 32 + len(fields) * 32 + 1
    record_length = 1 + sum(field[2] for field in fields)
    header = bytearray(bytes([0x03, 126, 7, 16]))
    header.extend(struct.pack("<IHH", len(rows), header_length, record_length))
    header.extend(b"\x00" * 20)
    for name, field_type, field_length, decimals in fields:
        descriptor = bytearray(32)
        descriptor[:len(name)] = name.encode("ascii")
        descriptor[11] = ord(field_type)
        descriptor[16] = field_length
        descriptor[17] = decimals
        header.extend(descriptor)
    header.append(0x0D)
    records = bytearray()
    for row in rows:
        records.extend(b" ")
        for name, field_type, field_length, decimals in fields:
            value = row.get(name)
            if value is None:
                encoded = b""
            elif field_type == "C":
                encoded = str(value).encode("ascii")[:field_length]
            else:
                encoded = f"{float(value):.{decimals}f}".encode("ascii")
            records.extend(encoded.ljust(field_length, b" ") if field_type == "C" else encoded.rjust(field_length, b" "))
    path.write_bytes(bytes(header + records + b"\x1A"))
