from __future__ import annotations

import json
from pathlib import Path

import pytest

from cities_reconstruction.stages.city_models.inputs import (
    point_cloud_cell_stats,
    read_feature_collection,
    read_json_object,
)


def test_read_feature_collection_keeps_polygon_inputs_without_reapplying_handoff_policy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "footprints.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": []},
                        "properties": {
                            "name": "polygon",
                            "contributes_to_geometry": False,
                        },
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "MultiPolygon", "coordinates": []},
                        "properties": {"name": "multipolygon"},
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
                        "properties": {"name": "point"},
                    },
                    "not-a-feature",
                ],
            }
        ),
        encoding="utf-8",
    )

    features = read_feature_collection(path)

    assert [feature["properties"]["name"] for feature in features] == [
        "polygon",
        "multipolygon",
    ]


def test_read_feature_collection_requires_features_list(tmp_path: Path) -> None:
    path = tmp_path / "invalid.geojson"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="feature collection missing features list"):
        read_feature_collection(path)


def test_read_json_object_rejects_non_object_root(tmp_path: Path) -> None:
    path = tmp_path / "diagnostics.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="expected JSON object"):
        read_json_object(path)


def test_point_cloud_cell_stats_aggregates_two_metre_cells(tmp_path: Path) -> None:
    path = tmp_path / "points.ply"
    path.write_text(
        "\n".join(
            (
                "ply",
                "format ascii 1.0",
                "element vertex 4",
                "property float x",
                "property float y",
                "property float z",
                "end_header",
                "0.1 0.1 8.0",
                "1.9 1.9 5.0",
                "2.0 -0.1 7.0 extra",
                "malformed row",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert point_cloud_cell_stats(path, prefer="min") == {
        (0, 0): 5.0,
        (1, -1): 7.0,
    }
    assert point_cloud_cell_stats(path, prefer="max") == {
        (0, 0): 8.0,
        (1, -1): 7.0,
    }


def test_point_cloud_cell_stats_rejects_unknown_preference(tmp_path: Path) -> None:
    path = tmp_path / "points.ply"

    with pytest.raises(ValueError, match="prefer must be either 'min' or 'max'"):
        point_cloud_cell_stats(path, prefer="median")
