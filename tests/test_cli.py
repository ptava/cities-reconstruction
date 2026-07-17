from __future__ import annotations

import json
from pathlib import Path
import struct
from types import SimpleNamespace
import zlib

import pytest

from cities_reconstruction.cli import main
from cities_reconstruction.stages import air_purifiers, point_cloud
from tests.config_helpers import write_complete_config


ROOT = Path(__file__).resolve().parents[1]


def test_validate_config_command(capsys) -> None:
    exit_code = main(["validate-config", "--config", str(ROOT / "config/examples/florence.toml")])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Configuration is valid" in captured.out


def test_dry_run_json_command(capsys) -> None:
    exit_code = main(
        [
            "dry-run",
            "--config",
            str(ROOT / "config/examples/florence.toml"),
            "--stage",
            "openfoam",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"stage": "openfoam"' in captured.out


def test_missing_config_returns_configuration_error(capsys) -> None:
    exit_code = main(["validate-config", "--config", "missing.toml"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Configuration error" in captured.err


def test_incomplete_config_returns_configuration_error(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "incomplete.toml"
    config_path.write_text(
        """
[region]
name = "Incomplete"
center_lat = 43.7696
center_lon = 11.2558
crs = "EPSG:25832"
inner_diameter_m = 200.0
outer_diameter_m = 400.0

[output]
root_directory = "outputs"
""".strip(),
        encoding="utf-8",
    )

    exit_code = main(["validate-config", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Configuration error" in captured.err
    assert "missing required [inputs] table" in captured.err


def test_config_argument_is_required(capsys) -> None:
    try:
        main(["validate-config"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("validate-config without --config should exit")

    captured = capsys.readouterr()
    assert "--config" in captured.err


def test_run_stage_shapefiles_with_cached_overpass_json(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(config_path, output_root=tmp_path / "outputs", name="CLI Fixture")
    raw_path.write_text('{"elements": []}', encoding="utf-8")

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "shapefiles",
            "--overpass-json",
            str(raw_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Feature Retrieval Report" in captured.out
    assert "Accepted features" in captured.out


def test_run_stage_shapefiles_accepts_supplemental_shapefile_overrides(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    streets_path = tmp_path / "streets.shp"
    green_path = tmp_path / "green.shp"
    write_complete_config(config_path, output_root=tmp_path / "outputs", name="CLI Surface Fixture")
    captured_configs: list[object] = []

    def fake_run(config, overpass_json_path=None):
        captured_configs.append(config)
        return SimpleNamespace(to_dict=lambda: {"status": "ok"})

    monkeypatch.setattr("cities_reconstruction.cli.shapefiles.run", fake_run)

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "shapefiles",
            "--streets-shapefile",
            streets_path.name,
            "--streets-shapefile-crs",
            "EPSG:3003",
            "--green-areas-shapefile",
            green_path.name,
            "--green-areas-shapefile-crs",
            "EPSG:4326",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"status": "ok"' in captured.out
    shapefiles_config = captured_configs[0].shapefiles
    surfaces = {surface.name: surface for surface in shapefiles_config.supplemental}
    assert surfaces["streets"].path == streets_path
    assert surfaces["streets"].crs == "EPSG:3003"
    assert surfaces["green_areas"].path == green_path
    assert surfaces["green_areas"].crs == "EPSG:4326"
    assert shapefiles_config.surface_precedence.index("supplemental:green_areas") < shapefiles_config.surface_precedence.index("green_areas")
    assert shapefiles_config.surface_precedence.index("supplemental:streets") < shapefiles_config.surface_precedence.index("roads")


def test_run_stage_visual_enrichment_with_segmentation_geojson(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    stage1_dir = output_root / "01_shapefiles"
    stage1_dir.mkdir(parents=True)
    (stage1_dir / "all_features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}),
        encoding="utf-8",
    )
    (stage1_dir / "imagery_diagnostics.json").write_text(
        json.dumps({"bbox_lon_lat": [11.254, 43.768, 11.258, 43.772], "sources": []}),
        encoding="utf-8",
    )
    segmentation_path = tmp_path / "segmentation.geojson"
    segmentation_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": []}),
        encoding="utf-8",
    )
    sat2lod2_path = tmp_path / "sat2lod2.geojson"
    sat2lod2_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": []}),
        encoding="utf-8",
    )
    write_complete_config(config_path, output_root=output_root, name="CLI Visual Fixture")

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "visual-enrichment",
            "--segmentation-geojson",
            str(segmentation_path),
            "--sat2lod2-geojson",
            str(sat2lod2_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"candidate_count": 0' in captured.out
    assert '"sat2lod2_feature_count": 0' in captured.out
    assert "segmentation_diagnostics_path" in captured.out


def test_run_stage_point_cloud_accepts_tree_overlay_argument(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    dtm_dir = tmp_path / "dtm"
    dsm_dir = tmp_path / "dsm"
    overlay_path = tmp_path / "tree_overlay.png"
    dtm_dir.mkdir()
    dsm_dir.mkdir()
    center_lon = 11.2558
    center_lat = 43.7696
    center_x, center_y = point_cloud._lonlat_to_epsg25832(center_lon, center_lat)
    _write_grid(dtm_dir / "tile.ASC", center_x, center_y, elevated=False)
    _write_roof_with_tree_peak_grid(dsm_dir / "tile.ASC", center_x, center_y)
    _write_buildings(output_root / "01_shapefiles" / "buildings.geojson", center_lon, center_lat)
    _write_trees(output_root / "01_shapefiles" / "trees.geojson", center_lon, center_lat)
    _write_png(overlay_path, width=5, height=5, rgba=(10, 160, 35, 255))
    write_complete_config(
        config_path,
        output_root=output_root,
        name="CLI Point Cloud Fixture",
        center_lat=center_lat,
        center_lon=center_lon,
        inner_diameter_m=12.0,
        outer_diameter_m=16.0,
        input_lines=(
            f'dtm_directory = "{dtm_dir.as_posix()}"',
            f'dsm_directory = "{dsm_dir.as_posix()}"',
        ),
    )

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "point-cloud",
            "--tree-canopy-overlay",
            str(overlay_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["tree_point_count"] > 0
    assert payload["tree_points_path"].endswith("tree_points.ply")


def test_run_stage_point_cloud_resolves_explicit_footprints_from_config_directory(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_dir = tmp_path / "scenario"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    output_root = tmp_path / "outputs"
    override_path = config_dir / "inputs" / "accepted_buildings.geojson"
    override_path.parent.mkdir()
    override_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    write_complete_config(config_path, output_root=output_root, name="CLI Footprint Override Fixture")
    captured_paths: list[Path | None] = []

    def fake_run(config, *, building_footprints_path=None):
        captured_paths.append(building_footprints_path)
        return SimpleNamespace(to_dict=lambda: {"status": "ok"})

    monkeypatch.setattr(point_cloud, "run", fake_run)

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "point-cloud",
            "--building-footprints-geojson",
            "inputs/accepted_buildings.geojson",
            "--json",
        ]
    )

    assert exit_code == 0
    assert captured_paths == [override_path.resolve()]
    assert '"status": "ok"' in capsys.readouterr().out


def test_rejects_building_footprint_override_for_unrelated_stage(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    override_path = tmp_path / "buildings.geojson"
    override_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    write_complete_config(config_path, output_root=tmp_path / "outputs")

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "trees",
            "--building-footprints-geojson",
            str(override_path),
        ]
    )

    assert exit_code == 2
    assert "valid only for the point-cloud stage" in capsys.readouterr().err


def test_run_stage_trees_json(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    stage1_dir = output_root / "01_shapefiles"
    stage1_dir.mkdir(parents=True)
    (stage1_dir / "trees.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [11.2558, 43.7696]},
                        "properties": {"category": "trees", "tags": {"natural": "tree", "species": "Tilia"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_complete_config(config_path, output_root=output_root, name="CLI Trees Fixture")

    exit_code = main(["run-stage", "--config", str(config_path), "trees", "--json"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"tree_count": 1' in captured.out
    assert "tree_models_manifest.json" in captured.out


def test_run_stage_trees_accepts_terrain_geometry_override(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    terrain_path = tmp_path / "terrain.obj"
    write_complete_config(config_path, output_root=output_root, name="CLI Trees Terrain Fixture")
    captured_configs: list[object] = []

    def fake_run(config):
        captured_configs.append(config)
        return SimpleNamespace(to_dict=lambda: {"status": "ok"})

    monkeypatch.setattr("cities_reconstruction.cli.trees.run", fake_run)

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "trees",
            "--tree-terrain-geometry",
            terrain_path.name,
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"status": "ok"' in captured.out
    assert captured_configs[0].inputs.tree_terrain_geometry_path == terrain_path


def test_cli_air_purifier_path_overrides_are_config_relative(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    captured: dict[str, object] = {}

    def fake_run(config, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(to_dict=lambda: {"status": "ok"})

    monkeypatch.setattr(air_purifiers, "run", fake_run)

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "air-purifiers",
            "--model-library",
            "assets/parameters.json",
            "--terrain-geometry",
            "outputs/terrain.obj",
            "--json",
        ]
    )

    assert exit_code == 0
    assert captured == {
        "model_library_path": tmp_path / "assets/parameters.json",
        "terrain_geometry_path": tmp_path / "outputs/terrain.obj",
    }
    assert '"status": "ok"' in capsys.readouterr().out


def test_cli_air_purifiers_uses_config_paths_when_overrides_are_absent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        air_purifiers_block="""
[air_purifiers]
model_library_path = "assets/configured.json"
terrain_geometry_path = "outputs/configured.obj"
""",
    )
    captured: dict[str, object] = {}

    def fake_run(config, **kwargs):
        captured["configured_model_library"] = config.air_purifiers.model_library_path
        captured["configured_terrain_geometry"] = config.air_purifiers.terrain_geometry_path
        captured.update(kwargs)
        return SimpleNamespace(to_dict=lambda: {"status": "ok"})

    monkeypatch.setattr(air_purifiers, "run", fake_run)

    exit_code = main(
        ["run-stage", "--config", str(config_path), "air-purifiers", "--json"]
    )

    assert exit_code == 0
    assert captured == {
        "configured_model_library": tmp_path / "assets/configured.json",
        "configured_terrain_geometry": tmp_path / "outputs/configured.obj",
        "model_library_path": None,
        "terrain_geometry_path": None,
    }
    assert '"status": "ok"' in capsys.readouterr().out


@pytest.mark.parametrize(
    "stage",
    ["shapefiles", "visual-enrichment", "point-cloud", "city-models", "trees"],
)
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--model-library", "assets/parameters.json"),
        ("--terrain-geometry", "outputs/terrain.obj"),
    ],
)
def test_cli_rejects_air_purifier_overrides_for_every_other_stage(
    stage: str,
    option: str,
    value: str,
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")

    exit_code = main(
        ["run-stage", "--config", str(config_path), stage, option, value]
    )

    assert exit_code == 2
    assert capsys.readouterr().err == (
        f"{option} is valid only for the air-purifiers stage\n"
    )


def test_cli_air_purifiers_reports_unresolved_model_library(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")

    exit_code = main(["run-stage", "--config", str(config_path), "air-purifiers"])

    assert exit_code == 2
    assert capsys.readouterr().err == (
        "Configuration error: air-purifier model library is unresolved; "
        "configure model_library_path or provide an override\n"
    )


def test_run_stage_help_lists_air_purifier_stage_and_overrides(capsys) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["run-stage", "--help"])

    output = capsys.readouterr().out
    assert "air-purifiers" in output
    assert "--model-library" in output
    assert "--terrain-geometry" in output


def test_run_stage_city_models_accepts_argument_overrides(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    write_complete_config(config_path, output_root=output_root, name="CLI City Models Fixture")
    captured_configs: list[object] = []

    def fake_run(config):
        captured_configs.append(config)
        return SimpleNamespace(to_dict=lambda: {"status": "ok"})

    monkeypatch.setattr("cities_reconstruction.cli.city_models.run", fake_run)

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "city-models",
            "--city-models-top-height",
            "410",
            "--city-models-flow-direction",
            "4",
            "5",
            "--city-models-reconstruction-influence-region",
            "88",
            "--no-city-models-reconstruction-validate",
            "--city-models-docker-image",
            "example/city4cfd:cli",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"status": "ok"' in captured.out
    config = captured_configs[0]
    assert config.city_models.top_height == 410.0
    assert config.city_models.flow_direction == (4.0, 5.0)
    assert config.city_models.reconstruction_region.influence_region_m == 88.0
    assert config.city_models.reconstruction_region.validate is False
    assert config.city_models.docker_image == "example/city4cfd:cli"


def test_run_stage_city_models_returns_one_after_printing_external_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    result = SimpleNamespace(
        stage_status="failed_external_execution",
        to_dict=lambda: {"stage_status": "failed_external_execution", "return_code": 17},
    )
    monkeypatch.setattr("cities_reconstruction.cli.city_models.run", lambda _config: result)

    exit_code = main(["run-stage", "--config", str(config_path), "city-models", "--json"])

    assert exit_code == 1
    assert '"stage_status": "failed_external_execution"' in capsys.readouterr().out


def test_city_models_toml_and_cli_validation_have_identical_errors_and_do_not_run_stage(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    valid_path = tmp_path / "valid.toml"
    invalid_path = tmp_path / "invalid.toml"
    write_complete_config(valid_path, output_root=tmp_path / "outputs")
    invalid_path.write_text(
        valid_path.read_text(encoding="utf-8").replace(
            "influence_region_m = 150.0",
            "influence_region_m = -1.0",
        ),
        encoding="utf-8",
    )

    toml_exit = main(["validate-config", "--config", str(invalid_path)])
    toml_error = capsys.readouterr().err
    stage_called = False

    def fail_if_called(_config):
        nonlocal stage_called
        stage_called = True
        raise AssertionError("invalid effective config must not run the stage")

    monkeypatch.setattr("cities_reconstruction.cli.city_models.run", fail_if_called)
    cli_exit = main(
        [
            "run-stage",
            "--config",
            str(valid_path),
            "city-models",
            "--city-models-reconstruction-influence-region",
            "-1",
        ]
    )
    cli_error = capsys.readouterr().err

    assert toml_exit == cli_exit == 2
    assert toml_error == cli_error
    assert toml_error == (
        "Configuration error: city_models.reconstruction_region.influence_region_m "
        "must be positive\n"
    )
    assert stage_called is False


def test_city_models_cli_rejects_non_finite_override_before_stage(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    monkeypatch.setattr(
        "cities_reconstruction.cli.city_models.run",
        lambda _config: (_ for _ in ()).throw(AssertionError("stage must not run")),
    )

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "city-models",
            "--city-models-top-height",
            "nan",
        ]
    )

    assert exit_code == 2
    assert "city_models.top_height must be finite" in capsys.readouterr().err


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


def _write_roof_with_tree_peak_grid(path: Path, center_x: float, center_y: float) -> None:
    rows = []
    for row in range(5):
        row_values = []
        for col in range(5):
            row_values.append("20" if row == 2 and col == 2 else "15")
        rows.append(" ".join(row_values))
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
    path.parent.mkdir(parents=True, exist_ok=True)
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
                            "building_base_height_m": 0.0,
                            "tags": {"building": "yes"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_trees(path: Path, center_lon: float, center_lat: float) -> None:
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
