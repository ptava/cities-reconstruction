from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.geometry.terrain import (
    load_terrain_sampler,
    validate_completed_city_models_terrain,
)
from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    StageOutput,
    StageStatus,
    publish_stage_manifest,
)
from cities_reconstruction.stages.trees import stage as trees
from tests.config_helpers import write_complete_config
from tests.stage_manifest_helpers import publish_test_stage_manifest


def test_generates_parametric_tree_stls_and_manifest(tmp_path: Path) -> None:
    config_path = _prepare_tree_fixture(tmp_path)
    legacy_manifest = tmp_path / "outputs/05_trees/tree_models_manifest.json"
    legacy_manifest.parent.mkdir(parents=True)
    legacy_manifest.write_text('{"legacy": true}', encoding="utf-8")

    result = trees.run(load_config(config_path))

    assert result.tree_count == 4
    assert result.species_counts == {"Celtis australis": 1, "Citrus spp.": 1, "Tilia": 2}
    assert result.trunks_stl_path.exists()
    assert result.crowns_stl_path.exists()
    assert result.combined_stl_path.exists()
    assert (result.surfaces_directory / "species_crowns" / "celtis_australis_crowns.stl").exists()
    assert (result.surfaces_directory / "species_crowns" / "citrus_spp_crowns.stl").exists()
    assert "facet normal" in result.combined_stl_path.read_text(encoding="utf-8")
    combined_vertices = _stl_vertices(result.combined_stl_path)
    assert max(abs(vertex[0]) for vertex in combined_vertices) < 100.0
    assert max(abs(vertex[1]) for vertex in combined_vertices) < 100.0
    placements = json.loads(result.placement_geojson_path.read_text(encoding="utf-8"))
    assert len(placements["features"]) == 4
    tilia = placements["features"][1]["properties"]
    assert tilia["species_model"] == "large_round_broadleaf"
    assert tilia["species"] == "Tilia"
    assert tilia["model_category"] == "large_round_broadleaf"
    assert tilia["height_m"] == 16.0
    assert tilia["crown_radius_m"] == 3.0
    assert tilia["projected_crs"] == "EPSG:25832"
    assert tilia["model_source"] == "tag:genus:species_category_mapping"
    assert tilia["height_source"] == "tag:height"
    assert tilia["crown_radius_source"] == "tag:crown:diameter"
    assert tilia["trunk_radius_source"] == "tag:diameter"
    citrus = placements["features"][2]["properties"]
    assert citrus["species"] == "Citrus spp."
    assert citrus["species_model"] == "small_round_broadleaf"
    assert citrus["model_category"] == "small_round_broadleaf"
    assert citrus["model_source"] == "tag:species:species_category_mapping"
    assert "species_model" not in citrus["defaulted_fields"]
    fallback = placements["features"][3]["properties"]
    assert fallback["species"] == "Tilia"
    assert fallback["source_species"] is None
    assert fallback["species_model"] == "large_round_broadleaf"
    assert fallback["model_source"] == "default:Tilia:species_category_mapping"
    assert "species_model" in fallback["defaulted_fields"]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.manifest_path == result.output_directory / "manifest.json"
    assert not legacy_manifest.exists()
    assert isinstance(result, StageOutput)
    assert result.to_dict() == manifest
    assert manifest["schema_version"] == 2
    assert manifest["stage"] == "trees"
    assert manifest["status"] == "completed"
    assert manifest["metrics"]["tree_count"] == 4
    assert manifest["metrics"]["species_counts"] == {"Celtis australis": 1, "Citrus spp.": 1, "Tilia": 2}
    assert manifest["details"]["surface_frame"]["name"] == "city4cfd_local_origin"
    information = manifest["details"]["information_summary"]
    assert information["trees_with_any_model_input_tags_or_allometry"] == 3
    assert information["trees_with_species_tag_model"] == 3
    assert information["trees_with_fallback_species_model"] == 1
    assert information["fallback_model_count"] == 1
    assert (
        information["trees_with_species_tag_model"]
        + information["trees_with_fallback_species_model"]
        == manifest["metrics"]["tree_count"]
    )
    assert manifest["details"]["fallback"]["default_species"] == "Tilia"
    assert manifest["details"]["fallback"]["tree_count"] == 1
    assert manifest["details"]["surfaces"]["species_crowns"]["Citrus spp."].endswith("citrus_spp_crowns.stl")
    assert manifest["details"]["tree_information"][2]["model_source"] == "tag:species:species_category_mapping"
    assert manifest["details"]["tree_information"][3]["model_source"] == "default:Tilia:species_category_mapping"
    artifacts = {artifact["name"]: artifact for artifact in manifest["artifacts"]}
    assert artifacts["trees-combined-surface"] == {
        "name": "trees-combined-surface", "path": str(result.combined_stl_path), "kind": "handoff", "required": True,
    }
    assert artifacts["tree-trunks-surface"]["kind"] == ArtifactKind.HANDOFF
    assert artifacts["tree-crowns-surface"]["kind"] == ArtifactKind.HANDOFF
    assert artifacts["tree-placements"]["kind"] == ArtifactKind.SUPPORTING
    assert artifacts["species-library"]["kind"] == ArtifactKind.SUPPORTING
    assert any(name.startswith("species-crown-") for name in artifacts)
    assert all(artifact["required"] is True for artifact in artifacts.values())
    preview = result.preview_path.read_text(encoding="utf-8")
    assert "<canvas" in preview
    assert "parametric tree models" in preview
    assert "same local origin used by the City4CFD handoff" in preview
    assert "Zoom in" in preview
    assert "Reset zoom" in preview
    assert "mouse wheel or zoom buttons" in preview
    assert "Species-tag models" in preview
    assert "Fallback species models" in preview
    assert "Named Trees" in preview
    assert "Citrus spp." in preview
    report = result.report_path.read_text(encoding="utf-8")
    assert "Tree Model Generation Report" in report
    assert "Trees reconstructed from species tags: 3 / 4" in report
    assert "Trees reconstructed with configured fallback species model (Tilia): 1 / 4" in report
    assert "Trees with any usable source information or allometry: 3 / 4" in report
    assert "Per-Tree Model Inputs" in report
    assert "Species crown STL surfaces" in report
    assert "STL surface frame: local City4CFD origin" in report


def test_tree_stage_uses_species_category_mapping_and_library(tmp_path: Path) -> None:
    model_library_path = tmp_path / "tree_categories.json"
    model_library_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "name": "large_round_broadleaf",
                        "aliases": ["large round broadleaf"],
                        "default_height_m": 16.0,
                        "default_crown_radius_m": 5.2,
                        "default_trunk_radius_m": 0.14,
                        "crown_base_fraction": 0.34,
                        "crown_shape": "ellipsoid",
                    },
                    {
                        "name": "small_round_broadleaf",
                        "aliases": ["small round broadleaf"],
                        "default_height_m": 8.5,
                        "default_crown_radius_m": 2.8,
                        "default_trunk_radius_m": 0.07,
                        "crown_base_fraction": 0.36,
                        "crown_shape": "rounded",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    category_mapping_path = tmp_path / "species_category_mapping.json"
    category_mapping_path.write_text(
        json.dumps(
            {
                    "species_to_category": {
                        "Celtis australis": "large_round_broadleaf",
                        "Citrus spp.": "small_round_broadleaf",
                        "Tilia": "large_round_broadleaf",
                    }
                }
        ),
        encoding="utf-8",
    )
    config_path = _prepare_tree_fixture(
        tmp_path,
        model_library_path=model_library_path,
        category_mapping_path=category_mapping_path,
    )

    result = trees.run(load_config(config_path))

    placements = json.loads(result.placement_geojson_path.read_text(encoding="utf-8"))
    celtis = placements["features"][0]["properties"]
    citrus = placements["features"][2]["properties"]
    assert celtis["species"] == "Celtis australis"
    assert celtis["model_category"] == "large_round_broadleaf"
    assert celtis["model_source"] == "tag:species:species_category_mapping"
    assert citrus["species"] == "Citrus spp."
    assert citrus["species_model"] == "small_round_broadleaf"
    assert citrus["model_category"] == "small_round_broadleaf"
    assert citrus["crown_shape"] == "rounded"
    assert citrus["model_source"] == "tag:species:species_category_mapping"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["metrics"]["category_counts"]["large_round_broadleaf"] == 3
    assert manifest["metrics"]["category_counts"]["small_round_broadleaf"] == 1
    assert manifest["metrics"]["species_counts"]["Citrus spp."] == 1


def test_species_slug_collisions_produce_unique_deterministic_crown_handoffs(
    tmp_path: Path,
) -> None:
    mapping_path = tmp_path / "species_category_mapping.json"
    mapping_path.write_text(
        json.dumps(
            {
                "species_to_category": {
                    "A b": "large_round_broadleaf",
                    "A-b": "large_round_broadleaf",
                    "Tilia": "large_round_broadleaf",
                }
            }
        ),
        encoding="utf-8",
    )
    config_path = _prepare_tree_fixture(tmp_path, category_mapping_path=mapping_path)
    features = [
        _tree_feature(11.2558, 43.7696, {"species": "A b"}, osm_id=1),
        _tree_feature(11.2559, 43.7697, {"species": "A-b"}, osm_id=2),
    ]
    _write_tree_features(tmp_path, features)

    first_result = trees.run(load_config(config_path))
    first_manifest = json.loads(first_result.manifest_path.read_text(encoding="utf-8"))
    first_paths = {
        species: Path(path).name
        for species, path in first_manifest["details"]["surfaces"]["species_crowns"].items()
    }
    first_artifact_names = [
        artifact["name"]
        for artifact in first_manifest["artifacts"]
        if artifact["name"].startswith("species-crown-")
    ]

    assert len(set(first_paths.values())) == 2
    assert "a_b_crowns.stl" in first_paths.values()
    assert len(first_artifact_names) == len(set(first_artifact_names)) == 2

    _write_tree_features(tmp_path, list(reversed(features)))
    second_result = trees.run(load_config(config_path))
    second_manifest = json.loads(second_result.manifest_path.read_text(encoding="utf-8"))
    second_paths = {
        species: Path(path).name
        for species, path in second_manifest["details"]["surfaces"]["species_crowns"].items()
    }

    assert second_paths == first_paths


def test_planned_tree_uses_direct_model_and_partial_dimension_overrides(tmp_path: Path) -> None:
    config_path = _prepare_tree_fixture(tmp_path)
    _write_tree_features(
        tmp_path,
        [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [11.2558, 43.7696]},
                "properties": {
                    "id": "TREE-001",
                    "category": "trees",
                    "direct_model_category": "small_round_broadleaf",
                    "height_m": 9.0,
                    "crown_diameter_m": 4.0,
                    "roi_zone": "inner",
                    "tags": {},
                },
            }
        ],
    )

    result = trees.run(load_config(config_path))

    placement = _placement(result, "TREE-001")
    assert placement["model_category"] == "small_round_broadleaf"
    assert placement["height_m"] == 9.0
    assert placement["crown_radius_m"] == 2.0
    assert placement["trunk_radius_m"] == 0.068
    assert placement["model_source"] == "urban_planning:model"
    assert placement["height_source"] == "urban_planning:height_m"
    assert placement["crown_radius_source"] == "urban_planning:crown_diameter_m"
    assert placement["trunk_radius_source"] == "default:small_round_broadleaf.trunk_radius_m"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    information = manifest["details"]["information_summary"]
    assert information["trees_with_direct_planning_model"] == 1
    assert information["trees_with_any_model_input_tags_or_allometry"] == 1
    assert information["planning_value_counts"] == {
        "species_model": 1,
        "height_m": 1,
        "crown_radius_m": 1,
        "trunk_radius_m": 0,
    }
    assert "Trees reconstructed from direct urban-planning models: 1 / 1" in result.report_path.read_text(
        encoding="utf-8"
    )
    assert "Direct planning models" in result.preview_path.read_text(encoding="utf-8")


def test_direct_planned_tree_does_not_require_default_species_mapping(tmp_path: Path) -> None:
    category_mapping_path = tmp_path / "species_category_mapping.json"
    category_mapping_path.write_text(
        json.dumps({"species_to_category": {}}),
        encoding="utf-8",
    )
    config_path = _prepare_tree_fixture(
        tmp_path,
        category_mapping_path=category_mapping_path,
    )
    _write_tree_features(
        tmp_path,
        [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [11.2558, 43.7696]},
                "properties": {
                    "id": "TREE-001",
                    "category": "trees",
                    "direct_model_category": "small_round_broadleaf",
                    "roi_zone": "inner",
                    "tags": {},
                },
            }
        ],
    )

    result = trees.run(load_config(config_path))

    assert _placement(result, "TREE-001")["model_category"] == "small_round_broadleaf"


def test_missing_tree_category_mapping_has_no_removed_input_fallback(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    config = replace(config, trees=replace(config.trees, category_mapping_path=None))

    assert trees._species_category_mapping(config) == {}


def test_model_property_without_direct_category_keeps_species_mapping_path(tmp_path: Path) -> None:
    config_path = _prepare_tree_fixture(tmp_path)
    _write_tree_features(
        tmp_path,
        [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [11.2558, 43.7696]},
                "properties": {
                    "id": "TREE-001",
                    "category": "trees",
                    "model": "small_round_broadleaf",
                    "roi_zone": "inner",
                    "tags": {"species": "Unmapped species"},
                },
            }
        ],
    )

    with pytest.raises(ConfigError, match="Unmapped species.*not present"):
        trees.run(load_config(config_path))


def test_non_planned_tree_ignores_planning_dimension_properties(tmp_path: Path) -> None:
    config_path = _prepare_tree_fixture(tmp_path)
    _write_tree_features(
        tmp_path,
        [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [11.2558, 43.7696]},
                "properties": {
                    "category": "trees",
                    "height_m": 30.0,
                    "crown_diameter_m": 20.0,
                    "trunk_diameter_m": 2.0,
                    "roi_zone": "inner",
                    "tags": {
                        "species": "Celtis australis",
                        "height": 12.0,
                        "crown:diameter": 6.0,
                        "diameter": 0.5,
                    },
                },
            }
        ],
    )

    result = trees.run(load_config(config_path))

    placement = _placement(result, "tree_0001")
    assert placement["height_m"] == 12.0
    assert placement["crown_radius_m"] == 3.0
    assert placement["trunk_radius_m"] == 0.25
    assert placement["model_source"] == "tag:species:species_category_mapping"


def test_tree_stage_rejects_missing_stage1_manifest(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)

    with pytest.raises(ConfigError, match="01_shapefiles/manifest.json"):
        trees.run(load_config(config_path))


def test_tree_stage_rejects_default_species_missing_from_mapping(tmp_path: Path) -> None:
    category_mapping_path = tmp_path / "species_category_mapping.json"
    category_mapping_path.write_text(
        json.dumps({"species_to_category": {"Celtis australis": "large_round_broadleaf"}}),
        encoding="utf-8",
    )
    config_path = _prepare_tree_fixture(tmp_path, category_mapping_path=category_mapping_path)

    with pytest.raises(ConfigError, match=r"trees.default 'Tilia' is not present"):
        trees.run(load_config(config_path))


def test_tree_preview_view_center_uses_tree_bounds_not_surface_origin() -> None:
    instances = [
        _tree_instance("tree_0001", x=1000.0, y=2000.0),
        _tree_instance("tree_0002", x=1020.0, y=2040.0),
    ]

    scene = trees._scene_data(instances, surface_origin_x=0.0, surface_origin_y=0.0)

    assert scene["surfaceFrame"] == {"originX": 0.0, "originY": 0.0}
    assert scene["viewCenter"] == {"x": 1010.0, "y": 2020.0}
    assert scene["extent"] < 30.0
    assert [item["x"] for item in scene["trees"]] == [-10.0, 10.0]
    assert [item["y"] for item in scene["trees"]] == [-20.0, 20.0]


def test_projects_tree_bases_onto_supplied_terrain_geometry(tmp_path: Path) -> None:
    terrain_path = tmp_path / "terrain.obj"
    _write_terrain_geometry(terrain_path)
    config_path = _prepare_tree_fixture(tmp_path, terrain_geometry_path=terrain_path)

    result = trees.run(load_config(config_path))

    placements = json.loads(result.placement_geojson_path.read_text(encoding="utf-8"))
    placement_zs = [round(feature["geometry"]["coordinates"][2], 3) for feature in placements["features"]]
    assert placement_zs == [9.95, 9.95, 9.95, 9.95]
    combined_vertices = _stl_vertices(result.combined_stl_path)
    assert min(vertex[2] for vertex in combined_vertices) == pytest.approx(9.95, abs=1e-3)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["details"]["terrain_geometry_path"] == str(terrain_path)
    report = result.report_path.read_text(encoding="utf-8")
    assert "terrain geometry file is provided" in report


def test_manifest_is_published_after_tree_preview_and_report(tmp_path: Path, monkeypatch) -> None:
    config = load_config(_prepare_tree_fixture(tmp_path))
    published: list[Path] = []
    original_publish = trees.publish_stage_manifest

    def observe_publication(**kwargs):
        assert kwargs["preview_path"].is_file()
        assert kwargs["report_path"].is_file()
        published.append(kwargs["output_directory"] / "manifest.json")
        return original_publish(**kwargs)

    monkeypatch.setattr(trees, "publish_stage_manifest", observe_publication)

    result = trees.run(config)

    assert published == [result.manifest_path]


def test_trees_does_not_claim_universal_stage_output_lock(tmp_path: Path) -> None:
    config_path = _prepare_tree_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "05_trees"
    output_dir.mkdir(parents=True)
    lock_path = output_dir / ".stage.lock"
    lock_path.write_text("owned by a future transactional runner\n", encoding="utf-8")

    result = trees.run(load_config(config_path))

    assert result.manifest_path.is_file()
    assert lock_path.read_text(encoding="utf-8") == "owned by a future transactional runner\n"


def test_trees_rejects_wrong_stage_manifest_for_default_tree_handoff(tmp_path: Path) -> None:
    config_path = _prepare_tree_fixture(tmp_path)
    stage1_dir = tmp_path / "outputs" / "01_shapefiles"
    manifest = publish_test_stage_manifest(
        stage1_dir,
        stage="visual-enrichment",
        named_artifacts={"category-trees": (stage1_dir / "trees.geojson", ArtifactKind.HANDOFF)},
    )

    with pytest.raises(ConfigError, match="expected stage 'shapefiles'") as error:
        trees.run(load_config(config_path))

    assert str(manifest.manifest_path) in str(error.value)


@pytest.mark.parametrize("context", ["tree", "air purifier"])
def test_rejects_terrain_from_failed_city_models_handoff(tmp_path: Path, context: str) -> None:
    config_path = _prepare_tree_fixture(tmp_path)
    config = load_config(config_path)
    stage_dir = config.output.root_directory / "04_city_models"
    terrain_path = stage_dir / "city4cfd_output" / "Mesh_Terrain_Combined.obj"
    terrain_path.parent.mkdir(parents=True)
    terrain_path.write_text("v 0 0 0\n", encoding="utf-8")
    manifest = publish_stage_manifest(
        stage="city-models",
        status=StageStatus.FAILED_EXTERNAL_EXECUTION,
        output_directory=stage_dir,
        report_path=stage_dir / "city_models_report.md",
        preview_path=stage_dir / "city_models_preview.html",
        input_state_fingerprint={"fixture": "failed-city-models"},
        artifacts=(),
        metrics={},
        details={},
    )

    with pytest.raises(ConfigError, match="not completed") as error:
        validate_completed_city_models_terrain(config, terrain_path, context=context)

    assert str(manifest.manifest_path) in str(error.value)


@pytest.mark.parametrize("artifact_kind", [None, ArtifactKind.PREVIEW])
def test_rejects_unlisted_or_preview_city_models_terrain(
    tmp_path: Path,
    artifact_kind: ArtifactKind | None,
) -> None:
    config = load_config(_prepare_tree_fixture(tmp_path))
    stage_dir = config.output.root_directory / "04_city_models"
    terrain_path = stage_dir / "city4cfd_output" / "Mesh_Terrain_Combined.obj"
    terrain_path.parent.mkdir(parents=True)
    terrain_path.write_text("v 0 0 0\n", encoding="utf-8")
    artifacts = (
        (ArtifactReference("terrain-preview", terrain_path, artifact_kind),)
        if artifact_kind is not None
        else ()
    )
    manifest = publish_stage_manifest(
        stage="city-models",
        status=StageStatus.COMPLETED,
        output_directory=stage_dir,
        report_path=stage_dir / "city_models_report.md",
        preview_path=stage_dir / "city_models_preview.html",
        input_state_fingerprint={"fixture": "completed-city-models"},
        artifacts=artifacts,
        metrics={},
        details={},
    )

    with pytest.raises(ConfigError, match="declared handoff") as error:
        validate_completed_city_models_terrain(config, terrain_path)

    assert str(manifest.manifest_path) in str(error.value)


def test_terrain_sampler_uses_nearest_surface_at_internal_mesh_hole(tmp_path: Path) -> None:
    terrain_path = tmp_path / "terrain_with_hole.obj"
    terrain_path.write_text(
        "\n".join(
            [
                "v -10 -10 5",
                "v -2 -10 5",
                "v -10 10 5",
                "v -2 10 5",
                "v 2 -10 7",
                "v 10 -10 7",
                "v 2 10 7",
                "v 10 10 7",
                "f 1 2 3",
                "f 3 2 4",
                "f 5 6 7",
                "f 7 6 8",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sampler = load_terrain_sampler(terrain_path)

    assert sampler(-5.0, 0.0) == pytest.approx(5.0)
    assert sampler(5.0, 0.0) == pytest.approx(7.0)
    assert sampler(-1.0, 0.0) == pytest.approx(5.0)

    with pytest.raises(ConfigError, match="could not be projected"):
        sampler(20.0, 0.0)


@pytest.mark.parametrize(
    ("name", "contents", "line_number"),
    [
        ("malformed.obj", "v not-a-number 0 0\n", 1),
        (
            "malformed.stl",
            "solid terrain\n"
            "  facet normal 0 0 1\n"
            "    outer loop\n"
            "      vertex not-a-number 0 0\n"
            "      vertex 1 0 0\n"
            "      vertex 0 1 0\n"
            "    endloop\n"
            "  endfacet\n"
            "endsolid terrain\n",
            4,
        ),
    ],
)
def test_terrain_sampler_wraps_malformed_coordinates_with_path_and_line(
    tmp_path: Path,
    name: str,
    contents: str,
    line_number: int,
) -> None:
    terrain_path = tmp_path / name
    terrain_path.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        load_terrain_sampler(terrain_path)

    assert str(terrain_path) in str(error.value)
    assert f"line {line_number}" in str(error.value)
    assert "not-a-number" in str(error.value)


def test_terrain_sampler_wraps_invalid_obj_face_index_with_path_and_line(tmp_path: Path) -> None:
    terrain_path = tmp_path / "invalid_face.obj"
    terrain_path.write_text(
        "v 0 0 0\n"
        "v 1 0 0\n"
        "v 0 1 0\n"
        "f 1 2 4\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_terrain_sampler(terrain_path)

    assert str(terrain_path) in str(error.value)
    assert "line 4" in str(error.value)
    assert "f 1 2 4" in str(error.value)


def test_terrain_sampler_accepts_multiple_complete_ascii_stl_solids(tmp_path: Path) -> None:
    terrain_path = tmp_path / "multi_solid.stl"
    terrain_path.write_text(
        "solid west\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex -2 -1 3\n"
        "      vertex 0 -1 3\n"
        "      vertex -1 1 3\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid west\n"
        "solid east\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 -1 4\n"
        "      vertex 2 -1 4\n"
        "      vertex 1 1 4\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid east\n",
        encoding="ascii",
    )

    sampler = load_terrain_sampler(terrain_path)

    assert sampler(-1.0, 0.0) == pytest.approx(3.0)
    assert sampler(1.0, 0.0) == pytest.approx(4.0)


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (
            b"vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n",
            "unexpected.*line 1",
        ),
        (
            b"solid terrain\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n",
            "incomplete.*line 5",
        ),
        (
            b"solid terrain\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendfacet\nendsolid terrain\n",
            "expected endloop.*line 7",
        ),
        (b"solid terr\xffain\n", "ASCII STL"),
    ],
    ids=("bare-vertices", "truncated-facet", "missing-endloop", "non-ascii"),
)
def test_terrain_sampler_rejects_structurally_invalid_ascii_stl(
    tmp_path: Path,
    contents: bytes,
    message: str,
) -> None:
    terrain_path = tmp_path / "invalid.stl"
    terrain_path.write_bytes(contents)

    with pytest.raises(ConfigError, match=message):
        load_terrain_sampler(terrain_path)


def _prepare_tree_fixture(
    tmp_path: Path,
    terrain_geometry_path: Path | None = None,
    model_library_path: Path | None = None,
    category_mapping_path: Path | None = None,
) -> Path:
    config_path = _write_config(
        tmp_path,
        terrain_geometry_path=terrain_geometry_path,
        model_library_path=model_library_path,
        category_mapping_path=category_mapping_path,
    )
    stage1_dir = tmp_path / "outputs" / "01_shapefiles"
    stage1_dir.mkdir(parents=True)
    (stage1_dir / "trees.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    _tree_feature(11.2558, 43.7696, {"species": "Celtis australis", "height": "12 m"}, osm_id=1),
                    _tree_feature(
                        11.2559,
                        43.7697,
                        {"genus": "Tilia", "height": 16, "crown:diameter": 6, "diameter": 0.5},
                        osm_id=2,
                    ),
                    _tree_feature(11.2560, 43.7698, {"species": "Citrus spp."}, osm_id=3),
                    _tree_feature(11.2561, 43.7699, {}, osm_id=4),
                ],
            }
        ),
        encoding="utf-8",
    )
    publish_test_stage_manifest(
        stage1_dir,
        stage="shapefiles",
        named_artifacts={
            "category-trees": (stage1_dir / "trees.geojson", ArtifactKind.HANDOFF),
        },
    )
    return config_path


def _write_tree_features(tmp_path: Path, features: list[dict[str, object]]) -> None:
    (tmp_path / "outputs/01_shapefiles/trees.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )


def _placement(result: trees.TreesStageOutput, tree_id: str) -> dict[str, object]:
    payload = json.loads(result.placement_geojson_path.read_text(encoding="utf-8"))
    return next(
        feature["properties"]
        for feature in payload["features"]
        if feature["properties"]["tree_id"] == tree_id
    )


def _write_config(
    tmp_path: Path,
    terrain_geometry_path: Path | None = None,
    model_library_path: Path | None = None,
    category_mapping_path: Path | None = None,
) -> Path:
    config_path = tmp_path / "config.toml"
    input_lines: list[str] = []
    if terrain_geometry_path is not None:
        input_lines.append(f'tree_terrain_geometry_path = "{terrain_geometry_path.as_posix()}"')
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        name="Trees Fixture",
        inner_diameter_m=100.0,
        outer_diameter_m=150.0,
        input_lines=tuple(input_lines),
        model_library_path=model_library_path,
        category_mapping_path=category_mapping_path,
    )
    return config_path


def _tree_feature(lon: float, lat: float, tags: dict[str, object], osm_id: int) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "category": "trees",
            "osm_id": osm_id,
            "roi_zone": "inner",
            "tags": {"natural": "tree", **tags},
        },
    }


def _tree_instance(tree_id: str, x: float, y: float) -> trees.TreeInstance:
    return trees.TreeInstance(
        tree_id=tree_id,
        species="Tilia",
        source_species=None,
        model_category="large_round_broadleaf",
        crown_shape="ellipsoid",
        x=x,
        y=y,
        z=0.0,
        height_m=16.0,
        crown_radius_m=5.2,
        trunk_radius_m=0.14,
        trunk_height_m=5.44,
        roi_zone="inner",
        osm_id=None,
        model_source="default:Tilia:species_category_mapping",
        height_source="default:large_round_broadleaf.height_m",
        crown_radius_source="default:large_round_broadleaf.crown_radius_m",
        trunk_radius_source="default:large_round_broadleaf.trunk_radius_m",
        used_tags=(),
        defaulted_fields=("species_model", "height_m", "crown_radius_m", "trunk_radius_m"),
    )


def _write_terrain_geometry(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "v -50 -50 10",
                "v 50 -50 10",
                "v 50 50 10",
                "v -50 50 10",
                "f 1 2 3",
                "f 1 3 4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _stl_vertices(path: Path) -> list[tuple[float, float, float]]:
    vertices: list[tuple[float, float, float]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) == 4 and parts[0] == "vertex":
            vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
    return vertices
