import json
from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.stages.point_cloud import stage
from tests.config_helpers import write_complete_config


def _ply(path: Path, vertices: str, count: int = 1) -> None:
    path.write_text(
        f"ply\nformat ascii 1.0\nelement vertex {count}\n"
        "property double x\nproperty double y\nproperty double z\nend_header\n" + vertices,
        encoding="utf-8",
    )


def _config(tmp_path: Path):
    _ply(tmp_path / "ground.ply", "15 0 10.125\n16 0 20\n", 2)
    _ply(tmp_path / "building.ply", "15 0 25.875\n")
    return load_config(
        write_complete_config(
            tmp_path / "config.toml",
            center_lon=15.0,
            center_lat=0.0,
            crs="EPSG:32633",
            input_lines=(
                'ground_point_cloud_path = "ground.ply"',
                'building_point_cloud_path = "building.ply"',
                'point_cloud_source_crs = "EPSG:4326"',
            ),
        )
    )


def _footprints(tmp_path: Path) -> Path:
    path = tmp_path / "buildings.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [14.9999, -0.0001],
                                    [15.0001, -0.0001],
                                    [15.0001, 0.0001],
                                    [14.9999, 0.0001],
                                    [14.9999, -0.0001],
                                ]
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_supplied_clouds_transform_xy_clip_roi_and_preserve_heights(tmp_path: Path) -> None:
    result = stage.run(_config(tmp_path), building_footprints_path=_footprints(tmp_path))
    assert result.ground_point_count == result.building_point_count == 1
    assert result.ground_points_path.read_text().split("end_header\n")[1] == "500000.000 0.000 10.125\n"
    assert result.building_points_path.read_text().split("end_header\n")[1] == "500000.000 0.000 25.875\n"
    projected = json.loads(result.projected_footprints_path.read_text())
    assert projected["crs"]["properties"]["name"] == "EPSG:32633"
    assert projected["features"][0]["geometry"]["coordinates"][0][0] == pytest.approx(
        [499988.8725, -11.0530],
        abs=0.001,
    )
    diagnostics = json.loads(result.diagnostics_path.read_text())
    assert diagnostics["input_mode"] == "supplied_clouds"
    assert diagnostics["dsm_classification_complete"] is None
    assert diagnostics["crs"]["point_cloud_source"] == "EPSG:4326"
    assert "user-classified" in diagnostics["alignment_evidence"]
    assert result.details["input_mode"] == "supplied_clouds"
    assert "user-classified" in result.preview_path.read_text()
    assert "filtered tree DSM points" not in result.preview_path.read_text()
    assert "sampled unclassified DSM cloud" not in result.preview_path.read_text()
    assert "not applicable" in result.report_path.read_text()


@pytest.mark.parametrize(
    "vertices,count,error",
    [
        ("nan 0 1\n", 1, "finite"),
        ("15 0 inf\n", 1, "finite"),
        ("15 0\n", 1, "vertex"),
        ("15 0 1\n", 2, "vertex"),
        ("15 0 1\n15 0 2\n", 1, "unexpected"),
    ],
)
def test_malformed_cloud_fails_without_publishing_manifest(
    tmp_path: Path, vertices: str, count: int, error: str
) -> None:
    config = _config(tmp_path)
    _ply(tmp_path / "ground.ply", vertices, count)
    with pytest.raises(ConfigError, match=error):
        stage.run(config, building_footprints_path=_footprints(tmp_path))
    assert not (config.output.root_directory / "03_point_cloud/manifest.json").exists()


def test_runtime_rechecks_raster_sidecar_before_reading_values(tmp_path: Path) -> None:
    for name in ("dtm", "dsm"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "tile.asc").write_text("not a grid", encoding="utf-8")
        (folder / "tile.prj").write_text("EPSG:25832", encoding="utf-8")
    config = load_config(
        write_complete_config(
            tmp_path / "config.toml",
            input_lines=(
                'dtm_directory = "dtm"',
                'dsm_directory = "dsm"',
            ),
        )
    )
    (tmp_path / "dtm/tile.prj").write_text("EPSG:32633", encoding="utf-8")
    with pytest.raises(ConfigError, match="working CRS"):
        stage.run(config, building_footprints_path=_footprints(tmp_path))


def test_sidecar_changes_affect_point_cloud_fingerprint(tmp_path: Path) -> None:
    folder = tmp_path / "dtm"
    folder.mkdir()
    (folder / "tile.asc").write_text("", encoding="utf-8")
    sidecar = folder / "tile.prj"
    sidecar.write_text("EPSG:25832", encoding="utf-8")
    config = load_config(write_complete_config(tmp_path / "config.toml", input_lines=('dtm_directory = "dtm"', 'dsm_directory = "dtm"')))
    footprints = _footprints(tmp_path)
    before = stage._point_cloud_input_fingerprint(config, footprints, None)
    sidecar.write_text("EPSG:25832\n", encoding="utf-8")
    assert before != stage._point_cloud_input_fingerprint(config, footprints, None)
