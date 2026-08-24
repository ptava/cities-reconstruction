from __future__ import annotations

from pathlib import Path

from cities_reconstruction.stages.city_models.reporting import render_surface_layer_report


def test_render_surface_layer_report_records_split_mesh_presence(tmp_path: Path) -> None:
    roads_mesh = tmp_path / "Mesh_roads.obj"
    roads_mesh.write_text("# mesh\n", encoding="utf-8")
    water_mesh = tmp_path / "Mesh_water.obj"
    layers = [
        {
            "category": "roads",
            "layer_path": tmp_path / "roads.geojson",
            "layer_name": "roads",
            "feature_count": 4,
        },
        {
            "category": "water",
            "layer_path": tmp_path / "water.geojson",
            "layer_name": "water",
            "feature_count": 2,
        },
    ]

    report = render_surface_layer_report(
        layers,
        {"roads": roads_mesh, "water": water_mesh},
    )

    assert f"generated mesh: `{roads_mesh}` (present)" in report
    assert f"generated mesh: `{water_mesh}` (not present)" in report
