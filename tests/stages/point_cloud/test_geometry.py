from __future__ import annotations

from cities_reconstruction.stages.point_cloud import geometry


def test_local_surface_relief_compares_nearby_xy_z_values() -> None:
    rows = [
        [10.0, 10.0, 10.0],
        [10.0, 15.0, 15.0],
        [10.0, 15.0, 15.0],
    ]

    assert geometry._local_surface_relief(rows, 1, 1, radius_cells=1, nodata_value=-9999.0) == 5.0
    assert geometry._local_surface_relief(rows, 2, 2, radius_cells=1, nodata_value=-9999.0) == 0.0
    assert geometry._local_surface_relief(rows, 2, 2, radius_cells=2, nodata_value=-9999.0) == 5.0


def test_estimates_nearby_roof_z_from_building_points() -> None:
    roof_index: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for point in [(0.0, 0.0, 15.0), (2.0, 0.0, 15.2), (4.0, 0.0, 22.0), (20.0, 20.0, 40.0)]:
        roof_index.setdefault(geometry._roof_index_key(point[0], point[1]), []).append(point)

    assert geometry._estimate_nearby_roof_z(1.0, 0.0, 16.0, roof_index) == 15.0
    assert geometry._estimate_nearby_roof_z(1.0, 0.0, 22.0, roof_index) == 22.0
    assert geometry._estimate_nearby_roof_z(4.2, 0.0, 22.3, roof_index) == 22.0
    assert geometry._estimate_nearby_roof_z(4.2, 0.0, 27.0, roof_index) == 22.0
    assert geometry._estimate_nearby_roof_z(100.0, 100.0, 20.0, roof_index) is None


def test_roof_cluster_selection_prefers_candidate_height_cluster_over_nearer_lower_roof() -> None:
    roof_index: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for point in [
        (0.0, 0.0, 15.0),
        (1.0, 0.0, 15.1),
        (3.0, 0.0, 21.8),
        (4.0, 0.0, 22.0),
        (5.0, 0.0, 22.1),
    ]:
        roof_index.setdefault(geometry._roof_index_key(point[0], point[1]), []).append(point)

    assert geometry._estimate_nearby_roof_z(1.2, 0.0, 22.0, roof_index) == 22.0


def test_building_footprint_buffer_matches_near_edge_points() -> None:
    polygon = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0), (0.0, 0.0)]

    assert geometry._point_within_any_polygon_buffer((4.9, 2.0), [_polygon(polygon)], 1.5) is True
    assert geometry._point_within_any_polygon_buffer((6.0, 2.0), [_polygon(polygon)], 1.5) is False


def test_polygon_spatial_index_preserves_exact_geometry_results() -> None:
    polygons = [
        _polygon([(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0), (0.0, 0.0)]),
        _polygon([(40.0, 40.0), (44.0, 40.0), (44.0, 44.0), (40.0, 44.0), (40.0, 40.0)]),
    ]
    index = geometry.PolygonSpatialIndex.build(polygons, buffer_m=1.5, cell_size=8.0)
    points = [(-2.0, 2.0), (-1.5, 2.0), (2.0, 2.0), (5.4, 2.0), (6.0, 2.0), (42.0, 42.0)]

    for point in points:
        assert index.contains(point) is geometry._point_in_any_polygon(point, polygons)
        assert index.within_buffer(point, 1.5) is geometry._point_within_any_polygon_buffer(
            point,
            polygons,
            1.5,
        )


def test_polygon_holes_are_excluded_with_explicit_boundary_and_buffer_semantics() -> None:
    polygon = _polygon(
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)],
        holes=[[(3.0, 3.0), (7.0, 3.0), (7.0, 7.0), (3.0, 7.0), (3.0, 3.0)]],
    )
    index = geometry.PolygonSpatialIndex.build([polygon], buffer_m=1.0, cell_size=4.0)
    expected_contains = {
        (2.0, 5.0): True,
        (5.0, 5.0): False,
        (0.0, 5.0): True,
        (3.0, 5.0): True,
        (-1.0, 5.0): False,
    }

    for point, expected in expected_contains.items():
        assert geometry._point_in_any_polygon(point, [polygon]) is expected
        assert index.contains(point) is expected

    assert index.within_buffer((3.5, 5.0), 1.0) is True
    assert index.within_buffer((5.0, 5.0), 1.0) is False
    assert index.within_buffer((3.0, 5.0), 1.0) is True


def _polygon(
    exterior: list[tuple[float, float]],
    holes: list[list[tuple[float, float]]] | None = None,
) -> geometry.ProjectedPolygon:
    return geometry.ProjectedPolygon(
        exterior=tuple(exterior),
        holes=tuple(tuple(hole) for hole in (holes or [])),
    )
