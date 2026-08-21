from __future__ import annotations

from pathlib import Path

from cities_reconstruction.config import load_config
from cities_reconstruction.stages.shapefiles.rendering import (
    render_imagery_overlay_html,
    render_preview_html,
)
from tests.config_helpers import write_complete_config


def test_render_preview_html_shows_region_and_category_count(tmp_path: Path) -> None:
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        name="Rendering fixture",
    )
    config = load_config(config_path)
    summary = {
        "tree_overlap_filter": {"removed_overpass_tree_markers": []},
        "feature_counts": {"by_category": {"buildings": 2}},
        "surface_overlap_diagnostics": {
            "precedence": ["buildings"],
            "input_polygon_features": 2,
            "accepted_polygon_features": 2,
            "clipped_polygon_features": 0,
            "removed_polygon_features": 0,
            "removed_overlap_area_m2": 0.0,
        },
    }

    rendered = render_preview_html(
        config,
        [],
        summary,
        categories=("buildings",),
    )

    assert "<title>Rendering fixture shapefiles preview</title>" in rendered
    assert 'aria-label="Retrieved feature preview"' in rendered
    assert "Buildings" in rendered
    assert "<td>2</td>" in rendered


def test_render_imagery_overlay_html_explains_missing_sources(tmp_path: Path) -> None:
    config_path = write_complete_config(
        tmp_path / "config.toml",
        output_root=tmp_path / "outputs",
        name="Rendering fixture",
    )
    config = load_config(config_path)
    diagnostics = {
        "bbox_lon_lat": {
            "min_lon": 11.25,
            "min_lat": 43.76,
            "max_lon": 11.26,
            "max_lat": 43.77,
        },
        "sources": [],
    }

    rendered = render_imagery_overlay_html(
        config,
        [],
        diagnostics,
        {"removed_overpass_tree_count": 0},
        categories=("buildings",),
    )

    assert "<title>Rendering fixture imagery overlay</title>" in rendered
    assert "<h2>No imagery fetched</h2>" in rendered
    assert "<li>No imagery sources are configured.</li>" in rendered
