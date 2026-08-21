from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.stage_contract import ArtifactKind
from cities_reconstruction.stages.point_cloud import stage as point_cloud
from cities_reconstruction.stages.point_cloud import rendering as point_cloud_rendering
from tests.config_helpers import write_complete_config
from tests.stage_manifest_helpers import publish_test_stage_manifest


def test_generates_separate_city4cfd_point_clouds_and_alignment_artifacts(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    outputs = tmp_path / "outputs"
    dtm_dir = tmp_path / "dtm"
    dsm_dir = tmp_path / "dsm"
    dtm_dir.mkdir()
    dsm_dir.mkdir()
    center_lon = 11.2558
    center_lat = 43.7696
    center_x, center_y = point_cloud._lonlat_to_epsg25832(center_lon, center_lat)
    _write_grid(dtm_dir / "tile.ASC", center_x, center_y, elevated=False)
    _write_grid(dsm_dir / "tile.ASC", center_x, center_y, elevated=True)
    stage1_footprints = outputs / "01_shapefiles" / "buildings.geojson"
    _write_buildings(stage1_footprints, center_lon, center_lat)
    dormant_footprints = outputs / "02_visual_enrichment" / "lod22_buildings.geojson"
    dormant_footprints.parent.mkdir(parents=True)
    dormant_footprints.write_text("{}", encoding="utf-8")
    stale_tree_points = outputs / "03_point_cloud" / "tree_points.ply"
    stale_tree_points.parent.mkdir(parents=True)
    stale_tree_points.write_text("stale tree cloud\n", encoding="utf-8")
    write_complete_config(
        config_path,
        output_root=outputs,
        name="Point Cloud Fixture",
        center_lat=center_lat,
        center_lon=center_lon,
        inner_diameter_m=12.0,
        outer_diameter_m=16.0,
        input_lines=(
            f'dtm_directory = "{dtm_dir.as_posix()}"',
            f'dsm_directory = "{dsm_dir.as_posix()}"',
        ),
    )

    def fail_if_tree_roof_index_is_built(*_args, **_kwargs):
        raise AssertionError("tree roof index must not be built when tree filtering is disabled")

    monkeypatch.setattr(point_cloud, "_building_roof_point_index", fail_if_tree_roof_index_is_built)
    result = point_cloud.run(load_config(config_path))

    assert result.ground_point_count == 25
    assert result.building_point_count > 0
    assert result.tree_point_count == 0
    assert result.tree_points_path is None
    assert not stale_tree_points.exists()
    assert result.alignment_status == "passed"
    assert result.ground_points_path.exists()
    assert result.building_points_path.exists()
    assert result.projected_footprints_path.exists()
    projected_footprints = json.loads(result.projected_footprints_path.read_text(encoding="utf-8"))
    assert projected_footprints["crs"]["properties"]["name"] == "EPSG:25832"
    assert {feature["properties"]["building_base_height_m"] for feature in projected_footprints["features"]} == {0.0}
    assert "element vertex 25" in result.ground_points_path.read_text(encoding="utf-8")
    ground_vertices = _ply_vertices(result.ground_points_path)
    building_vertices = _ply_vertices(result.building_points_path)
    assert {round(vertex[2], 3) for vertex in ground_vertices} == {10.0}
    assert {round(vertex[2], 3) for vertex in building_vertices} == {15.0}
    assert result.unclassified_points_path.exists()
    assert result.ground_point_count == (
        result.building_point_count
        + result.tree_point_count
        + result.unclassified_point_count
    )
    unclassified_vertices = _ply_vertices(result.unclassified_points_path)
    assert len(unclassified_vertices) == result.unclassified_point_count
    assert all(vertex not in building_vertices for vertex in unclassified_vertices)

    legacy_manifest_path = result.output_directory / "city4cfd_point_cloud_manifest.json"
    assert result.manifest_path == result.output_directory / "manifest.json"
    assert not legacy_manifest_path.exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["application_version"] == "0.1.0"
    assert manifest["stage"] == "point-cloud"
    assert manifest["status"] == "completed"
    assert manifest["finished_at_utc"].endswith("+00:00")
    assert manifest["input_state_fingerprint"]["kind"] == "sha256-canonical-path-size-mtime-ns"
    artifacts = {artifact["name"]: artifact for artifact in manifest["artifacts"]}
    assert artifacts["projected-building-footprints"] == {
        "name": "projected-building-footprints",
        "path": str(result.projected_footprints_path),
        "kind": "handoff",
        "required": True,
    }
    assert artifacts["ground-points"] == {
        "name": "ground-points",
        "path": str(result.ground_points_path),
        "kind": "handoff",
        "required": True,
    }
    assert artifacts["building-points"] == {
        "name": "building-points",
        "path": str(result.building_points_path),
        "kind": "handoff",
        "required": True,
    }
    assert "tree-points" not in artifacts
    assert artifacts["unclassified-points"] == {
        "name": "unclassified-points",
        "path": str(result.unclassified_points_path),
        "kind": "diagnostic",
        "required": True,
    }
    assert manifest["metrics"] == {
        "ground_point_count": result.ground_point_count,
        "building_point_count": result.building_point_count,
        "tree_point_count": result.tree_point_count,
        "unclassified_point_count": result.unclassified_point_count,
        "alignment_status": "passed",
    }
    assert result.to_dict() == manifest

    diagnostics = json.loads(result.diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["footprint_path"] == str(stage1_footprints)
    assert diagnostics["crs"]["same_metric_output_crs"] is True
    assert diagnostics["estimated_horizontal_shift_m"] <= 2.0
    assert diagnostics["alignment_candidate_point_count"] == 9
    assert diagnostics["unclassified_point_count"] == result.unclassified_point_count
    assert diagnostics["dsm_classification_complete"] is True
    assert "before footprint and tree classification" in diagnostics["alignment_evidence"]

    preview = result.preview_path.read_text(encoding="utf-8")
    assert "<canvas" in preview
    assert "point-cloud alignment 3D" in preview
    assert "sampled ground cloud" in preview
    assert "sampled building cloud" in preview
    assert "voxel-grid subsampled" in preview
    assert "nearest local ground elevation" in preview
    assert "does not exaggerate vertical scale" in preview
    assert "Drag to rotate the 3D view" in preview
    assert "Zoom in" in preview
    assert "Reset zoom" in preview
    assert "mouse wheel or zoom buttons" in preview
    assert "terrain-load buttons" in preview
    assert "Terrain load" in preview
    assert "Buildings And Footprints" in preview
    assert "Building load" in preview
    assert "buildingScene" in preview
    assert "DSM points classified as buildings" in preview
    assert "Light" in preview
    assert "Dense" in preview
    assert "estimated nearby roof Z" in preview
    assert "sampled unclassified DSM cloud" in preview
    assert "totalUnclassifiedPoints" in preview
    assert "unclassifiedPoints" in preview
    assert "valid DSM points not classified as buildings or trees" in preview
    assert "Buildings cloud load" in preview
    assert "Unclassified cloud load" in preview
    assert 'id="buildingsCloudLoadControls"' in preview
    assert 'id="unclassifiedCloudLoadControls"' in preview
    assert "scene.buildingsCloudSampleLevels" in preview
    assert "scene.unclassifiedCloudSampleLevels" in preview
    assert "activeBuildingsCloudSampleIndex" in preview
    assert "activeUnclassifiedCloudSampleIndex" in preview
    assert 'id="cloudLoadControls"' not in preview
    assert "scene.cloudSampleLevels" not in preview
    assert "<strong>Classified cloud load</strong>" not in preview
    assert preview.count('id="terrainCloudVisibilityToggle"') == 1
    assert preview.count('id="buildingsCloudVisibilityToggle"') == 1
    assert preview.count('id="unclassifiedCloudVisibilityToggle"') == 1
    assert 'aria-pressed="true">Hide terrain cloud</button>' in preview
    assert 'aria-pressed="true">Hide buildings cloud</button>' in preview
    assert 'aria-pressed="true">Hide unclassified cloud</button>' in preview
    assert "showTerrainCloud: true" in preview
    assert "showBuildingsCloud: true" in preview
    assert "showUnclassifiedCloud: true" in preview
    assert (
        'bindCloudVisibilityToggle("terrainCloudVisibilityToggle", '
        'views[0], "showTerrainCloud", "terrain cloud")'
        in preview
    )
    assert (
        'bindCloudVisibilityToggle("buildingsCloudVisibilityToggle", '
        'views[0], "showBuildingsCloud", "buildings cloud")'
        in preview
    )
    assert (
        'bindCloudVisibilityToggle("unclassifiedCloudVisibilityToggle", '
        'views[0], "showUnclassifiedCloud", "unclassified cloud")'
        in preview
    )
    assert "view[stateKey] = !view[stateKey];" in preview
    assert 'button.setAttribute("aria-pressed", String(visible));' in preview
    assert "views[0].activeTerrainSampleIndex = index;" in preview
    assert "views[0].activeBuildingsCloudSampleIndex = index;" in preview
    assert "views[0].activeUnclassifiedCloudSampleIndex = index;" in preview
    assert "view.showTerrainCloud ? terrainSamples.groundPoints.map" in preview
    assert (
        "view.showBuildingsCloud ? buildingsCloudSamples.buildingPoints.map"
        in preview
    )
    assert "view.showBuildingsCloud ? buildingsCloudSamples.treePoints.map" in preview
    assert (
        "view.showUnclassifiedCloud ? unclassifiedCloudSamples.unclassifiedPoints.map"
        in preview
    )
    assert 'view.mode === "buildings"' in preview


def test_voxel_grid_subsample_keeps_one_point_per_cell() -> None:
    points = [
        (0.1, 0.1, 1.0),
        (0.8, 0.7, 2.0),
        (2.1, 0.2, 3.0),
        (2.9, 0.1, 4.0),
        (4.4, 4.2, 5.0),
        (4.1, 4.8, 6.0),
    ]

    subsampled = point_cloud_rendering._voxel_grid_subsample_many(points, [2.0])[0]

    assert subsampled == [
        (0.8, 0.7, 2.0),
        (2.9, 0.1, 4.0),
        (4.1, 4.8, 6.0),
    ]


def test_preview_scene_includes_unclassified_points_in_bounds(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    config = load_config(config_path)

    scene = point_cloud_rendering.point_cloud_scene_data(
        config,
        building_polygons=[],
        ground_points=[(0.0, 0.0, 10.0)],
        building_points=[],
        tree_points=[],
        unclassified_points=[(0.0, 0.0, 30.0)],
        projected_bbox=(-8.0, -8.0, 8.0, 8.0),
    )

    assert scene["maxZ"] == 20.0
    assert scene["totalUnclassifiedPoints"] == 1
    assert "cloudSampleLevels" not in scene
    assert "defaultCloudSampleLevelIndex" not in scene
    assert scene["buildingsCloudSampleLevels"]
    assert scene["unclassifiedCloudSampleLevels"]
    assert all(
        level["totalLoadedPoints"] == len(level["buildingPoints"]) + len(level["treePoints"])
        for level in scene["buildingsCloudSampleLevels"]
    )
    assert all("unclassifiedPoints" not in level for level in scene["buildingsCloudSampleLevels"])
    assert all(
        level["totalLoadedPoints"] == len(level["unclassifiedPoints"])
        for level in scene["unclassifiedCloudSampleLevels"]
    )
    assert all("buildingPoints" not in level for level in scene["unclassifiedCloudSampleLevels"])
    assert any(level["unclassifiedPoints"] for level in scene["unclassifiedCloudSampleLevels"])


def test_multi_level_voxel_subsample_matches_individual_levels() -> None:
    points = [
        (0.1, 0.1, 1.0),
        (0.8, 0.7, 2.0),
        (2.1, 0.2, 3.0),
        (2.9, 0.1, 4.0),
        (4.4, 4.2, 5.0),
        (4.1, 4.8, 6.0),
    ]

    assert point_cloud_rendering._voxel_grid_subsample_many(points, [2.0, 4.0]) == [
        [
            (0.8, 0.7, 2.0),
            (2.9, 0.1, 4.0),
            (4.1, 4.8, 6.0),
        ],
        [
            (0.8, 0.7, 2.0),
            (4.1, 4.8, 6.0),
        ],
    ]


def test_local_surface_relief_compares_nearby_xy_z_values() -> None:
    rows = [
        [10.0, 10.0, 10.0],
        [10.0, 15.0, 15.0],
        [10.0, 15.0, 15.0],
    ]

    assert point_cloud._local_surface_relief(rows, 1, 1, radius_cells=1, nodata_value=-9999.0) == 5.0
    assert point_cloud._local_surface_relief(rows, 2, 2, radius_cells=1, nodata_value=-9999.0) == 0.0
    assert point_cloud._local_surface_relief(rows, 2, 2, radius_cells=2, nodata_value=-9999.0) == 5.0


def test_estimates_nearby_roof_z_from_building_points() -> None:
    roof_index: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for point in [(0.0, 0.0, 15.0), (2.0, 0.0, 15.2), (4.0, 0.0, 22.0), (20.0, 20.0, 40.0)]:
        roof_index.setdefault(point_cloud._roof_index_key(point[0], point[1]), []).append(point)

    assert point_cloud._estimate_nearby_roof_z(1.0, 0.0, 16.0, roof_index) == 15.0
    assert point_cloud._estimate_nearby_roof_z(1.0, 0.0, 22.0, roof_index) == 22.0
    assert point_cloud._estimate_nearby_roof_z(4.2, 0.0, 22.3, roof_index) == 22.0
    assert point_cloud._estimate_nearby_roof_z(4.2, 0.0, 27.0, roof_index) == 22.0
    assert point_cloud._estimate_nearby_roof_z(100.0, 100.0, 20.0, roof_index) is None


def test_roof_cluster_selection_prefers_candidate_height_cluster_over_nearer_lower_roof() -> None:
    roof_index: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for point in [
        (0.0, 0.0, 15.0),
        (1.0, 0.0, 15.1),
        (3.0, 0.0, 21.8),
        (4.0, 0.0, 22.0),
        (5.0, 0.0, 22.1),
    ]:
        roof_index.setdefault(point_cloud._roof_index_key(point[0], point[1]), []).append(point)

    assert point_cloud._estimate_nearby_roof_z(1.2, 0.0, 22.0, roof_index) == 22.0


def test_building_footprint_buffer_matches_near_edge_points() -> None:
    polygon = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0), (0.0, 0.0)]

    assert point_cloud._point_within_any_polygon_buffer((4.9, 2.0), [_polygon(polygon)], 1.5) is True
    assert point_cloud._point_within_any_polygon_buffer((6.0, 2.0), [_polygon(polygon)], 1.5) is False


def test_polygon_spatial_index_preserves_exact_geometry_results() -> None:
    polygons = [
        _polygon([(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0), (0.0, 0.0)]),
        _polygon([(40.0, 40.0), (44.0, 40.0), (44.0, 44.0), (40.0, 44.0), (40.0, 40.0)]),
    ]
    index = point_cloud.PolygonSpatialIndex.build(polygons, buffer_m=1.5, cell_size=8.0)
    points = [(-2.0, 2.0), (-1.5, 2.0), (2.0, 2.0), (5.4, 2.0), (6.0, 2.0), (42.0, 42.0)]

    for point in points:
        assert index.contains(point) is point_cloud._point_in_any_polygon(point, polygons)
        assert index.within_buffer(point, 1.5) is point_cloud._point_within_any_polygon_buffer(
            point,
            polygons,
            1.5,
        )


def test_polygon_holes_are_excluded_with_explicit_boundary_and_buffer_semantics() -> None:
    polygon = _polygon(
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)],
        holes=[[(3.0, 3.0), (7.0, 3.0), (7.0, 7.0), (3.0, 7.0), (3.0, 3.0)]],
    )
    index = point_cloud.PolygonSpatialIndex.build([polygon], buffer_m=1.0, cell_size=4.0)
    expected_contains = {
        (2.0, 5.0): True,
        (5.0, 5.0): False,
        (0.0, 5.0): True,
        (3.0, 5.0): True,
        (-1.0, 5.0): False,
    }

    for point, expected in expected_contains.items():
        assert point_cloud._point_in_any_polygon(point, [polygon]) is expected
        assert index.contains(point) is expected

    assert index.within_buffer((3.5, 5.0), 1.0) is True
    assert index.within_buffer((5.0, 5.0), 1.0) is False
    assert index.within_buffer((3.0, 5.0), 1.0) is True

    best_offset, score = point_cloud._estimate_horizontal_offset([(5.0, 5.0, 12.0)], [polygon])
    assert best_offset == (-2, 0)
    assert score == 1


def test_projected_multipolygon_preserves_component_and_hole_order() -> None:
    feature = {
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [
                [
                    [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0], [0.0, 0.0]],
                    [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0], [1.0, 1.0]],
                    [[3.0, 3.0], [3.5, 3.0]],
                ],
                [
                    [[10.0, 10.0], [14.0, 10.0], [14.0, 14.0], [10.0, 14.0], [10.0, 10.0]],
                    [[11.0, 11.0], [12.0, 11.0], [12.0, 12.0], [11.0, 12.0], [11.0, 11.0]],
                ],
            ],
        }
    }

    polygons = point_cloud._project_feature_polygon(feature)

    assert [polygon.exterior[0] for polygon in polygons] == [(0.0, 0.0), (10.0, 10.0)]
    assert [polygon.holes[0][0] for polygon in polygons] == [(1.0, 1.0), (11.0, 11.0)]


def test_elevated_courtyard_cell_is_alignment_evidence_not_building_output(tmp_path: Path) -> None:
    dtm_dir = tmp_path / "dtm"
    dsm_dir = tmp_path / "dsm"
    dtm_dir.mkdir()
    dsm_dir.mkdir()
    center_x = 500_000.0
    center_y = 4_850_000.0
    _write_flat_grid(dtm_dir / "tile.ASC", center_x, center_y, value=10.0)
    _write_single_peak_grid(dsm_dir / "tile.ASC", center_x, center_y)
    polygon = _polygon(
        [
            (center_x - 5.0, center_y - 5.0),
            (center_x + 5.0, center_y - 5.0),
            (center_x + 5.0, center_y + 5.0),
            (center_x - 5.0, center_y + 5.0),
            (center_x - 5.0, center_y - 5.0),
        ],
        holes=[
            [
                (center_x - 0.5, center_y - 0.5),
                (center_x + 0.5, center_y - 0.5),
                (center_x + 0.5, center_y + 0.5),
                (center_x - 0.5, center_y + 0.5),
                (center_x - 0.5, center_y - 0.5),
            ]
        ],
    )

    _ground, buildings, _trees, unclassified, candidates, _summary = point_cloud._points_from_rasters(
        dtm_directory=dtm_dir,
        dsm_directory=dsm_dir,
        bbox=(center_x - 8.0, center_y - 8.0, center_x + 8.0, center_y + 8.0),
        building_polygons=[polygon],
        tree_mask=None,
        tree_tag_points=[],
    )

    assert buildings == []
    assert len(unclassified) == 25
    assert (center_x, center_y, 15.0) in unclassified
    assert candidates == [(center_x, center_y, 15.0)]


def test_raster_scan_classifies_every_valid_dsm_point(tmp_path: Path) -> None:
    dtm_dir = tmp_path / "dtm"
    dsm_dir = tmp_path / "dsm"
    dtm_dir.mkdir()
    dsm_dir.mkdir()
    center_x = 500_000.0
    center_y = 4_850_000.0
    _write_flat_grid(dtm_dir / "tile.ASC", center_x, center_y, value=10.0)
    _write_single_peak_grid(dsm_dir / "tile.ASC", center_x, center_y)

    ground, buildings, trees, unclassified, candidates, _summary = point_cloud._points_from_rasters(
        dtm_directory=dtm_dir,
        dsm_directory=dsm_dir,
        bbox=(center_x - 8.0, center_y - 8.0, center_x + 8.0, center_y + 8.0),
        building_polygons=[],
        tree_mask=None,
        tree_tag_points=[],
    )

    assert buildings == []
    assert trees == []
    assert len(ground) == len(unclassified) == 25
    assert (center_x, center_y, 15.0) in unclassified
    assert candidates == [(center_x, center_y, 15.0)]
    assert len(ground) == len(buildings) + len(trees) + len(unclassified)


def test_alignment_diagnostics_uses_raw_shifted_elevated_candidates(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    config = load_config(config_path)
    footprint = [_polygon([(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5), (0.0, 0.0)])]

    def diagnostics(candidates: list[tuple[float, float, float]]) -> dict[str, object]:
        return point_cloud._build_alignment_diagnostics(
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


def test_raster_scan_collects_alignment_candidates_before_footprint_filtering(tmp_path: Path) -> None:
    dtm_dir = tmp_path / "dtm"
    dsm_dir = tmp_path / "dsm"
    dtm_dir.mkdir()
    dsm_dir.mkdir()
    center_x = 500_000.0
    center_y = 4_850_000.0
    _write_flat_grid(dtm_dir / "tile.ASC", center_x, center_y, value=10.0)
    _write_single_peak_grid(dsm_dir / "tile.ASC", center_x, center_y)
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    config = load_config(config_path)
    bbox = (center_x - 8.0, center_y - 8.0, center_x + 8.0, center_y + 8.0)

    def scan_and_diagnose(shift_m: float) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]], dict[str, object]]:
        footprint = [_polygon([
            (center_x - shift_m - 0.5, center_y - 0.5),
            (center_x - shift_m + 0.5, center_y - 0.5),
            (center_x - shift_m + 0.5, center_y + 0.5),
            (center_x - shift_m - 0.5, center_y + 0.5),
            (center_x - shift_m - 0.5, center_y - 0.5),
        ])]
        ground, buildings, trees, unclassified, candidates, raster_summary = point_cloud._points_from_rasters(
            dtm_directory=dtm_dir,
            dsm_directory=dsm_dir,
            bbox=bbox,
            building_polygons=footprint,
            tree_mask=None,
            tree_tag_points=[],
        )
        diagnostics = point_cloud._build_alignment_diagnostics(
            config=config,
            footprint_path=tmp_path / "buildings.geojson",
            building_polygons=footprint,
            ground_points=ground,
            building_points=buildings,
            alignment_candidate_points=candidates,
            tree_points=trees,
            unclassified_points=unclassified,
            raster_summary=raster_summary,
            tree_mask=None,
            tree_tag_points=[],
        )
        return buildings, candidates, diagnostics

    warning_buildings, warning_candidates, warning = scan_and_diagnose(3.0)
    failed_buildings, failed_candidates, failed = scan_and_diagnose(6.0)

    assert warning_buildings == []
    assert warning_candidates == [(center_x, center_y, 15.0)]
    assert warning["best_offset_m"] == {"x": 3, "y": 0}
    assert warning["alignment_status"] == "warning"
    assert failed_buildings == []
    assert failed_candidates == warning_candidates
    assert failed["best_offset_m"] == {"x": 6, "y": 0}
    assert failed["alignment_status"] == "failed"


def test_building_footprint_selection_requires_explicit_override(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    write_complete_config(config_path, output_root=output_root)
    config = load_config(config_path)
    stage1_path = output_root / "01_shapefiles" / "buildings.geojson"
    explicit_path = tmp_path / "accepted_buildings.geojson"
    dormant_path = output_root / "02_visual_enrichment" / "lod22_buildings.geojson"
    for path in (stage1_path, explicit_path, dormant_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    publish_test_stage_manifest(
        stage1_path.parent,
        stage="shapefiles",
        named_artifacts={"category-buildings": (stage1_path, ArtifactKind.HANDOFF)},
    )

    assert point_cloud._select_building_footprints_path(config) == stage1_path
    assert point_cloud._select_building_footprints_path(config, explicit_path) == explicit_path

    stage1_path.unlink()
    with pytest.raises(ConfigError, match="manifest missing required artifact.*buildings.geojson"):
        point_cloud._select_building_footprints_path(config)
    with pytest.raises(ConfigError, match="explicit building-footprint GeoJSON does not exist"):
        point_cloud._select_building_footprints_path(config, tmp_path / "missing.geojson")


def test_default_building_footprints_require_shapefiles_manifest_but_explicit_override_does_not(
    tmp_path: Path,
) -> None:
    config_path = write_complete_config(tmp_path / "config.toml", output_root=tmp_path / "outputs")
    config = load_config(config_path)
    stage1_path = tmp_path / "outputs" / "01_shapefiles" / "buildings.geojson"
    stage1_path.parent.mkdir(parents=True)
    stage1_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    explicit_path = tmp_path / "explicit.geojson"
    explicit_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")

    with pytest.raises(ConfigError, match=str(stage1_path.parent / "manifest.json")):
        point_cloud._select_building_footprints_path(config)

    assert point_cloud._select_building_footprints_path(config, explicit_path) == explicit_path


def test_explicit_footprints_do_not_consume_stale_stage1_tree_tags_without_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    outputs = tmp_path / "outputs"
    dtm_dir = tmp_path / "dtm"
    dsm_dir = tmp_path / "dsm"
    overlay_path = tmp_path / "tree_overlay.png"
    explicit_footprints = tmp_path / "inputs" / "explicit_buildings.geojson"
    dtm_dir.mkdir()
    dsm_dir.mkdir()
    center_lon = 11.2558
    center_lat = 43.7696
    center_x, center_y = point_cloud._lonlat_to_epsg25832(center_lon, center_lat)
    _write_grid(dtm_dir / "tile.ASC", center_x, center_y, elevated=False)
    _write_roof_with_tree_peak_grid(dsm_dir / "tile.ASC", center_x, center_y)
    _write_buildings(explicit_footprints, center_lon, center_lat)
    stale_trees = outputs / "01_shapefiles" / "trees.geojson"
    _write_trees(stale_trees, center_lon, center_lat, publish_manifest=False)
    _write_png(overlay_path, width=5, height=5, rgba=(10, 160, 35, 255))
    write_complete_config(
        config_path,
        output_root=outputs,
        name="Explicit Footprint Without Stage 1 Manifest",
        center_lat=center_lat,
        center_lon=center_lon,
        inner_diameter_m=12.0,
        outer_diameter_m=16.0,
        input_lines=(
            f'dtm_directory = "{dtm_dir.as_posix()}"',
            f'dsm_directory = "{dsm_dir.as_posix()}"',
            f'tree_canopy_overlay_path = "{overlay_path.as_posix()}"',
        ),
    )
    fingerprint_paths: list[Path] = []
    real_fingerprint = point_cloud.lightweight_state_fingerprint

    def capture_fingerprint(payload, paths):
        fingerprint_paths.extend(paths)
        return real_fingerprint(payload, paths)

    monkeypatch.setattr(point_cloud, "lightweight_state_fingerprint", capture_fingerprint)

    result = point_cloud.run(
        load_config(config_path),
        building_footprints_path=explicit_footprints,
    )

    diagnostics = json.loads(result.diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["tree_filter"]["tree_tag_point_count"] == 0
    assert stale_trees not in fingerprint_paths


def test_failed_point_cloud_qa_does_not_publish_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    dtm_dir = tmp_path / "dtm"
    dsm_dir = tmp_path / "dsm"
    dtm_dir.mkdir()
    dsm_dir.mkdir()
    center_lon = 11.2558
    center_lat = 43.7696
    center_x, center_y = point_cloud._lonlat_to_epsg25832(center_lon, center_lat)
    _write_grid(dtm_dir / "tile.ASC", center_x, center_y, elevated=False)
    _write_grid(dsm_dir / "tile.ASC", center_x, center_y, elevated=True)
    _write_buildings(output_root / "01_shapefiles" / "buildings.geojson", center_lon, center_lat)
    write_complete_config(
        config_path,
        output_root=output_root,
        center_lat=center_lat,
        center_lon=center_lon,
        inner_diameter_m=12.0,
        outer_diameter_m=16.0,
        input_lines=(
            f'dtm_directory = "{dtm_dir.as_posix()}"',
            f'dsm_directory = "{dsm_dir.as_posix()}"',
        ),
    )
    stage_dir = output_root / "03_point_cloud"
    manifest_path = stage_dir / "manifest.json"
    legacy_manifest_path = stage_dir / "city4cfd_point_cloud_manifest.json"
    stage_dir.mkdir(parents=True)
    manifest_path.write_text('{"stale":true}', encoding="utf-8")
    legacy_manifest_path.write_text('{"stale":true}', encoding="utf-8")
    monkeypatch.setattr(
        point_cloud,
        "render_preview_html",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("preview failed")),
    )

    with pytest.raises(RuntimeError, match="preview failed"):
        point_cloud.run(load_config(config_path))

    assert not manifest_path.exists()
    assert not legacy_manifest_path.exists()
    assert not (manifest_path.parent / ".stage.lock").exists()


def test_early_point_cloud_validation_failure_invalidates_old_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    write_complete_config(config_path, output_root=output_root)
    stage_dir = output_root / "03_point_cloud"
    manifest_path = stage_dir / "manifest.json"
    legacy_manifest_path = stage_dir / "city4cfd_point_cloud_manifest.json"
    stage_dir.mkdir(parents=True)
    manifest_path.write_text('{"stale":true}', encoding="utf-8")
    legacy_manifest_path.write_text('{"stale":true}', encoding="utf-8")

    def fail_validation(_config) -> None:
        raise ConfigError("input validation failed")

    monkeypatch.setattr(point_cloud, "_validate_inputs", fail_validation)

    with pytest.raises(ConfigError, match="input validation failed"):
        point_cloud.run(load_config(config_path))

    assert not manifest_path.exists()
    assert not legacy_manifest_path.exists()


def test_rejects_paired_grid_origin_mismatch_before_roi_skip(tmp_path: Path) -> None:
    dtm_dir = tmp_path / "dtm"
    dsm_dir = tmp_path / "dsm"
    dtm_dir.mkdir()
    dsm_dir.mkdir()
    _write_flat_grid(dtm_dir / "tile.ASC", 100.0, 100.0, value=10.0)
    _write_flat_grid(dsm_dir / "tile.ASC", 0.0, 0.0, value=15.0)

    with pytest.raises(ConfigError, match="DTM/DSM tile grid mismatch") as error:
        point_cloud._points_from_rasters(
            dtm_directory=dtm_dir,
            dsm_directory=dsm_dir,
            bbox=(-8.0, -8.0, 8.0, 8.0),
            building_polygons=[],
            tree_mask=None,
            tree_tag_points=[],
        )

    assert str(dtm_dir / "tile.ASC") in str(error.value)
    assert str(dsm_dir / "tile.ASC") in str(error.value)


def test_rejects_unmatched_raster_tiles_only_when_they_intersect_roi(tmp_path: Path) -> None:
    def raster_directories(name: str) -> tuple[Path, Path]:
        dtm_dir = tmp_path / name / "dtm"
        dsm_dir = tmp_path / name / "dsm"
        dtm_dir.mkdir(parents=True)
        dsm_dir.mkdir(parents=True)
        _write_flat_grid(dtm_dir / "tile.ASC", 0.0, 0.0, value=10.0)
        _write_flat_grid(dsm_dir / "tile.ASC", 0.0, 0.0, value=15.0)
        return dtm_dir, dsm_dir

    def scan(dtm_dir: Path, dsm_dir: Path):
        return point_cloud._points_from_rasters(
            dtm_directory=dtm_dir,
            dsm_directory=dsm_dir,
            bbox=(-8.0, -8.0, 8.0, 8.0),
            building_polygons=[],
            tree_mask=None,
            tree_tag_points=[],
        )

    dtm_in, dsm_in = raster_directories("dtm_in")
    _write_flat_grid(dtm_in / "dtm_only.ASC", 0.0, 0.0, value=10.0)
    with pytest.raises(ConfigError, match="unmatched DTM tile"):
        scan(dtm_in, dsm_in)

    dtm_dsm, dsm_dsm = raster_directories("dsm_in")
    _write_flat_grid(dsm_dsm / "dsm_only.ASC", 0.0, 0.0, value=15.0)
    with pytest.raises(ConfigError, match="unmatched DSM tile"):
        scan(dtm_dsm, dsm_dsm)

    dtm_out, dsm_out = raster_directories("outside")
    _write_flat_grid(dtm_out / "dtm_only.ASC", 100.0, 100.0, value=10.0)
    _write_flat_grid(dsm_out / "dsm_only.ASC", -100.0, -100.0, value=15.0)
    ground, buildings, trees, unclassified, candidates, summary = scan(dtm_out, dsm_out)

    assert len(ground) == 25
    assert buildings == []
    assert trees == []
    assert len(unclassified) == 25
    assert len(candidates) == 25
    assert summary["paired_tile_count"] == 1


def test_roof_offset_tree_candidate_is_removed_from_building_cloud(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    outputs = tmp_path / "outputs"
    dtm_dir = tmp_path / "dtm"
    dsm_dir = tmp_path / "dsm"
    overlay_path = tmp_path / "stage1_tree_overlay.png"
    dtm_dir.mkdir()
    dsm_dir.mkdir()
    center_lon = 11.2558
    center_lat = 43.7696
    center_x, center_y = point_cloud._lonlat_to_epsg25832(center_lon, center_lat)
    tree_lon = center_lon
    tree_x, _tree_y = point_cloud._lonlat_to_epsg25832(tree_lon, center_lat)
    _write_grid(dtm_dir / "tile.ASC", center_x, center_y, elevated=False)
    _write_roof_with_tree_peak_grid(dsm_dir / "tile.ASC", center_x, center_y)
    _write_buildings(outputs / "01_shapefiles" / "buildings.geojson", center_lon, center_lat)
    _write_trees(outputs / "01_shapefiles" / "trees.geojson", tree_lon, center_lat)
    _write_png(overlay_path, width=5, height=5, rgba=(10, 160, 35, 255))
    write_complete_config(
        config_path,
        output_root=outputs,
        name="Point Cloud Tree Fixture",
        center_lat=center_lat,
        center_lon=center_lon,
        inner_diameter_m=12.0,
        outer_diameter_m=16.0,
        input_lines=(
            f'dtm_directory = "{dtm_dir.as_posix()}"',
            f'dsm_directory = "{dsm_dir.as_posix()}"',
            f'tree_canopy_overlay_path = "{overlay_path.as_posix()}"',
        ),
    )

    result = point_cloud.run(load_config(config_path))

    assert result.tree_points_path is not None
    assert result.tree_points_path.exists()
    assert result.tree_point_count > 0
    assert result.building_point_count > 0
    tree_vertices = _ply_vertices(result.tree_points_path)
    building_vertices = _ply_vertices(result.building_points_path)
    assert all(abs(vertex[0] - tree_x) <= point_cloud.TREE_TAG_ASSOCIATION_RADIUS_M for vertex in tree_vertices)
    assert all(vertex not in tree_vertices for vertex in building_vertices)

    diagnostics = json.loads(result.diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["tree_filter"]["enabled"] is True
    assert diagnostics["tree_filter"]["tree_tag_point_count"] == 1
    assert diagnostics["tree_filter"]["counts"]["building_footprint_candidate_count"] > 0
    assert diagnostics["tree_filter"]["counts"]["roof_offset_pass_count"] == result.tree_point_count
    assert diagnostics["tree_filter"]["roof_offset_threshold_m"] == point_cloud.TREE_ROOF_OFFSET_THRESHOLD_M
    assert diagnostics["tree_filter"]["building_footprint_buffer_m"] == point_cloud.TREE_BUILDING_FOOTPRINT_BUFFER_M
    assert diagnostics["tree_point_count"] == result.tree_point_count

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    artifacts = {artifact["name"]: artifact for artifact in manifest["artifacts"]}
    assert artifacts["tree-points"] == {
        "name": "tree-points",
        "path": str(result.tree_points_path),
        "kind": "handoff",
        "required": False,
    }
    assert manifest["metrics"]["tree_point_count"] == result.tree_point_count

    preview = result.preview_path.read_text(encoding="utf-8")
    assert "filtered tree DSM points" in preview
    assert "stage-1 natural=tree tags" in preview


def test_flat_green_roof_near_tree_tag_stays_building(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    outputs = tmp_path / "outputs"
    dtm_dir = tmp_path / "dtm"
    dsm_dir = tmp_path / "dsm"
    overlay_path = tmp_path / "stage1_tree_overlay.png"
    dtm_dir.mkdir()
    dsm_dir.mkdir()
    center_lon = 11.2558
    center_lat = 43.7696
    center_x, center_y = point_cloud._lonlat_to_epsg25832(center_lon, center_lat)
    _write_grid(dtm_dir / "tile.ASC", center_x, center_y, elevated=False)
    _write_flat_grid(dsm_dir / "tile.ASC", center_x, center_y, value=15)
    _write_buildings(outputs / "01_shapefiles" / "buildings.geojson", center_lon, center_lat)
    _write_trees(outputs / "01_shapefiles" / "trees.geojson", center_lon, center_lat)
    _write_png(overlay_path, width=5, height=5, rgba=(10, 160, 35, 255))
    write_complete_config(
        config_path,
        output_root=outputs,
        name="Point Cloud Flat Roof Fixture",
        center_lat=center_lat,
        center_lon=center_lon,
        inner_diameter_m=12.0,
        outer_diameter_m=16.0,
        input_lines=(
            f'dtm_directory = "{dtm_dir.as_posix()}"',
            f'dsm_directory = "{dsm_dir.as_posix()}"',
            f'tree_canopy_overlay_path = "{overlay_path.as_posix()}"',
        ),
    )

    result = point_cloud.run(load_config(config_path))

    assert result.tree_point_count == 0
    assert result.building_point_count > 0
    diagnostics = json.loads(result.diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["tree_filter"]["roof_offset_threshold_m"] == point_cloud.TREE_ROOF_OFFSET_THRESHOLD_M
    assert diagnostics["tree_filter"]["counts"]["roof_estimate_candidate_count"] > 0
    assert diagnostics["tree_filter"]["counts"]["roof_offset_pass_count"] == 0


def test_tree_tag_inside_building_filters_only_when_roof_offset_passes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    outputs = tmp_path / "outputs"
    dtm_dir = tmp_path / "dtm"
    dsm_dir = tmp_path / "dsm"
    overlay_path = tmp_path / "gray_overlay.png"
    dtm_dir.mkdir()
    dsm_dir.mkdir()
    center_lon = 11.2558
    center_lat = 43.7696
    center_x, center_y = point_cloud._lonlat_to_epsg25832(center_lon, center_lat)
    _write_grid(dtm_dir / "tile.ASC", center_x, center_y, elevated=False)
    _write_roof_with_tree_peak_grid(dsm_dir / "tile.ASC", center_x, center_y)
    _write_buildings(outputs / "01_shapefiles" / "buildings.geojson", center_lon, center_lat)
    _write_trees(outputs / "01_shapefiles" / "trees.geojson", center_lon, center_lat)
    _write_png(overlay_path, width=5, height=5, rgba=(120, 120, 120, 255))
    write_complete_config(
        config_path,
        output_root=outputs,
        name="Point Cloud Tagged Nonvegetation Fixture",
        center_lat=center_lat,
        center_lon=center_lon,
        inner_diameter_m=12.0,
        outer_diameter_m=16.0,
        input_lines=(
            f'dtm_directory = "{dtm_dir.as_posix()}"',
            f'dsm_directory = "{dsm_dir.as_posix()}"',
            f'tree_canopy_overlay_path = "{overlay_path.as_posix()}"',
        ),
    )

    result = point_cloud.run(load_config(config_path))

    assert result.tree_point_count == 1
    assert result.building_point_count > 0
    diagnostics = json.loads(result.diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["tree_filter"]["counts"]["evidence_candidate_count"] > 0
    assert diagnostics["tree_filter"]["counts"]["roof_offset_pass_count"] == 1
    assert diagnostics["tree_filter"]["canopy_mask_search_radius_px"] == point_cloud.TREE_CANOPY_MASK_SEARCH_RADIUS_PX
    assert diagnostics["tree_filter"]["excess_green_threshold"] == point_cloud.TREE_EXCESS_GREEN_THRESHOLD
    assert diagnostics["tree_filter"]["min_green_channel"] == point_cloud.TREE_MIN_GREEN_CHANNEL


def _polygon(
    exterior: list[tuple[float, float]],
    holes: list[list[tuple[float, float]]] | None = None,
) -> point_cloud.ProjectedPolygon:
    return point_cloud.ProjectedPolygon(
        exterior=tuple(exterior),
        holes=tuple(tuple(hole) for hole in (holes or [])),
    )


def _write_grid(path: Path, center_x: float, center_y: float, elevated: bool) -> None:
    values = []
    for row in range(5):
        row_values = []
        for col in range(5):
            is_center = 1 <= row <= 3 and 1 <= col <= 3
            row_values.append("15" if elevated and is_center else "10")
        values.append(" ".join(row_values))
    path.write_text(
        "\n".join(
            [
                "ncols 5",
                "nrows 5",
                f"xllcorner {center_x - 5}",
                f"yllcorner {center_y - 5}",
                "cellsize 2",
                "NODATA_value -9999",
                *values,
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_flat_grid(path: Path, center_x: float, center_y: float, value: float) -> None:
    rows = [" ".join(str(value) for _col in range(5)) for _row in range(5)]
    path.write_text(
        "\n".join(
            [
                "ncols 5",
                "nrows 5",
                f"xllcorner {center_x - 5}",
                f"yllcorner {center_y - 5}",
                "cellsize 2",
                "NODATA_value -9999",
                *rows,
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_roof_with_tree_peak_grid(path: Path, center_x: float, center_y: float) -> None:
    rows = []
    for row in range(5):
        values = []
        for col in range(5):
            values.append("20" if row == 2 and col == 2 else "15")
        rows.append(" ".join(values))
    path.write_text(
        "\n".join(
            [
                "ncols 5",
                "nrows 5",
                f"xllcorner {center_x - 5}",
                f"yllcorner {center_y - 5}",
                "cellsize 2",
                "NODATA_value -9999",
                *rows,
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_single_peak_grid(path: Path, center_x: float, center_y: float) -> None:
    rows = []
    for row in range(5):
        values = ["15" if row == 2 and col == 2 else "10" for col in range(5)]
        rows.append(" ".join(values))
    path.write_text(
        "\n".join(
            [
                "ncols 5",
                "nrows 5",
                f"xllcorner {center_x - 5}",
                f"yllcorner {center_y - 5}",
                "cellsize 2",
                "NODATA_value -9999",
                *rows,
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_buildings(path: Path, center_lon: float, center_lat: float) -> None:
    path.parent.mkdir(parents=True)
    ring = [
        [center_lon - 0.00004, center_lat - 0.00003],
        [center_lon + 0.00004, center_lat - 0.00003],
        [center_lon + 0.00004, center_lat + 0.00003],
        [center_lon - 0.00004, center_lat + 0.00003],
        [center_lon - 0.00004, center_lat - 0.00003],
    ]
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": [ring]},
                        "properties": {
                            "category": "buildings",
                            "contributes_to_geometry": True,
                            "building_base_height_m": 0.0,
                            "tags": {"building": "yes", "building:levels": "3", "roof:shape": "hipped"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    if path.parent.name == "01_shapefiles":
        publish_test_stage_manifest(
            path.parent,
            stage="shapefiles",
            named_artifacts={"category-buildings": (path, ArtifactKind.HANDOFF)},
        )


def _write_east_building(path: Path, center_lon: float, center_lat: float) -> None:
    path.parent.mkdir(parents=True)
    ring = [
        [center_lon + 0.000025, center_lat - 0.00003],
        [center_lon + 0.00008, center_lat - 0.00003],
        [center_lon + 0.00008, center_lat + 0.00003],
        [center_lon + 0.000025, center_lat + 0.00003],
        [center_lon + 0.000025, center_lat - 0.00003],
    ]
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": [ring]},
                        "properties": {
                            "category": "buildings",
                            "contributes_to_geometry": True,
                            "include_in_building_lod22_reconstruction": True,
                            "tags": {"building": "yes"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_trees(
    path: Path,
    center_lon: float,
    center_lat: float,
    *,
    publish_manifest: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [center_lon, center_lat]},
                        "properties": {"category": "trees", "tags": {"natural": "tree"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    if publish_manifest and path.parent.name == "01_shapefiles":
        named_artifacts = {"category-trees": (path, ArtifactKind.HANDOFF)}
        buildings_path = path.parent / "buildings.geojson"
        if buildings_path.is_file():
            named_artifacts["category-buildings"] = (buildings_path, ArtifactKind.HANDOFF)
        publish_test_stage_manifest(
            path.parent,
            stage="shapefiles",
            named_artifacts=named_artifacts,
        )


def _write_png(path: Path, width: int, height: int, rgba: tuple[int, int, int, int]) -> None:
    row = bytes([0] + list(rgba) * width)
    raw = row * height
    chunks = [
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
        _png_chunk(b"IDAT", zlib.compress(raw)),
        _png_chunk(b"IEND", b""),
    ]
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"".join(chunks))


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def _ply_vertices(path: Path) -> list[tuple[float, float, float]]:
    vertices: list[tuple[float, float, float]] = []
    in_data = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if in_data:
            x_str, y_str, z_str = line.split()
            vertices.append((float(x_str), float(y_str), float(z_str)))
        elif line.strip() == "end_header":
            in_data = True
    return vertices
