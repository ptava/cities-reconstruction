from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

import pytest

from cities_reconstruction import artifacts
from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.geometry.stl_regions import REGION_NAMES, mesh_bounds, mesh_edge_counts, read_region_stl
from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    StageOutput,
    StageStatus,
    publish_stage_manifest,
)
from cities_reconstruction.stages import air_purifiers, shapefiles, trees
from tests.config_helpers import DEFAULT_SHAPEFILES_BLOCK, ROOT, write_complete_config
from tests.stage_manifest_helpers import publish_test_stage_manifest

PARAMETERS = ROOT / "docs/assets/air_purifier_towers/parameters.json"
MERCATO_PLAN = (
    ROOT
    / "docs/assets/data/urban_planning/mercato_centrale/urban_plan.geojson"
)


def _catalog_payload() -> dict[str, object]:
    return json.loads(PARAMETERS.read_text(encoding="utf-8"))


def _write_catalog(tmp_path: Path, models: list[dict[str, object]]) -> Path:
    catalog = tmp_path / "catalog.json"
    payload = {"schema_version": 1, "models": models}
    catalog.write_text(json.dumps(payload), encoding="utf-8")
    return catalog


def _relative_model(model: dict[str, object], catalog_dir: Path) -> dict[str, object]:
    result = dict(model)
    source = PARAMETERS.parent / str(result["output_path"])
    result["output_path"] = os.path.relpath(source, catalog_dir)
    return result


def _preview_payload(path: Path) -> dict[str, object]:
    html = path.read_text(encoding="utf-8")
    match = re.search(
        r'<script id="preview-data" type="application/json">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def _config(tmp_path: Path, *, catalog: Path | None = PARAMETERS, terrain: Path | None = None):
    path = tmp_path / "config.toml"
    lines = []
    if catalog is not None:
        lines.append(f'model_library_path = "{catalog.as_posix()}"')
    if terrain is not None:
        lines.append(f'terrain_geometry_path = "{terrain.as_posix()}"')
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        air_purifiers_block="[air_purifiers]\n" + "\n".join(lines),
    )
    return load_config(path)


def _feature(
    purifier_id: str,
    model: str = "compact_octagonal_tower",
    *,
    lon: float = 11.2558,
    lat: float = 43.7696,
    height: float | None = None,
    width: float | None = None,
    depth: float | None = None,
    rotation: float | None = None,
) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "purifier_id": purifier_id,
            "model": model,
            "height_m": height,
            "width_m": width,
            "depth_m": depth,
            "rotation_deg": rotation,
            "urban_planning_input_id": "fixture",
            "roi_zone": "inner",
            "source": "fixture.geojson",
            "source_crs": "EPSG:4326",
            "source_feature_index": 0,
            "source_properties": {"street": "fixture"},
        },
    }


def _write_features(config, features: list[dict[str, object]]) -> None:
    path = config.output.root_directory / "01_shapefiles/air_purifiers.geojson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    publish_test_stage_manifest(
        path.parent,
        stage="shapefiles",
        named_artifacts={"air-purifiers": (path, ArtifactKind.HANDOFF)},
    )


def _write_flat_terrain(path: Path, *, extent: float = 10.0, z: float = 2.0) -> None:
    path.write_text(
        "\n".join(
            (
                f"v {-extent} {-extent} {z}", f"v {extent} {-extent} {z}",
                f"v {extent} {extent} {z}", f"v {-extent} {extent} {z}",
                "f 1 2 3", "f 1 3 4", "",
            )
        ),
        encoding="utf-8",
    )


def test_generates_scaled_aggregate_and_per_unit_surfaces(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_features(config, [
        _feature("AP-001", height=3.6, width=1.35, depth=1.20),
        _feature("AP-002", "compact_four_side_tower"),
    ])

    result = air_purifiers.run(config)

    assert result.purifier_count == 2
    assert set(result.instance_stl_paths) == {"AP-001", "AP-002"}
    combined = read_region_stl(result.combined_stl_path)
    instances = [read_region_stl(path) for path in result.instance_stl_paths.values()]
    assert {name: len(combined[name]) for name in REGION_NAMES} == {
        name: sum(len(mesh[name]) for mesh in instances) for name in REGION_NAMES
    }
    first_bounds = mesh_bounds(instances[0])
    assert first_bounds[1] - first_bounds[0] == pytest.approx(1.35, abs=3e-6)
    assert first_bounds[3] - first_bounds[2] == pytest.approx(1.20, abs=3e-6)
    assert first_bounds[5] - first_bounds[4] == pytest.approx(3.6, abs=3e-6)
    placements = json.loads(result.placement_geojson_path.read_text(encoding="utf-8"))
    assert placements["features"][0]["properties"]["height_source"] == "attribute:HEIGHT_M"
    assert placements["features"][1]["properties"]["height_source"] == "default:compact_four_side_tower"
    assert all(count == 2 for count in mesh_edge_counts(combined).values())


def test_air_purifiers_requires_declared_named_shapefiles_handoff(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_features(config, [_feature("AP-X")])
    stage1_dir = config.output.root_directory / "01_shapefiles"
    manifest = publish_test_stage_manifest(
        stage1_dir,
        stage="shapefiles",
        named_artifacts={"unrelated": (stage1_dir / "air_purifiers.geojson", ArtifactKind.HANDOFF)},
    )

    with pytest.raises(ConfigError, match="air-purifiers") as error:
        air_purifiers.run(config)

    assert str(manifest.manifest_path) in str(error.value)


def test_rotation_local_origin_and_terrain_clearance(tmp_path: Path) -> None:
    terrain = tmp_path / "terrain.obj"
    _write_flat_terrain(terrain)
    config = _config(tmp_path, terrain=terrain)
    _write_features(config, [_feature("AP-ROT", width=2.0, depth=1.0, rotation=450.0)])

    result = air_purifiers.run(config)
    mesh = read_region_stl(result.instance_stl_paths["AP-ROT"])
    bounds = mesh_bounds(mesh)
    assert bounds[1] - bounds[0] == pytest.approx(1.0, abs=3e-6)
    assert bounds[3] - bounds[2] == pytest.approx(2.0, abs=3e-6)
    assert bounds[4] == pytest.approx(1.95)
    payload = json.loads(result.placement_geojson_path.read_text(encoding="utf-8"))
    props = payload["features"][0]["properties"]
    assert props["local_x"] == pytest.approx(0.0, abs=0.02)
    assert props["local_y"] == pytest.approx(0.0, abs=0.02)
    assert props["rotation_deg"] == 90.0
    assert props["terrain_source"] == str(terrain)


def test_z_zero_fallback_and_exact_output_contract(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_features(config, [_feature("AP-001")])
    legacy_manifest = config.output.root_directory / "06_air_purifiers/air_purifier_models_manifest.json"
    legacy_manifest.parent.mkdir(parents=True)
    legacy_manifest.write_text('{"legacy": true}', encoding="utf-8")
    result = air_purifiers.run(config)
    assert mesh_bounds(read_region_stl(result.combined_stl_path))[4] == 0.0
    assert result.manifest_path.name == "manifest.json"
    assert not legacy_manifest.exists()
    assert result.report_path.name == "air_purifier_models_report.md"
    assert result.preview_path.name == "air_purifier_models_preview.html"
    html = result.preview_path.read_text(encoding="utf-8")
    for text in ("#2f80ed", "#eb5757", "#b9c1c9", "Orbit", "Zoom", "Reset", "AP-001", "compact_octagonal_tower"):
        assert text in html


def test_normalized_provenance_is_retained_without_planning_status_outputs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_features(config, [_feature("AP-001")])

    result = air_purifiers.run(config)

    placements = json.loads(result.placement_geojson_path.read_text(encoding="utf-8"))
    properties = placements["features"][0]["properties"]
    assert properties["urban_planning_input_id"] == "fixture"
    assert properties["source"] == "fixture.geojson"
    assert properties["source_crs"] == "EPSG:4326"
    assert properties["source_feature_index"] == 0
    assert properties["source_properties"] == {"street": "fixture"}
    assert properties["roi_zone"] == "inner"
    assert properties["source_coordinates"] == [11.2558, 43.7696]
    assert "planning_status" not in properties

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert isinstance(result, StageOutput)
    assert result.to_dict() == manifest
    assert manifest["schema_version"] == 2
    assert manifest["stage"] == "air-purifiers"
    assert manifest["status"] == "completed"
    assert "statuses" not in manifest["metrics"]
    report = result.report_path.read_text(encoding="utf-8")
    assert "planning status" not in report.lower()
    preview = _preview_payload(result.preview_path)
    assert "status" not in preview["instances"][0]
    html = result.preview_path.read_text(encoding="utf-8")
    assert 'data-model="compact_octagonal_tower"' in html
    assert 'data-instance="AP-001"' in html
    assert "data-status" not in html
    assert "Planning status" not in html


def test_accepts_dotted_normalized_planning_id(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_features(config, [_feature("AP.001")])

    result = air_purifiers.run(config)

    assert set(result.instance_stl_paths) == {"AP.001"}


def test_mercato_fixture_runs_offline_from_stage1_to_z_zero_surfaces(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        name="Mercato Centrale air-purifier integration",
        center_lat=43.77677036533063,
        center_lon=11.253741873814542,
        inner_diameter_m=100.0,
        outer_diameter_m=300.0,
        shapefiles_block=DEFAULT_SHAPEFILES_BLOCK
        + f'''

[[urban_planning.inputs]]
name = "mercato_centrale"
path = "{MERCATO_PLAN.as_posix()}"
crs = "EPSG:4326"
''',
    )
    cached_overpass = tmp_path / "overpass.json"
    cached_overpass.write_text('{"elements": []}', encoding="utf-8")
    config = load_config(config_path)

    stage1 = shapefiles.run(config, overpass_json_path=cached_overpass)
    normalized = json.loads(stage1.air_purifiers_path.read_text(encoding="utf-8"))
    result = air_purifiers.run(config, model_library_path=PARAMETERS)

    assert [
        feature["properties"]["purifier_id"] for feature in normalized["features"]
    ] == [f"AP-{index:03d}" for index in range(1, 8)]
    assert all("planning_status" not in feature["properties"] for feature in normalized["features"])
    assert {
        feature["properties"]["urban_planning_input_id"]
        for feature in normalized["features"]
    } == {"mercato_centrale"}
    assert result.purifier_count == 7
    assert result.model_counts == {
        "compact_four_side_tower": 1,
        "compact_octagonal_tower": 6,
    }
    assert set(result.instance_stl_paths) == {f"AP-{index:03d}" for index in range(1, 8)}
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["metrics"]["purifier_count"] == 7
    assert manifest["metrics"]["model_counts"] == {
        "compact_four_side_tower": 1,
        "compact_octagonal_tower": 6,
    }
    assert manifest["details"]["resolved_overrides"]["terrain_geometry_path"] is None
    assert manifest["details"]["terrain"] == {
        "path": None,
        "status": "z=0 fallback",
        "base_clearance_m": 0.0,
        "footprint_validation": "all four rotated bounding-box corners",
    }
    placements = json.loads(result.placement_geojson_path.read_text(encoding="utf-8"))
    assert {feature["properties"]["base_z"] for feature in placements["features"]} == {0.0}
    combined = read_region_stl(result.combined_stl_path)
    instances = [read_region_stl(path) for path in result.instance_stl_paths.values()]
    assert {region: len(combined[region]) for region in REGION_NAMES} == {
        region: sum(len(mesh[region]) for mesh in instances) for region in REGION_NAMES
    }


def test_mixed_crs_planning_runs_from_stage1_through_both_model_stages(tmp_path: Path) -> None:
    lon, lat = 11.2558, 43.7696
    mercator_x = 6_378_137.0 * math.radians(lon)
    mercator_y = 6_378_137.0 * math.log(
        math.tan(math.pi / 4.0 + math.radians(lat) / 2.0)
    )
    tree_plan = tmp_path / "trees-4326.geojson"
    tree_plan.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [lon, lat]},
                        "properties": {
                            "id": "TREE-4326",
                            "kind": "tree",
                            "model": "small_round_broadleaf",
                            "height_m": 9.0,
                            "crown_diameter_m": 4.0,
                            "label": "portable tree",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    purifier_plan = tmp_path / "purifiers-3857.geojson"
    purifier_plan.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [mercator_x, mercator_y],
                        },
                        "properties": {
                            "id": "AP-3857",
                            "kind": "air_purifier",
                            "model": "compact_octagonal_tower",
                            "height_m": 4.2,
                            "width_m": 1.4,
                            "depth_m": 1.2,
                            "rotation_deg": 15.0,
                            "notes": "Web Mercator source",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.toml"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        air_purifiers_block=f'''[air_purifiers]
model_library_path = "{PARAMETERS.as_posix()}"''',
        shapefiles_block=DEFAULT_SHAPEFILES_BLOCK
        + f'''

[[urban_planning.inputs]]
name = "portable_trees"
path = "{tree_plan.as_posix()}"
crs = "EPSG:4326"

[[urban_planning.inputs]]
name = "web_mercator_purifiers"
path = "{purifier_plan.as_posix()}"
crs = "EPSG:3857"
''',
    )
    cached_overpass = tmp_path / "overpass.json"
    cached_overpass.write_text('{"elements": []}', encoding="utf-8")
    config = load_config(config_path)

    stage1 = shapefiles.run(config, overpass_json_path=cached_overpass)
    tree_result = trees.run(config)
    purifier_result = air_purifiers.run(config)

    planning = json.loads(stage1.urban_planning_path.read_text(encoding="utf-8"))["features"]
    assert [feature["properties"]["id"] for feature in planning] == ["TREE-4326", "AP-3857"]
    assert {feature["properties"]["urban_planning_input_id"] for feature in planning} == {
        "portable_trees",
        "web_mercator_purifiers",
    }
    assert planning[0]["properties"]["source_properties"] == {"label": "portable tree"}
    assert planning[1]["properties"]["source_crs"] == "EPSG:3857"
    assert planning[1]["properties"]["source_properties"] == {
        "notes": "Web Mercator source"
    }
    assert planning[1]["geometry"]["coordinates"] == pytest.approx([lon, lat])
    assert all("planning_status" not in feature["properties"] for feature in planning)

    tree_placements = json.loads(
        tree_result.placement_geojson_path.read_text(encoding="utf-8")
    )["features"]
    assert tree_result.tree_count == 1
    assert tree_placements[0]["properties"]["tree_id"] == "TREE-4326"
    assert tree_placements[0]["properties"]["model_category"] == "small_round_broadleaf"
    assert tree_placements[0]["properties"]["model_source"] == "urban_planning:model"
    assert tree_placements[0]["properties"]["height_m"] == 9.0
    assert tree_placements[0]["properties"]["crown_radius_m"] == 2.0
    assert tree_placements[0]["properties"]["crown_radius_source"] == (
        "urban_planning:crown_diameter_m"
    )
    assert tree_placements[0]["properties"]["trunk_radius_m"] == 0.068
    assert tree_placements[0]["properties"]["trunk_radius_source"] == (
        "default:small_round_broadleaf.trunk_radius_m"
    )

    purifier_placements = json.loads(
        purifier_result.placement_geojson_path.read_text(encoding="utf-8")
    )["features"]
    purifier_properties = purifier_placements[0]["properties"]
    assert purifier_result.purifier_count == 1
    assert purifier_properties["purifier_id"] == "AP-3857"
    assert purifier_properties["urban_planning_input_id"] == "web_mercator_purifiers"
    assert purifier_properties["source_crs"] == "EPSG:3857"
    assert purifier_properties["source_properties"] == {"notes": "Web Mercator source"}
    assert purifier_properties["height_m"] == 4.2
    assert purifier_properties["width_m"] == 1.4
    assert purifier_properties["depth_m"] == 1.2
    assert purifier_properties["rotation_deg"] == 15.0
    assert purifier_properties["height_source"] == "attribute:HEIGHT_M"
    assert purifier_properties["width_source"] == "attribute:WIDTH_M"
    assert purifier_properties["depth_source"] == "attribute:DEPTH_M"
    assert purifier_properties["rotation_source"] == "attribute:ROTATION_D"
    assert "planning_status" not in purifier_properties
    regions = read_region_stl(purifier_result.instance_stl_paths["AP-3857"])
    assert tuple(regions) == REGION_NAMES
    assert {region: len(regions[region]) for region in REGION_NAMES} == {
        "inlet": 16,
        "outlet": 8,
        "tower": 72,
    }


def test_custom_model_catalog_supplied_only_to_placement_runs_from_stage1(tmp_path: Path) -> None:
    purifier_path = tmp_path / "custom_purifier.geojson"
    purifier_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [11.2558, 43.7696]},
                        "properties": {
                            "id": "AP-CUSTOM",
                            "kind": "air_purifier",
                            "model": "custom_market_tower",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    model = _relative_model(dict(_catalog_payload()["models"][0]), tmp_path)
    model["name"] = "custom_market_tower"
    catalog = _write_catalog(tmp_path, [model])
    config_path = tmp_path / "config.toml"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        shapefiles_block=DEFAULT_SHAPEFILES_BLOCK
        + f'''

[[urban_planning.inputs]]
name = "custom_plan"
path = "{purifier_path.as_posix()}"
crs = "EPSG:4326"
''',
    )
    cached_overpass = tmp_path / "overpass.json"
    cached_overpass.write_text('{"elements": []}', encoding="utf-8")
    config = load_config(config_path)

    stage1 = shapefiles.run(config, overpass_json_path=cached_overpass)
    normalized = json.loads(stage1.air_purifiers_path.read_text(encoding="utf-8"))
    result = air_purifiers.run(config, model_library_path=catalog)

    assert normalized["features"][0]["properties"]["model"] == "custom_market_tower"
    assert result.model_counts == {"custom_market_tower": 1}
    assert read_region_stl(result.instance_stl_paths["AP-CUSTOM"])


def test_preview_contains_transformed_region_geometry_and_fitted_scene(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_features(
        config,
        [
            _feature("AP-NEAR", width=2.0, depth=1.0, height=6.0, rotation=90.0),
            _feature("AP-FAR", "compact_four_side_tower", lon=11.2658, lat=43.7796),
        ],
    )

    result = air_purifiers.run(config)

    preview = _preview_payload(result.preview_path)
    assert preview["patch_colours"] == {
        "inlet": "#2f80ed",
        "outlet": "#eb5757",
        "tower": "#b9c1c9",
    }
    scene = preview["scene"]
    assert scene["radius"] > 100.0
    assert scene["centre"] != [0.0, 0.0, 0.0]
    assert scene["default_scale"] * scene["radius"] <= 620 * 0.45
    by_id = {item["id"]: item for item in preview["instances"]}
    near = by_id["AP-NEAR"]
    assert tuple(near["regions"]) == REGION_NAMES
    preview_points = [
        point
        for region in REGION_NAMES
        for triangle in near["regions"][region]
        for point in triangle
    ]
    preview_bounds = (
        min(point[0] for point in preview_points), max(point[0] for point in preview_points),
        min(point[1] for point in preview_points), max(point[1] for point in preview_points),
        min(point[2] for point in preview_points), max(point[2] for point in preview_points),
    )
    assert preview_bounds == pytest.approx(mesh_bounds(read_region_stl(result.instance_stl_paths["AP-NEAR"])))
    assert preview_bounds[1] - preview_bounds[0] == pytest.approx(1.0, abs=3e-6)
    assert preview_bounds[3] - preview_bounds[2] == pytest.approx(2.0, abs=3e-6)
    assert preview_bounds[5] - preview_bounds[4] == pytest.approx(6.0, abs=3e-6)
    html = result.preview_path.read_text(encoding="utf-8")
    assert 'data-model="compact_octagonal_tower"' in html
    assert "data-status" not in html
    assert 'data-instance="AP-NEAR"' in html
    assert "function resetView()" in html
    assert "camera.scale=preview.scene.default_scale" in html


@pytest.mark.parametrize(
    ("features", "message"),
    [
        ([], "no air-purifier features to generate"),
        ([_feature("AP-X", "missing")], "unknown air-purifier model"),
        ([_feature("AP-X"), _feature("AP-X")], "duplicate air-purifier ID"),
    ],
)
def test_rejects_invalid_feature_sets(tmp_path: Path, features, message: str) -> None:
    config = _config(tmp_path)
    _write_features(config, features)
    with pytest.raises(ConfigError, match=message):
        air_purifiers.run(config)


def test_validates_rotated_footprint_and_stage3_manifest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    terrain = config.output.root_directory / "04_city_models/terrain.obj"
    terrain.parent.mkdir(parents=True)
    _write_flat_terrain(terrain, extent=0.6)
    _write_features(config, [_feature("AP-X", width=1.0, depth=1.0, rotation=45)])
    with pytest.raises(ConfigError, match="manifest is missing"):
        air_purifiers.run(config, terrain_geometry_path=terrain)
    publish_stage_manifest(
        stage="city-models",
        status=StageStatus.COMPLETED,
        output_directory=terrain.parent,
        report_path=terrain.parent / "city_models_report.md",
        preview_path=terrain.parent / "city_models_preview.html",
        input_state_fingerprint={"fixture": "completed-city-models"},
        artifacts=(ArtifactReference("terrain", terrain, ArtifactKind.HANDOFF),),
        metrics={},
        details={},
    )
    with pytest.raises(ConfigError, match="footprint.*terrain"):
        air_purifiers.run(config, terrain_geometry_path=terrain)


def test_rerun_is_deterministic_and_removes_only_manifest_allowlisted_instances(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_features(config, [_feature("AP-001"), _feature("AP-OLD", lon=11.2559)])
    first = air_purifiers.run(config)
    unrelated = first.instance_stl_paths["AP-OLD"].parent / "keep-me.stl"
    unrelated.write_text("user data", encoding="utf-8")
    _write_features(config, [_feature("AP-001")])
    second = air_purifiers.run(config)
    first_text = second.combined_stl_path.read_text(encoding="ascii")
    air_purifiers.run(config)
    assert second.combined_stl_path.read_text(encoding="ascii") == first_text
    assert not first.instance_stl_paths["AP-OLD"].exists()
    assert unrelated.read_text(encoding="utf-8") == "user data"


def test_rejects_malformed_catalog_and_source_mesh(tmp_path: Path) -> None:
    bad_catalog = tmp_path / "catalog.json"
    bad_catalog.write_text(json.dumps({"schema_version": 2, "models": []}), encoding="utf-8")
    config = _config(tmp_path, catalog=bad_catalog)
    _write_features(config, [_feature("AP-X")])
    with pytest.raises(ConfigError, match="schema_version"):
        air_purifiers.run(config)

    malformed_stl = tmp_path / "broken.stl"
    malformed_stl.write_text("solid inlet\nendsolid inlet\n", encoding="ascii")
    bad_catalog.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "models": [
                    {
                        "name": "broken",
                        "kind": "four_side",
                        "output_path": malformed_stl.name,
                        "height_m": 4.0,
                        "width_m": 1.5,
                        "depth_m": 1.5,
                        "linear_tolerance_m": 0.002,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_features(config, [_feature("AP-X", "broken")])
    with pytest.raises(ConfigError, match="exactly the solids|empty solid"):
        air_purifiers.run(config)


@pytest.mark.parametrize(
    ("catalog_case", "message"),
    [
        ("duplicate", "duplicate air-purifier model name"),
        ("unknown_kind", "unknown air-purifier catalog kind"),
        ("missing_source", "readable ASCII STL"),
        ("bounds_mismatch", "do not match catalog dimensions"),
        ("absolute_output", "relative output_path"),
    ],
)
def test_rejects_catalog_contract_errors(tmp_path: Path, catalog_case: str, message: str) -> None:
    raw_models = _catalog_payload()["models"]
    models = [_relative_model(dict(item), tmp_path) for item in raw_models]
    if catalog_case == "duplicate":
        models[1]["name"] = models[0]["name"]
    elif catalog_case == "unknown_kind":
        models[0]["kind"] = "cylindrical"
    elif catalog_case == "missing_source":
        models[0]["output_path"] = "models/does-not-exist.stl"
    elif catalog_case == "bounds_mismatch":
        models[0]["height_m"] = 99.0
    elif catalog_case == "absolute_output":
        models[0]["output_path"] = str(
            (PARAMETERS.parent / str(raw_models[0]["output_path"])).resolve()
        )
    catalog = _write_catalog(tmp_path, models)
    config = _config(tmp_path, catalog=catalog)
    _write_features(config, [_feature("AP-X", str(models[0]["name"]))])

    with pytest.raises(ConfigError, match=message):
        air_purifiers.run(config)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("height_m", -1.0, "height_m must be positive"),
        ("width_m", math.inf, "width_m must be a finite number"),
        ("depth_m", "wide", "depth_m must be a finite number"),
        ("rotation_deg", math.nan, "rotation_deg.*finite number"),
    ],
)
def test_rejects_invalid_target_values(tmp_path: Path, key: str, value: object, message: str) -> None:
    config = _config(tmp_path)
    feature = _feature("AP-X")
    feature["properties"][key] = value
    _write_features(config, [feature])

    with pytest.raises(ConfigError, match=message):
        air_purifiers.run(config)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("urban_planning_input_id", None, "urban_planning_input_id"),
        ("urban_planning_input_id", {"bad": "value"}, "urban_planning_input_id"),
        ("roi_zone", "outside", "roi_zone.*inner.*annular.*full"),
        ("source", None, "source"),
        ("source_crs", [], "source_crs"),
        ("source_feature_index", -1, "source_feature_index.*non-negative integer"),
        ("source_properties", [], "source_properties.*object"),
    ],
)
def test_rejects_invalid_normalized_metadata(
    tmp_path: Path, key: str, value: object, message: str
) -> None:
    config = _config(tmp_path)
    feature = _feature("AP-X")
    feature["properties"][key] = value
    _write_features(config, [feature])

    with pytest.raises(ConfigError, match=message):
        air_purifiers.run(config)


def test_placement_preserves_original_source_coordinates(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_features(config, [_feature("AP-X", lon=11.25123, lat=43.77123)])

    result = air_purifiers.run(config)

    properties = json.loads(result.placement_geojson_path.read_text(encoding="utf-8"))["features"][0]["properties"]
    assert properties["source_coordinates"] == [11.25123, 43.77123]
    assert properties["source_coordinate_crs"] == "EPSG:4326"


def test_lock_rejection_and_failure_leave_completion_manifest_absent(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _write_features(config, [_feature("AP-X")])
    result = air_purifiers.run(config)
    output_dir = result.output_directory
    lock_path = output_dir / ".stage.lock"
    lock_path.write_text("held", encoding="utf-8")
    with pytest.raises(ConfigError, match="air-purifiers output is locked"):
        air_purifiers.run(config)
    lock_path.unlink()

    def fail_report(*args, **kwargs):
        raise ConfigError("forced report failure")

    monkeypatch.setattr(air_purifiers, "_render_report", fail_report)
    with pytest.raises(ConfigError, match="forced report failure"):
        air_purifiers.run(config)
    assert not result.manifest_path.exists()
    assert not lock_path.exists()


@pytest.mark.parametrize("publication", ["instance", "aggregate"])
def test_failed_stl_publication_preserves_prior_complete_artifact(
    tmp_path: Path,
    monkeypatch,
    publication: str,
) -> None:
    config = _config(tmp_path)
    _write_features(config, [_feature("AP-X")])
    result = air_purifiers.run(config)
    target = (
        result.instance_stl_paths["AP-X"]
        if publication == "instance"
        else result.combined_stl_path
    )
    prior_bytes = target.read_bytes()
    real_replace = artifacts.os.replace

    def fail_target_replace(source: Path | str, destination: Path | str) -> None:
        if Path(destination) == target:
            raise OSError(f"forced {publication} publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(artifacts.os, "replace", fail_target_replace)

    with pytest.raises(OSError, match=f"forced {publication} publication failure"):
        air_purifiers.run(config)

    assert target.read_bytes() == prior_bytes
    assert not result.manifest_path.exists()
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


def test_manifest_is_written_last_and_report_contains_diagnostics(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    first = _feature("AP-1", height=5.0)
    second = _feature("AP-2", "compact_four_side_tower", lon=11.2559)
    second["properties"]["urban_planning_input_id"] = "second-source"
    _write_features(config, [first, second])
    published: list[Path] = []
    original_publish = air_purifiers.publish_stage_manifest

    def observe_publication(**kwargs):
        assert kwargs["preview_path"].is_file()
        assert kwargs["report_path"].is_file()
        published.append(kwargs["output_directory"] / "manifest.json")
        return original_publish(**kwargs)

    monkeypatch.setattr(air_purifiers, "publish_stage_manifest", observe_publication)

    result = air_purifiers.run(config)

    assert published == [result.manifest_path]
    report = result.report_path.read_text(encoding="utf-8")
    assert "Counts by model" in report
    assert "Counts by input" in report
    assert "`fixture`: 1" in report
    assert "`second-source`: 1" in report
    assert "planning status" not in report.lower()
    assert "Parameter provenance" in report
    assert "attribute:HEIGHT_M" in report
    assert "default:compact_four_side_tower" in report


def test_manifest_lists_purifier_handoffs_and_supporting_placement(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_features(config, [_feature("AP-001")])

    result = air_purifiers.run(config)

    artifacts_by_name = {artifact.name: artifact for artifact in result.artifacts}
    assert artifacts_by_name["combined-surface"].kind is ArtifactKind.HANDOFF
    assert artifacts_by_name["combined-surface"].path == result.combined_stl_path
    assert artifacts_by_name["instance-AP-001"].kind is ArtifactKind.HANDOFF
    assert artifacts_by_name["instance-AP-001"].path == result.instance_stl_paths["AP-001"]
    assert artifacts_by_name["placements"].kind is ArtifactKind.SUPPORTING
    assert all(artifact.required is True for artifact in artifacts_by_name.values())
    assert result.metrics["purifier_count"] == 1
    assert result.metrics["model_counts"] == {"compact_octagonal_tower": 1}
    assert result.details["terrain"]["status"] == "z=0 fallback"


def test_air_purifier_terrain_errors_use_stage_specific_wording(tmp_path: Path) -> None:
    config = _config(tmp_path)
    terrain = config.output.root_directory / "04_city_models/terrain.obj"
    terrain.parent.mkdir(parents=True)
    _write_flat_terrain(terrain)
    _write_features(config, [_feature("AP-X")])

    with pytest.raises(ConfigError) as error:
        air_purifiers.run(config, terrain_geometry_path=terrain)

    assert "configured air-purifier terrain" in str(error.value)
    assert "tree" not in str(error.value)
