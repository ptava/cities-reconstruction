from dataclasses import replace
from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError, load_config, load_settings
from cities_reconstruction.geometry.spatial_plan import verify_spatial_plan
from tests.config_helpers import write_complete_config


def _settings(tmp_path: Path, *, inputs: tuple[str, ...] = (), advanced: str = "") -> Path:
    path = write_complete_config(tmp_path / "config.toml", input_lines=inputs, crs=None)
    text = path.read_text()
    path.write_text(text + advanced)
    return path


def test_region_needs_no_crs_and_request_is_preserved(tmp_path: Path) -> None:
    config = load_config(_settings(tmp_path))
    assert config.reconstruction.working_crs is None
    assert config.working_crs == "EPSG:32632"
    assert config.coordinate_plan.selection_reason == "UTM selected from ROI center"
    assert not hasattr(config.region, "crs")


def test_advanced_working_crs_is_not_raster_source_evidence(tmp_path: Path) -> None:
    path = _settings(
        tmp_path,
        inputs=('dtm_directory = "dtm"', 'dsm_directory = "dsm"'),
        advanced='\n[reconstruction]\nworking_crs = "EPSG:25832"\n',
    )
    with pytest.raises(ConfigError, match="source CRS"):
        load_config(path)


def test_suitable_prepared_cloud_crs_is_retained(tmp_path: Path) -> None:
    config = load_config(
        _settings(
            tmp_path,
            inputs=(
                'ground_point_cloud_path = "ground.ply"',
                'building_point_cloud_path = "buildings.ply"',
                'point_cloud_source_crs = "EPSG:25832"',
            ),
        )
    )
    assert config.working_crs == "EPSG:25832"
    assert config.coordinate_plan.input_mode == "prepared_clouds"
    assert config.coordinate_plan.datasets[0].evidence == "declared"
    assert config.coordinate_plan.datasets[0].action == "retain coordinates"


def test_raster_metadata_selects_working_crs_without_duplicate_setting(tmp_path: Path) -> None:
    for label in ("dtm", "dsm"):
        directory = tmp_path / label
        directory.mkdir()
        (directory / "tile.asc").write_text("")
        (directory / "tile.prj").write_text("EPSG:25832")
    config = load_config(_settings(tmp_path, inputs=('dtm_directory = "dtm"', 'dsm_directory = "dsm"')))
    assert config.reconstruction.working_crs is None
    assert config.working_crs == "EPSG:25832"
    assert config.coordinate_plan.input_mode == "terrain_rasters"
    assert all(dataset.evidence == "metadata" for dataset in config.coordinate_plan.datasets)


def test_legacy_region_crs_has_actionable_migration_error(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    path.write_text(path.read_text().replace("[region]", '[region]\ncrs = "EPSG:25832"'))
    with pytest.raises(ConfigError, match="reconstruction.working_crs"):
        load_config(path)


def test_settings_parse_without_inspecting_dataset_metadata(tmp_path: Path) -> None:
    path = _settings(tmp_path, inputs=('dtm_directory = "missing-dtm"', 'dsm_directory = "missing-dsm"'))
    settings = load_settings(path)
    assert settings.spatial_plan is None
    with pytest.raises(ConfigError, match="unresolved"):
        _ = settings.working_crs
    with pytest.raises(ConfigError, match="source CRS"):
        load_config(path)


def test_runtime_does_not_silently_change_auto_working_frame(tmp_path: Path) -> None:
    for name in ("dtm", "dsm"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "tile.asc").write_text("")
        (directory / "tile.prj").write_text("EPSG:25832")
    config = load_config(_settings(tmp_path, inputs=('dtm_directory = "dtm"', 'dsm_directory = "dsm"')))
    for name in ("dtm", "dsm"):
        (tmp_path / name / "tile.prj").write_text("EPSG:32633")
    with pytest.raises(ConfigError, match="changed after preparation"):
        verify_spatial_plan(config)


def test_incomplete_raster_route_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="both dtm_directory and dsm_directory"):
        load_config(_settings(tmp_path, inputs=('dtm_directory = "dtm"', 'dtm_source_crs = "EPSG:25832"')))


def test_vector_source_aliases_cannot_be_combined(tmp_path: Path) -> None:
    path = _settings(tmp_path, advanced='''
[[urban_planning.inputs]]
name = "scenario"
path = "scenario.geojson"
source_crs = "EPSG:4326"
crs = "EPSG:4326"
''')
    with pytest.raises(ConfigError, match="only source_crs"):
        load_config(path)


@pytest.mark.parametrize("stage_name", ["shapefiles", "point_cloud", "city_models", "trees", "air_purifiers", "visual_enrichment"])
def test_every_stage_rejects_a_stale_coordinate_plan(tmp_path: Path, stage_name: str, monkeypatch) -> None:
    import importlib
    def unexpected_network(*args, **kwargs):
        pytest.fail("stale coordinate plan reached the network boundary")
    monkeypatch.setattr("urllib.request.urlopen", unexpected_network)
    stage = importlib.import_module(f"cities_reconstruction.stages.{stage_name}")
    config = load_config(_settings(tmp_path))
    stale = replace(config, region=replace(config.region, center_lon=11.3))
    with pytest.raises(ConfigError, match="changed after preparation"):
        stage.run(stale)
