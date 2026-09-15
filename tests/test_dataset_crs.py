from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.geometry import crs
from cities_reconstruction.geometry.crs_inputs import source_crs
from cities_reconstruction.geometry.spatial_plan import verify_spatial_plan
from cities_reconstruction.stage_runtime import StageRunOptions, _apply_shapefile_input_overrides, prepare_run_config
from cities_reconstruction.urban_planning import _point_coordinates
from tests.config_helpers import write_complete_config


def test_planning_utm_input_is_normalized_to_lonlat() -> None:
    result = _point_coordinates({"type": "Point", "coordinates": [500000.0, 0.0]}, "EPSG:32633", "fixture")
    assert result == pytest.approx((15.0, 0.0), abs=1e-10)


def test_sidecar_retains_explicit_datum_operation(tmp_path: Path) -> None:
    from pyproj import CRS

    definition = (
        "+proj=utm +zone=32 +ellps=intl +towgs84=-104.1,-49.1,-9.9,0.971,-2.917,0.714,-11.68 +units=m +type=crs"
    )
    metadata = CRS.from_user_input(definition)
    path = tmp_path / "input.shp"
    path.with_suffix(".prj").write_text(metadata.to_wkt(), encoding="utf-8")
    assert metadata.source_crs is not None
    resolved = source_crs(path, metadata.source_crs.to_wkt())
    assert CRS.from_user_input(resolved).is_bound


def test_project_to_second_utm_zone_uses_requested_zone() -> None:
    assert crs.project_lonlat(15.0, 0.0, "EPSG:32633") == pytest.approx((500000.0, 0.0), abs=1e-6)


@pytest.mark.parametrize("alias", ["EPSG::25832", "epsg::25832", "epsg:25832"])
def test_canonical_crs_preserves_legacy_epsg_aliases(alias: str) -> None:
    assert crs.canonical_crs(alias) == "EPSG:25832"


@pytest.mark.parametrize("target", ["EPSG:4326", "EPSG:3857", "EPSG:2263", "EPSG:4978", "invalid"])
def test_config_rejects_unsuitable_working_crs_before_execution(tmp_path: Path, target: str) -> None:
    with pytest.raises(ConfigError, match="CRS|crs"):
        load_config(write_complete_config(tmp_path / "config.toml", crs=target))


def test_auto_working_crs_uses_declared_raster_crs(tmp_path: Path) -> None:
    config = load_config(
        write_complete_config(
            tmp_path / "config.toml",
            crs="auto",
            input_lines=(
                'dtm_directory = "dtm"',
                'dsm_directory = "dsm"',
                'dtm_source_crs = "EPSG:32633"',
                'dsm_source_crs = "EPSG:32633"',
            ),
        )
    )
    assert config.working_crs == "EPSG:32633"


def test_auto_cannot_guess_raster_source_crs(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="source CRS|source crs"):
        load_config(
            write_complete_config(
                tmp_path / "config.toml",
                crs="auto",
                input_lines=('dtm_directory = "dtm"', 'dsm_directory = "dsm"'),
            )
        )


def test_auto_revalidation_rejects_deleted_raster_sidecar(tmp_path: Path) -> None:
    for name in ("dtm", "dsm"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "tile.asc").write_text("", encoding="utf-8")
        (directory / "tile.prj").write_text("EPSG:32633", encoding="utf-8")
    config = load_config(
        write_complete_config(
            tmp_path / "config.toml",
            crs="auto",
            input_lines=('dtm_directory = "dtm"', 'dsm_directory = "dsm"'),
        )
    )
    (tmp_path / "dtm" / "tile.prj").unlink()

    with pytest.raises(ConfigError, match="missing source CRS"):
        verify_spatial_plan(config)


def test_auto_revalidation_rejects_new_metadata_free_raster_tile(tmp_path: Path) -> None:
    for name in ("dtm", "dsm"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "tile.asc").write_text("", encoding="utf-8")
        (directory / "tile.prj").write_text("EPSG:32633", encoding="utf-8")
    config = load_config(
        write_complete_config(
            tmp_path / "config.toml",
            crs="auto",
            input_lines=('dtm_directory = "dtm"', 'dsm_directory = "dsm"'),
        )
    )
    (tmp_path / "dsm" / "new.asc").write_text("", encoding="utf-8")

    with pytest.raises(ConfigError, match="missing source CRS"):
        verify_spatial_plan(config)


def test_declared_raster_crs_cannot_be_relabelled_as_working_crs(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="working CRS|working crs"):
        load_config(
            write_complete_config(
                tmp_path / "config.toml",
                input_lines=(
                    'dtm_directory = "dtm"',
                    'dsm_directory = "dsm"',
                    'dtm_source_crs = "EPSG:32633"',
                    'dsm_source_crs = "EPSG:32633"',
                ),
            )
        )


def test_raster_sidecar_conflict_is_rejected(tmp_path: Path) -> None:
    dtm = tmp_path / "dtm"
    dtm.mkdir()
    (dtm / "tile.asc").write_text("", encoding="utf-8")
    (dtm / "tile.prj").write_text("EPSG:32633", encoding="utf-8")
    with pytest.raises(ConfigError, match="conflict"):
        load_config(
            write_complete_config(
                tmp_path / "config.toml",
                input_lines=(
                    'dtm_directory = "dtm"',
                    'dsm_directory = "dsm"',
                    'dtm_source_crs = "EPSG:25832"',
                    'dsm_source_crs = "EPSG:25832"',
                ),
            )
        )


def test_transform_rejects_nonfinite_coordinates() -> None:
    with pytest.raises(ConfigError, match="finite"):
        crs.transform_xy(float("nan"), 0.0, "EPSG:4326", "EPSG:32633")


def test_new_cli_shapefile_override_uses_its_own_sidecar(tmp_path: Path) -> None:
    config = load_config(write_complete_config(tmp_path / "config.toml"))
    streets = tmp_path / "streets.shp"
    streets.touch()
    streets.with_suffix(".prj").write_text("EPSG:32633", encoding="utf-8")

    resolved = _apply_shapefile_input_overrides(
        config,
        StageRunOptions(streets_shapefile=streets),
    )

    surface = next(item for item in resolved.shapefiles.supplemental if item.name == "streets")
    assert surface.crs is None  # Metadata must not become a user declaration.
    assert source_crs(surface.path, surface.crs) == "EPSG:32633"


def test_new_cli_shapefile_override_requires_source_crs_evidence(tmp_path: Path) -> None:
    config = load_config(write_complete_config(tmp_path / "config.toml"))
    streets = tmp_path / "streets.shp"
    streets.touch()

    with pytest.raises(ConfigError, match="missing source CRS"):
        prepare_run_config(
            config,
            StageRunOptions(streets_shapefile=streets),
        )


def test_auto_with_web_mercator_clouds_selects_roi_utm(tmp_path: Path) -> None:
    config = load_config(
        write_complete_config(
            tmp_path / "config.toml",
            crs="auto",
            center_lon=15.0,
            center_lat=1.0,
            input_lines=(
                'ground_point_cloud_path = "ground.ply"',
                'building_point_cloud_path = "building.ply"',
                'point_cloud_source_crs = "EPSG:3857"',
            ),
        )
    )
    assert config.working_crs == "EPSG:32633"


@pytest.mark.parametrize(
    "extra,error",
    [
        ((), "point_cloud_source_crs"),
        (('point_cloud_source_crs = "EPSG:4326"', 'dtm_directory = "dtm"'), "mutually exclusive"),
        (('point_cloud_source_crs = "EPSG:4326"', 'tree_canopy_overlay_path = "mask.png"'), "raster"),
    ],
)
def test_supplied_cloud_mode_rejects_ambiguous_inputs(tmp_path: Path, extra: tuple[str, ...], error: str) -> None:
    with pytest.raises(ConfigError, match=error):
        load_config(
            write_complete_config(
                tmp_path / "config.toml",
                input_lines=(
                    'ground_point_cloud_path = "ground.ply"',
                    'building_point_cloud_path = "building.ply"',
                    *extra,
                ),
            )
        )
