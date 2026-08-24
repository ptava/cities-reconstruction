from cities_reconstruction.stages.trees import geometry


def test_translate_triangles_offsets_each_vertex_without_changing_label() -> None:
    triangles = [("crown", (1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0))]

    translated = geometry.translate_triangles(triangles, dx=-1.0, dy=2.0, dz=0.5)

    assert translated == [("crown", (0.0, 4.0, 3.5), (3.0, 7.0, 6.5), (6.0, 10.0, 9.5))]
