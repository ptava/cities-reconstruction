from __future__ import annotations

from pathlib import Path

from cities_reconstruction.config import load_config
from cities_reconstruction.stages.point_cloud.rendering import (
    point_cloud_scene_data,
    render_preview_html,
)
from cities_reconstruction.stages.point_cloud.reporting import render_report
from tests.config_helpers import write_complete_config


def test_render_report_describes_point_cloud_results(tmp_path: Path) -> None:
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        name="Presentation fixture",
    )
    diagnostics = {
        "alignment_status": "passed",
        "message": "Fixture alignment passed.",
        "ground_point_count": 2,
        "building_point_count": 1,
        "tree_point_count": 0,
        "unclassified_point_count": 1,
        "dsm_classification_complete": True,
        "footprint_polygon_count": 1,
        "estimated_horizontal_shift_m": 0.0,
        "tree_filter": {"enabled": False, "tree_tag_point_count": 0},
        "assumptions": ["Fixture assumption."],
    }

    rendered = render_report(
        config=load_config(config_path),
        footprint_path=tmp_path / "buildings.geojson",
        projected_footprints_path=tmp_path / "projected.geojson",
        ground_path=tmp_path / "ground.ply",
        building_path=tmp_path / "buildings.ply",
        tree_path=None,
        unclassified_path=tmp_path / "unclassified.ply",
        diagnostics_path=tmp_path / "diagnostics.json",
        manifest_path=tmp_path / "manifest.json",
        preview_path=tmp_path / "preview.html",
        diagnostics=diagnostics,
    )

    assert "# Point Cloud Preparation Report" in rendered
    assert "- Name: Presentation fixture" in rendered
    assert "- Ground points: 2" in rendered
    assert "- Tree filter: disabled" in rendered
    assert "Fixture assumption." in rendered


def test_rendering_builds_scene_data_and_html(tmp_path: Path) -> None:
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        name="Presentation fixture",
    )
    config = load_config(config_path)
    scene = point_cloud_scene_data(
        config,
        building_polygons=[],
        ground_points=[(0.0, 0.0, 10.0)],
        building_points=[],
        tree_points=[],
        unclassified_points=[(0.0, 0.0, 30.0)],
        projected_bbox=(-8.0, -8.0, 8.0, 8.0),
    )
    rendered = render_preview_html(
        config,
        building_polygons=[],
        ground_points=[(0.0, 0.0, 10.0)],
        building_points=[],
        tree_points=[],
        unclassified_points=[(0.0, 0.0, 30.0)],
        diagnostics={"alignment_status": "passed", "estimated_horizontal_shift_m": 0.0},
        projected_bbox=(-8.0, -8.0, 8.0, 8.0),
        tree_building_footprint_buffer_m=1.5,
        tree_roof_offset_threshold_m=4.0,
        tree_roof_search_radius_m=8.0,
    )

    assert scene["maxZ"] == 20.0
    assert scene["totalUnclassifiedPoints"] == 1
    assert "<title>Presentation fixture point-cloud alignment</title>" in rendered
    assert '"totalUnclassifiedPoints":1' in rendered
