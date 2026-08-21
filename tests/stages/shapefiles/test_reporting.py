from __future__ import annotations

from pathlib import Path

from cities_reconstruction.config import load_config
from cities_reconstruction.stages.shapefiles.reporting import render_report
from tests.config_helpers import write_complete_config


def test_render_report_formats_uniform_roi_and_empty_diagnostics(tmp_path: Path) -> None:
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        inner_diameter_m=None,
        name="Rendering fixture",
    )
    config = load_config(config_path)
    summary = {
        "source": "cached fixture",
        "assumptions": ["Fixture assumption."],
        "classification_rules": [],
        "feature_counts": {
            "raw_overpass_elements": 0,
            "accepted": 0,
            "skipped": 0,
            "skipped_by_reason": {},
            "by_category": {"buildings": 0},
            "by_group_tag": {},
            "by_roi_zone": {},
            "available_not_mapped_to_core": {},
        },
        "tag_inventory": {
            "raw_elements": 0,
            "tagged_elements": 0,
            "tag_key_counts": {},
            "unclassified_feature_like_tag_value_counts": {},
        },
        "geometry_diagnostics": {
            "contributing_feature_count": 0,
            "non_contributing_feature_count": 0,
            "generated_gap_fill_feature_count": 0,
            "contributing_by_category": {},
            "non_contributing_by_category": {},
            "non_contributing_by_geometry_type": {},
            "contribution_rule": "Polygonal features contribute.",
            "gap_fill_policy": "Fill remaining ROI coverage.",
        },
        "tree_overlap_filter": {
            "removed_overpass_tree_count": 0,
            "tolerance_m": 1.0,
        },
        "tree_input_diagnostics": {"inputs": {}},
        "surface_input_diagnostics": {"surfaces": {}},
        "surface_overlap_diagnostics": {
            "by_supplemental": {},
            "precedence": ["buildings"],
            "input_polygon_features": 0,
            "accepted_polygon_features": 0,
            "clipped_polygon_features": 0,
            "removed_polygon_features": 0,
            "removed_overlap_area_m2": 0.0,
            "policy": "Higher-precedence surfaces win.",
        },
        "urban_planning": {
            "accepted_by_kind": {"tree": 0, "air_purifier": 0},
            "outside_roi": 0,
            "inputs": {},
            "outside_records": [],
        },
    }
    artifact_paths = {
        name: tmp_path / name
        for name in (
            "tag_inventory_query.txt",
            "tag_inventory_raw.json",
            "tag_inventory.json",
            "overpass_query.txt",
            "overpass_raw.json",
            "all_features.geojson",
            "urban_planning.geojson",
            "air_purifiers.geojson",
            "geometry_diagnostics.json",
            "non_contributing_features.geojson",
            "imagery_diagnostics.json",
            "imagery_overlay.html",
            "summary.json",
            "preview.html",
        )
    }

    report = render_report(
        config=config,
        summary=summary,
        categories=("buildings",),
        tag_inventory_query_path=artifact_paths["tag_inventory_query.txt"],
        tag_inventory_raw_path=artifact_paths["tag_inventory_raw.json"],
        tag_inventory_path=artifact_paths["tag_inventory.json"],
        query_path=artifact_paths["overpass_query.txt"],
        raw_path=artifact_paths["overpass_raw.json"],
        all_features_path=artifact_paths["all_features.geojson"],
        urban_planning_path=artifact_paths["urban_planning.geojson"],
        air_purifiers_path=artifact_paths["air_purifiers.geojson"],
        category_paths={"buildings": tmp_path / "buildings.geojson"},
        region_paths={"full_region": tmp_path / "full_region.geojson"},
        diagnostics_path=artifact_paths["geometry_diagnostics.json"],
        diagnostics_geojson_path=artifact_paths["non_contributing_features.geojson"],
        imagery_diagnostics_path=artifact_paths["imagery_diagnostics.json"],
        imagery_overlay_path=artifact_paths["imagery_overlay.html"],
        summary_path=artifact_paths["summary.json"],
        preview_path=artifact_paths["preview.html"],
    )

    assert "# Feature Retrieval Report" in report
    assert "- Name: Rendering fixture" in report
    assert "- Inner diameter: not set (uniform treatment across the outer ROI)" in report
    assert "- buildings: 0" in report
    assert "- none: 0" in report
    assert f"- Graphical preview: `{artifact_paths['preview.html']}`" in report
