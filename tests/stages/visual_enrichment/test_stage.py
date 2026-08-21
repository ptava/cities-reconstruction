from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.stage_contract import ArtifactKind, StageOutput, StageStatus
from cities_reconstruction.stages.visual_enrichment import stage as visual_enrichment
from tests.config_helpers import write_complete_config
from tests.stage_manifest_helpers import publish_test_stage_manifest

ROOT = Path(__file__).resolve().parents[3]


def test_visual_enrichment_is_deferred_segmentation_plan() -> None:
    config = load_config(ROOT / "config/examples/florence.toml")

    result = visual_enrichment.plan(config)

    assert result.stage == "visual-enrichment"
    assert "segmentation" in result.summary
    planned = " ".join(result.planned_actions)
    assert "building footprints" in planned
    assert "LOD 2.2" in planned
    assert "roads, asphalt, paved surfaces, and concrete surfaces" in planned
    assert "do not promote segmented geometry" in planned


def test_visual_enrichment_failure_invalidates_stale_completion_manifest(tmp_path: Path) -> None:
    config_path = write_complete_config(tmp_path / "config.toml", output_root=tmp_path / "outputs")
    manifest_path = tmp_path / "outputs" / "02_visual_enrichment" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('{"stale": true}', encoding="utf-8")

    with pytest.raises(ConfigError, match="01_shapefiles/manifest.json"):
        visual_enrichment.run(load_config(config_path))

    assert not manifest_path.exists()


def test_visual_enrichment_rejects_failed_shapefiles_handoff(tmp_path: Path) -> None:
    config_path = write_complete_config(tmp_path / "config.toml", output_root=tmp_path / "outputs")
    stage1_dir = tmp_path / "outputs" / "01_shapefiles"
    stage1_dir.mkdir(parents=True)
    source_path = stage1_dir / "all_features.geojson"
    source_path.write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
    manifest = publish_test_stage_manifest(
        stage1_dir,
        stage="shapefiles",
        status=StageStatus.FAILED_EXTERNAL_EXECUTION,
        named_artifacts={"all-features": (source_path, ArtifactKind.HANDOFF)},
    )

    with pytest.raises(ConfigError, match="not completed") as error:
        visual_enrichment.run(load_config(config_path))

    assert str(manifest.manifest_path) in str(error.value)


def test_visual_enrichment_does_not_claim_universal_stage_output_lock(tmp_path: Path) -> None:
    config_path = write_complete_config(tmp_path / "config.toml", output_root=tmp_path / "outputs")
    stage1_dir = tmp_path / "outputs" / "01_shapefiles"
    stage1_dir.mkdir(parents=True)
    (stage1_dir / "all_features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}),
        encoding="utf-8",
    )
    publish_test_stage_manifest(
        stage1_dir,
        stage="shapefiles",
        named_artifacts={
            "all-features": (stage1_dir / "all_features.geojson", ArtifactKind.HANDOFF),
        },
    )
    output_dir = tmp_path / "outputs" / "02_visual_enrichment"
    output_dir.mkdir(parents=True)
    lock_path = output_dir / ".stage.lock"
    lock_path.write_text("owned by a future transactional runner\n", encoding="utf-8")

    result = visual_enrichment.run(load_config(config_path))

    assert result.manifest_path.is_file()
    assert lock_path.read_text(encoding="utf-8") == "owned by a future transactional runner\n"


def test_visual_enrichment_fingerprint_canonicalizes_external_candidate_paths(tmp_path: Path) -> None:
    config_path = write_complete_config(tmp_path / "config.toml", output_root=tmp_path / "outputs")
    stage1_dir = tmp_path / "outputs" / "01_shapefiles"
    stage1_dir.mkdir(parents=True)
    (stage1_dir / "all_features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}),
        encoding="utf-8",
    )
    publish_test_stage_manifest(
        stage1_dir,
        stage="shapefiles",
        named_artifacts={
            "all-features": (stage1_dir / "all_features.geojson", ArtifactKind.HANDOFF),
        },
    )
    segmentation_path = tmp_path / "segmentation.geojson"
    segmentation_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": []}),
        encoding="utf-8",
    )
    relative_segmentation_path = Path(os.path.relpath(segmentation_path, Path.cwd()))

    absolute_result = visual_enrichment.run(
        load_config(config_path),
        segmentation_geojson_path=segmentation_path.resolve(),
    )
    relative_result = visual_enrichment.run(
        load_config(config_path),
        segmentation_geojson_path=relative_segmentation_path,
    )

    assert relative_result.manifest.input_state_fingerprint == absolute_result.manifest.input_state_fingerprint
    assert relative_result.details["segmentation_source"] == str(segmentation_path.resolve())


def test_visual_enrichment_writes_reviewable_segmentation_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    write_complete_config(config_path, output_root=output_root, name="Visual Fixture")
    stage1_dir = output_root / "01_shapefiles"
    stage1_dir.mkdir(parents=True)
    (stage1_dir / "all_features.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    _feature(
                        "building",
                        "buildings",
                        [
                            [11.25575, 43.76955],
                            [11.25585, 43.76955],
                            [11.25585, 43.76965],
                            [11.25575, 43.76965],
                            [11.25575, 43.76955],
                        ],
                        osm_id=1,
                    ),
                    _feature(
                        "gap",
                        "gap_fill",
                        [
                            [11.25588, 43.76955],
                            [11.25600, 43.76955],
                            [11.25600, 43.76967],
                            [11.25588, 43.76967],
                            [11.25588, 43.76955],
                        ],
                        osm_id="gap_fill_1",
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )
    (stage1_dir / "imagery_diagnostics.json").write_text(
        json.dumps(
            {
                "bbox_lon_lat": {
                    "min_lon": 11.254,
                    "min_lat": 43.768,
                    "max_lon": 11.258,
                    "max_lat": 43.772,
                },
                "sources": [
                    {
                        "name": "Fixture orthophoto",
                        "status": "fetched",
                        "image_path": str(stage1_dir / "imagery" / "fixture.png"),
                        "width": 1200,
                        "height": 1200,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    publish_test_stage_manifest(
        stage1_dir,
        stage="shapefiles",
        named_artifacts={
            "all-features": (stage1_dir / "all_features.geojson", ArtifactKind.HANDOFF),
            "imagery-diagnostics": (
                stage1_dir / "imagery_diagnostics.json",
                ArtifactKind.DIAGNOSTIC,
            ),
        },
    )
    segmentation_path = tmp_path / "segmentation.geojson"
    segmentation_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    _segmentation_feature(
                        "building",
                        0.91,
                        [
                            [11.25574, 43.76955],
                            [11.25580, 43.76954],
                            [11.25586, 43.76956],
                            [11.25586, 43.76964],
                            [11.25580, 43.76966],
                            [11.25574, 43.76964],
                            [11.25574, 43.76955],
                        ],
                    ),
                    _segmentation_feature(
                        "vegetation",
                        0.82,
                        [
                            [11.25589, 43.76956],
                            [11.25599, 43.76956],
                            [11.25599, 43.76966],
                            [11.25589, 43.76966],
                            [11.25589, 43.76956],
                        ],
                    ),
                    _segmentation_feature(
                        "asphalt",
                        0.76,
                        [
                            [11.25570, 43.76970],
                            [11.25595, 43.76970],
                            [11.25595, 43.76974],
                            [11.25570, 43.76974],
                            [11.25570, 43.76970],
                        ],
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )
    sat2lod2_path = tmp_path / "sat2lod2_building_polygons.geojson"
    sat2lod2_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [11.255735, 43.769545],
                                    [11.255805, 43.769535],
                                    [11.255865, 43.769565],
                                    [11.255875, 43.769635],
                                    [11.255805, 43.769670],
                                    [11.255735, 43.769635],
                                    [11.255735, 43.769545],
                                ]
                            ],
                        },
                        "properties": {"confidence": 0.88, "source_image": "sat2lod2_orthophoto.tif"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    published: list[Path] = []
    original_publish = visual_enrichment.publish_stage_manifest

    def observe_publication(**kwargs):
        assert kwargs["report_path"].is_file()
        assert kwargs["preview_path"].is_file()
        published.append(kwargs["output_directory"] / "manifest.json")
        return original_publish(**kwargs)

    monkeypatch.setattr(visual_enrichment, "publish_stage_manifest", observe_publication)

    result = visual_enrichment.run(
        load_config(config_path),
        segmentation_geojson_path=segmentation_path,
        sat2lod2_geojson_path=sat2lod2_path,
    )

    assert isinstance(result, StageOutput)
    assert result.candidate_count == 4
    assert result.sat2lod2_feature_count == 1
    assert result.candidate_building_footprints_path.exists()
    assert result.candidate_terrain_surfaces_path.exists()
    assert result.candidate_roads_paved_concrete_path.exists()
    assert result.enriched_all_features_path.exists()
    assert result.segmentation_overlay_path.exists()
    overlay = result.segmentation_overlay_path.read_text(encoding="utf-8")
    assert "Zoom in" in overlay
    assert "Reset zoom" in overlay
    assert "mouse wheel or zoom buttons" in overlay

    buildings = json.loads(result.candidate_building_footprints_path.read_text(encoding="utf-8"))
    assert len(buildings["features"]) == 2
    building = buildings["features"][0]["properties"]
    assert building["review_status"] == "needs_review"
    assert building["proposed_action"] == "refine_existing_footprint_for_lod22"
    assert building["footprint_vertex_count_delta"] > 0
    assert building["include_in_building_lod22_reconstruction"] is False
    assert building["contributes_to_geometry"] is False
    sat2lod2_building = buildings["features"][1]["properties"]
    assert sat2lod2_building["source"] == "sat2lod2"
    assert sat2lod2_building["segmentation_backend"] == "GDAOSU/LOD2BuildingModel SAT2LoD2 external adapter"
    assert sat2lod2_building["proposed_action"] == "import_sat2lod2_refined_footprint_for_lod22"

    terrain = json.loads(result.candidate_terrain_surfaces_path.read_text(encoding="utf-8"))
    assert terrain["features"][0]["properties"]["proposed_action"] == "replace_gap_fill_with_green_areas"

    roads = json.loads(result.candidate_roads_paved_concrete_path.read_text(encoding="utf-8"))
    assert roads["features"][0]["properties"]["suggested_source_tag"] == "surface=asphalt"

    enriched = json.loads(result.enriched_all_features_path.read_text(encoding="utf-8"))
    assert len(enriched["features"]) == 6
    assert sum(1 for feature in enriched["features"] if feature["properties"].get("source") == "segmentation") == 3
    assert sum(1 for feature in enriched["features"] if feature["properties"].get("source") == "sat2lod2") == 1

    diagnostics = json.loads(result.segmentation_diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["status"] == "processed_segmentation_input"
    assert diagnostics["candidate_counts"] == {"buildings": 2, "roads_paved_concrete": 1, "terrain": 1}
    assert diagnostics["sat2lod2_feature_count"] == 1
    assert "review_status=needs_review" in diagnostics["review_policy"]
    manifest = json.loads(result.sat2lod2_handoff_manifest_path.read_text(encoding="utf-8"))
    assert manifest["adapter"] == "GDAOSU/LOD2BuildingModel SAT2LoD2 external adapter"
    assert manifest["status"] == "sat2lod2_output_available"

    report = result.report_path.read_text(encoding="utf-8")
    assert "Visual Enrichment Report" in report
    assert "Candidate roads / paved / concrete surfaces: 1" in report
    assert "SAT2LoD2 building polygons read: 1" in report

    stage_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert stage_manifest["schema_version"] == 2
    assert stage_manifest["stage"] == "visual-enrichment"
    assert stage_manifest["status"] == "completed"
    assert stage_manifest["preview_path"] == str(result.segmentation_overlay_path)
    artifacts = {artifact["name"]: artifact for artifact in stage_manifest["artifacts"]}
    assert all(artifact["required"] is True for artifact in artifacts.values())
    assert artifacts["candidate-building-footprints"]["kind"] == "supporting"
    assert artifacts["segmentation-diagnostics"]["kind"] == "diagnostic"
    assert artifacts["segmentation-overlay"]["kind"] == "preview"
    assert stage_manifest["metrics"] == {
        "source_feature_count": result.source_feature_count,
        "segmentation_feature_count": result.segmentation_feature_count,
        "sat2lod2_feature_count": result.sat2lod2_feature_count,
        "candidate_count": result.candidate_count,
    }
    assert result.to_dict() == stage_manifest
    assert published == [result.manifest_path]


def _feature(name: str, category: str, coordinates: list[list[float]], osm_id: int | str) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [coordinates]},
        "properties": {
            "osm_type": "way" if isinstance(osm_id, int) else "generated",
            "osm_id": osm_id,
            "category": category,
            "source_tag": f"{name}=fixture",
            "roi_zone": "inner",
            "contributes_to_geometry": True,
        },
    }


def _segmentation_feature(segmentation_class: str, confidence: float, coordinates: list[list[float]]) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [coordinates]},
        "properties": {
            "segmentation_class": segmentation_class,
            "confidence": confidence,
            "backend": "fixture-segmenter",
            "source_image": "fixture.png",
        },
    }
