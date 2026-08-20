from __future__ import annotations

from pathlib import Path

from cities_reconstruction.stage_layout import (
    STAGE_LAYOUTS,
    StageId,
    stage_output_directory,
)


def test_stage_layout_catalogue_preserves_current_pipeline_paths() -> None:
    assert tuple(
        (layout.stage_id.value, layout.number, layout.number_name)
        for layout in STAGE_LAYOUTS
    ) == (
        ("shapefiles", 1, "01_shapefiles"),
        ("visual-enrichment", 2, "02_visual_enrichment"),
        ("point-cloud", 3, "03_point_cloud"),
        ("city-models", 4, "04_city_models"),
        ("trees", 5, "05_trees"),
        ("air-purifiers", 6, "06_air_purifiers"),
        ("openfoam", 7, "07_openfoam"),
    )

    assert all(
        layout.number_name
        == f"{layout.number:02d}_{layout.stage_id.value.replace('-', '_')}"
        for layout in STAGE_LAYOUTS
    )
    assert len({layout.number for layout in STAGE_LAYOUTS}) == 7
    assert len({layout.number_name for layout in STAGE_LAYOUTS}) == 7


def test_stage_output_directory_uses_catalogued_layout(tmp_path: Path) -> None:
    assert stage_output_directory(
        tmp_path,
        StageId.POINT_CLOUD,
    ) == tmp_path / "03_point_cloud"
