from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import shape

from cities_reconstruction.adapters.city4cfd import City4CFDExecutionResult
from cities_reconstruction.config import ConfigError
from cities_reconstruction.stages.city_models.geometry import (
    building_preview_triangles,
    clip_surface_layer_features,
    project_surface_layer_feature,
    terrain_preview_triangles,
    validate_successful_city4cfd_geometry,
)


def _polygon_feature(
    coordinates: list[list[float]],
    **properties: object,
) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [coordinates]},
        "properties": properties,
    }


def _execution(status: str) -> City4CFDExecutionResult:
    return City4CFDExecutionResult(
        status=status,
        backend="native",
        argv=("city4cfd",),
        return_code=0,
        stdout="",
        stderr="",
    )


def test_project_surface_layer_feature_projects_lonlat_and_records_crs(tmp_path: Path) -> None:
    feature = _polygon_feature(
        [
            [11.2558, 43.7696],
            [11.2559, 43.7696],
            [11.2559, 43.7697],
            [11.2558, 43.7696],
        ],
        category="roads",
    )

    projected = project_surface_layer_feature(
        feature,
        target_crs="EPSG:25832",
        source_path=tmp_path / "roads.geojson",
    )

    first = projected["geometry"]["coordinates"][0][0]
    assert first == pytest.approx([681557.25, 4848756.39], abs=0.02)
    assert projected["properties"] == {
        "category": "roads",
        "source_crs": "EPSG:4326",
        "projected_crs": "EPSG:25832",
    }


def test_project_surface_layer_feature_rejects_non_lonlat_coordinates(tmp_path: Path) -> None:
    feature = _polygon_feature(
        [[681000.0, 4849000.0], [681001.0, 4849000.0], [681000.0, 4849000.0]],
    )
    source_path = tmp_path / "roads.geojson"

    with pytest.raises(ConfigError, match="must be EPSG:4326 lon/lat") as error:
        project_surface_layer_feature(
            feature,
            target_crs="EPSG:25832",
            source_path=source_path,
        )

    assert str(source_path) in str(error.value)


def test_clip_surface_layer_features_intersects_outer_region() -> None:
    feature = _polygon_feature(
        [[-2.0, -2.0], [2.0, -2.0], [2.0, 2.0], [-2.0, 2.0], [-2.0, -2.0]],
        category="roads",
    )

    clipped = clip_surface_layer_features([feature], center_xy=(0.0, 0.0), radius_m=1.0)

    assert len(clipped) == 1
    assert shape(clipped[0]["geometry"]).bounds == pytest.approx((-1.0, -1.0, 1.0, 1.0))
    assert clipped[0]["properties"] == {
        "category": "roads",
        "clipped_to_outer_region": True,
    }


def test_building_preview_triangles_use_point_cloud_elevations_and_lod22_roof() -> None:
    feature = _polygon_feature(
        [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]],
        roof_shape="gabled",
    )

    triangles = building_preview_triangles(
        [feature],
        ground_elevation_index={(0, 0): 10.0},
        building_roof_index={(0, 0): 15.0},
    )

    labels = [triangle[0] for triangle in triangles]
    assert labels.count("building_1_wall") == 8
    assert labels.count("building_1_roof") == 2
    assert labels.count("building_1_lod22_roof") == 4
    assert {point[2] for label, *points in triangles if label == "building_1_lod22_roof" for point in points} == {
        15.0,
        16.5,
    }


def test_terrain_preview_triangles_use_median_ground_elevation() -> None:
    triangles = terrain_preview_triangles(
        region_bbox=(0.0, 1.0, 4.0, 5.0),
        features=[],
        ground_elevation_index={(0, 0): 8.0, (1, 0): 10.0, (2, 0): 30.0},
    )

    assert triangles == [
        ("terrain", (0.0, 1.0, 10.0), (4.0, 1.0, 10.0), (4.0, 5.0, 10.0)),
        ("terrain", (0.0, 1.0, 10.0), (4.0, 5.0, 10.0), (0.0, 5.0, 10.0)),
    ]


def test_validate_successful_city4cfd_geometry_rejects_empty_output(tmp_path: Path) -> None:
    empty_path = tmp_path / "Mesh_Buildings.obj"
    empty_path.touch()

    with pytest.raises(ConfigError, match="reported success.*missing or empty") as error:
        validate_successful_city4cfd_geometry(
            _execution("native_succeeded"),
            (empty_path, None),
        )

    assert str(empty_path) in str(error.value)
    assert "<unresolved>" in str(error.value)


def test_validate_successful_city4cfd_geometry_ignores_unsuccessful_execution(
    tmp_path: Path,
) -> None:
    validate_successful_city4cfd_geometry(
        _execution("external_failed"),
        (tmp_path / "missing.obj",),
    )
