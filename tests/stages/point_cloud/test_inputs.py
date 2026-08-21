from __future__ import annotations

import json
from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError
from cities_reconstruction.stages.point_cloud.inputs import (
    paired_tile_cells,
    read_ascii_grid_values,
    read_feature_collection,
    tiles_by_name,
)


def test_read_feature_collection_requires_features_list(tmp_path: Path) -> None:
    path = tmp_path / "invalid.geojson"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="feature collection missing features list"):
        read_feature_collection(path)


def test_read_feature_collection_keeps_only_geometry_contributors(tmp_path: Path) -> None:
    path = tmp_path / "buildings.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": []},
                        "properties": {"name": "included"},
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": []},
                        "properties": {"contributes_to_geometry": False},
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
                        "properties": {},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    features = read_feature_collection(path)

    assert [feature["properties"]["name"] for feature in features] == ["included"]


def test_ascii_grid_reader_normalizes_center_origin_and_skips_nodata(tmp_path: Path) -> None:
    raster_dir = tmp_path / "rasters"
    raster_dir.mkdir()
    path = raster_dir / "tile.asc"
    path.write_text(
        "\n".join(
            (
                "ncols 2",
                "nrows 2",
                "xllcenter 1",
                "yllcenter 1",
                "cellsize 2",
                "NODATA_value -9999",
                "1 2",
                "3 -9999",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    tile = tiles_by_name(raster_dir)["tile.asc"]
    rows = read_ascii_grid_values(tile)

    assert (tile.xllcorner, tile.yllcorner, tile.max_x, tile.max_y) == (0.0, 0.0, 4.0, 4.0)
    assert list(paired_tile_cells(tile, tile, rows, rows)) == [
        (1.0, 3.0, 1.0, 1.0, 0, 0),
        (3.0, 3.0, 2.0, 2.0, 0, 1),
        (1.0, 1.0, 3.0, 3.0, 1, 0),
    ]


def test_tiles_by_name_rejects_case_insensitive_duplicate_basenames(tmp_path: Path) -> None:
    raster_dir = tmp_path / "rasters"
    first = raster_dir / "a" / "tile.ASC"
    second = raster_dir / "b" / "TILE.asc"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    grid = "\n".join(
        (
            "ncols 1",
            "nrows 1",
            "xllcorner 0",
            "yllcorner 0",
            "cellsize 1",
            "NODATA_value -9999",
            "1",
        )
    )
    first.write_text(grid, encoding="utf-8")
    second.write_text(grid, encoding="utf-8")

    with pytest.raises(ConfigError, match="duplicate ASCII grid basename") as error:
        tiles_by_name(raster_dir)

    assert str(first) in str(error.value)
    assert str(second) in str(error.value)
