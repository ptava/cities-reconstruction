from __future__ import annotations

import json
from pathlib import Path

from cities_reconstruction.config import load_config
from cities_reconstruction.stages import visual_enrichment
from tests.config_helpers import write_complete_config


ROOT = Path(__file__).resolve().parents[1]


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


def test_visual_enrichment_writes_reviewable_segmentation_candidates(tmp_path: Path) -> None:
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

    result = visual_enrichment.run(
        load_config(config_path),
        segmentation_geojson_path=segmentation_path,
        sat2lod2_geojson_path=sat2lod2_path,
    )

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
