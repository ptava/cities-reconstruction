from __future__ import annotations

from pathlib import Path

from cities_reconstruction.stages.city_models.rendering import city4cfd_mesh_scene_data


def test_city4cfd_mesh_scene_recenters_elevated_obj_z(tmp_path: Path) -> None:
    building_mesh = tmp_path / "Mesh_Buildings.obj"
    terrain_mesh = tmp_path / "Mesh_Terrain.obj"
    building_mesh.write_text(
        "\n".join(
            [
                "v 10 20 44",
                "v 11 20 47",
                "v 10 21 58",
                "f 1 2 3",
            ]
        ),
        encoding="utf-8",
    )
    terrain_mesh.write_text(
        "\n".join(
            [
                "v 8 18 43",
                "v 9 18 43",
                "v 8 19 43",
                "f 1 2 3",
            ]
        ),
        encoding="utf-8",
    )

    scene = city4cfd_mesh_scene_data(building_mesh, terrain_mesh)
    z_values = [point[2] for triangle in scene["triangles"] for point in triangle["points"]]

    assert min(z_values) == 0
    assert max(z_values) == 15
