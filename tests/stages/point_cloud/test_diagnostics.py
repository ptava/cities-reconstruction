from __future__ import annotations

from pathlib import Path

from cities_reconstruction.config import load_config
from cities_reconstruction.stages.point_cloud.diagnostics import (
    build_alignment_diagnostics,
    estimate_horizontal_offset,
)
from cities_reconstruction.stages.point_cloud.geometry import ProjectedPolygon
from tests.config_helpers import write_complete_config


def test_alignment_diagnostics_uses_raw_shifted_elevated_candidates(tmp_path: Path) -> None:
    config = load_config(write_complete_config(tmp_path / "config.toml", output_root=tmp_path / "outputs"))
    footprint = [_polygon([(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5), (0.0, 0.0)])]

    def diagnostics(candidates: list[tuple[float, float, float]]) -> dict[str, object]:
        return build_alignment_diagnostics(
            config=config,
            footprint_path=tmp_path / "buildings.geojson",
            building_polygons=footprint,
            ground_points=[],
            building_points=[],
            alignment_candidate_points=candidates,
            tree_points=[],
            unclassified_points=[],
            raster_summary={},
            tree_mask=None,
            tree_tag_points=[],
            same_metric_output_crs=True,
        )

    aligned = diagnostics([(0.25, 0.25, 12.0)])
    warning = diagnostics([(3.25, 0.25, 12.0)])
    failed = diagnostics([(6.25, 0.25, 12.0)])
    insufficient = diagnostics([])

    assert aligned["alignment_status"] == "passed"
    assert aligned["best_offset_m"] == {"x": 0, "y": 0}
    assert warning["alignment_status"] == "warning"
    assert warning["best_offset_m"] == {"x": 3, "y": 0}
    assert failed["alignment_status"] == "failed"
    assert failed["best_offset_m"] == {"x": 6, "y": 0}
    assert insufficient["alignment_status"] == "warning"
    assert insufficient["alignment_candidate_point_count"] == 0


def test_alignment_offset_respects_polygon_holes() -> None:
    polygon = _polygon(
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)],
        holes=[[(3.0, 3.0), (7.0, 3.0), (7.0, 7.0), (3.0, 7.0), (3.0, 3.0)]],
    )

    best_offset, score = estimate_horizontal_offset([(5.0, 5.0, 12.0)], [polygon])

    assert best_offset == (-2, 0)
    assert score == 1


def _polygon(
    exterior: list[tuple[float, float]],
    holes: list[list[tuple[float, float]]] | None = None,
) -> ProjectedPolygon:
    return ProjectedPolygon(
        exterior=tuple(exterior),
        holes=tuple(tuple(hole) for hole in (holes or [])),
    )
