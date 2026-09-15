from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cities_reconstruction.config import AppConfig, ConfigError, load_config
from cities_reconstruction.geometry.crs import (
    EPSG_25832,
    WEB_MERCATOR,
    WGS84,
    epsg25832_to_lonlat,
    local_xy_to_lonlat,
    lonlat_bbox_from_radius,
    lonlat_to_epsg25832,
    lonlat_to_local_xy,
    normalize_crs_name,
    web_mercator_to_lonlat,
)
from cities_reconstruction.stages.air_purifiers.geometry import (
    lonlat_to_epsg25832 as project_air_purifier_lonlat,
)
from cities_reconstruction.stages.city_models.geometry import (
    lonlat_to_epsg25832 as project_city_model_lonlat,
)
from cities_reconstruction.stages.point_cloud.stage import (
    _lonlat_to_epsg25832 as project_point_cloud_lonlat,
)
from cities_reconstruction.stages.shapefiles import supplemental as shapefiles_supplemental
from cities_reconstruction.stages.shapefiles import transformation as shapefiles_transformation
from cities_reconstruction.stages.trees.geometry import (
    lonlat_to_epsg25832 as project_tree_lonlat,
)
from cities_reconstruction.stages.visual_enrichment import stage as visual_enrichment
from cities_reconstruction.urban_planning import _inverse_web_mercator
from tests.config_helpers import write_complete_config

ForwardProjector = Callable[[float, float], tuple[float, float]]

# Independently checked with PROJ 9.4.0 `cs2cs`, using EPSG:4326's latitude/longitude axis order.
FLORENCE_LON_LAT = (11.2558, 43.7696)
FLORENCE_EPSG25832 = (681557.2487172568, 4848756.3854547078)
FLORENCE_EPSG3857 = (1252989.9244709287, 5429856.207136513)


@pytest.mark.parametrize(
    "projector",
    (
        project_air_purifier_lonlat,
        project_city_model_lonlat,
        project_point_cloud_lonlat,
        project_tree_lonlat,
    ),
    ids=("air-purifiers", "city-models", "point-cloud", "trees"),
)
@pytest.mark.parametrize(
    ("lon_lat", "expected"),
    (
        (FLORENCE_LON_LAT, FLORENCE_EPSG25832),
        ((9.0, 0.0), (500000.0, 0.0)),
    ),
    ids=("florence", "utm-zone-32-origin"),
)
def test_epsg25832_forward_projectors_match_independent_references(
    projector: ForwardProjector,
    lon_lat: tuple[float, float],
    expected: tuple[float, float],
) -> None:
    assert projector(*lon_lat) == pytest.approx(expected, abs=1.0e-3)


@pytest.mark.parametrize(
    ("lon_lat", "expected"),
    (
        (FLORENCE_LON_LAT, FLORENCE_EPSG25832),
        ((9.0, 0.0), (500000.0, 0.0)),
    ),
    ids=("florence", "utm-zone-32-origin"),
)
def test_shared_epsg25832_forward_transform_matches_independent_references(
    lon_lat: tuple[float, float],
    expected: tuple[float, float],
) -> None:
    assert lonlat_to_epsg25832(*lon_lat) == pytest.approx(expected, abs=1.0e-3)


def test_epsg25832_inverse_matches_independent_florence_reference() -> None:
    result = shapefiles_supplemental._shapefile_xy_to_lonlat(
        *FLORENCE_EPSG25832,
        "EPSG::25832",
        "fixture.crs",
    )

    assert result == pytest.approx(FLORENCE_LON_LAT, abs=5.0e-8)


def test_shared_epsg25832_inverse_matches_independent_florence_reference() -> None:
    assert epsg25832_to_lonlat(*FLORENCE_EPSG25832) == pytest.approx(
        FLORENCE_LON_LAT,
        abs=5.0e-8,
    )


def test_supplemental_inverse_reports_invalid_crs_with_context() -> None:
    with pytest.raises(
        ConfigError,
        match=r"fixture\.crs: invalid horizontal CRS",
    ):
        shapefiles_supplemental._shapefile_xy_to_lonlat(
            *FLORENCE_EPSG25832,
            "EPSG:0",
            "fixture.crs",
        )


def test_web_mercator_inverse_matches_independent_florence_reference() -> None:
    result = _inverse_web_mercator(*FLORENCE_EPSG3857, "planning feature")

    assert result == pytest.approx(FLORENCE_LON_LAT, abs=1.0e-12)


def test_shared_web_mercator_inverse_matches_independent_florence_reference() -> None:
    assert web_mercator_to_lonlat(*FLORENCE_EPSG3857) == pytest.approx(
        FLORENCE_LON_LAT,
        abs=1.0e-12,
    )


@pytest.mark.parametrize(
    ("coordinates", "message"),
    (
        ((21_000_000.0, 0.0), "planning feature EPSG:3857 coordinates exceed Web Mercator bounds"),
        ((-21_000_000.0, 0.0), "planning feature EPSG:3857 coordinates exceed Web Mercator bounds"),
        ((0.0, 21_000_000.0), "planning feature EPSG:3857 coordinates exceed Web Mercator bounds"),
        ((0.0, -21_000_000.0), "planning feature EPSG:3857 coordinates exceed Web Mercator bounds"),
        ((float("nan"), 0.0), "planning feature EPSG:3857 coordinates do not transform to finite"),
        ((0.0, float("nan")), "planning feature EPSG:3857 coordinates do not transform to finite"),
    ),
    ids=(
        "positive-x-outside-bounds",
        "negative-x-outside-bounds",
        "positive-y-outside-bounds",
        "negative-y-outside-bounds",
        "non-finite-x-result",
        "non-finite-y-result",
    ),
)
def test_web_mercator_inverse_preserves_error_contract(
    coordinates: tuple[float, float],
    message: str,
) -> None:
    with pytest.raises(ConfigError, match=message):
        _inverse_web_mercator(*coordinates, "planning feature")


@pytest.mark.parametrize(
    "projector",
    (
        shapefiles_transformation._project_coordinate_m,
        visual_enrichment._project_coordinate_m,
    ),
    ids=("shapefiles", "visual-enrichment"),
)
def test_local_metric_projectors_match_small_roi_reference(
    tmp_path: Path,
    projector: Callable[[list[float], AppConfig], tuple[float, float]],
) -> None:
    config = load_config(
        write_complete_config(
            tmp_path / "config.toml",
            center_lon=FLORENCE_LON_LAT[0],
            center_lat=FLORENCE_LON_LAT[1],
        )
    )

    assert projector([11.2568, 43.7706], config) == pytest.approx(
        (80.2968992543, 111.1949266451),
        abs=1.0e-6,
    )


def test_shared_local_projection_round_trips_small_roi_reference() -> None:
    local_xy = lonlat_to_local_xy(
        11.2568,
        43.7706,
        center_lon=FLORENCE_LON_LAT[0],
        center_lat=FLORENCE_LON_LAT[1],
    )

    assert local_xy == pytest.approx((80.2968992543, 111.1949266451), abs=1.0e-6)
    assert local_xy_to_lonlat(
        *local_xy,
        center_lon=FLORENCE_LON_LAT[0],
        center_lat=FLORENCE_LON_LAT[1],
    ) == pytest.approx((11.2568, 43.7706), abs=1.0e-12)


def test_shared_lonlat_bbox_matches_small_roi_reference() -> None:
    assert lonlat_bbox_from_radius(
        center_lon=FLORENCE_LON_LAT[0],
        center_lat=FLORENCE_LON_LAT[1],
        radius_m=500.0,
    ) == pytest.approx(
        (11.2495731095, 43.7651033920, 11.2620268905, 43.7740966080),
        abs=1.0e-10,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("EPSG:4326", WGS84),
        (" epsg::3857 ", WEB_MERCATOR),
        ("epsg:25832", EPSG_25832),
    ),
)
def test_normalize_crs_name_canonicalizes_supported_spelling(raw: str, expected: str) -> None:
    assert normalize_crs_name(raw) == expected
