from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError
from cities_reconstruction.stages.air_purifiers import geometry
from cities_reconstruction.stages.air_purifiers.publication import placement_payload
from cities_reconstruction.stages.air_purifiers.models import AirPurifierModel


def _model() -> AirPurifierModel:
    return AirPurifierModel(
        name="tower", kind="octagonal", source_path=Path("tower.stl"),
        native_width_m=1.0, native_depth_m=1.0, native_height_m=2.0,
        linear_tolerance_m=0.01, mesh={"inlet": [], "outlet": [], "tower": []},
    )


def test_resolve_instances_uses_requested_working_crs() -> None:
    feature = {
        "type": "Feature", "geometry": {"type": "Point", "coordinates": [11.2558, 43.7696]},
        "properties": {"purifier_id": "AP-1", "model": "tower", "urban_planning_input_id": "fixture",
                       "source": "fixture", "source_crs": "EPSG:4326", "source_feature_index": 0,
                       "roi_zone": "inner", "source_properties": {}},
    }

    instances = geometry.resolve_instances(
        [feature], {"tower": _model()}, origin_x=198600.0, origin_y=4853000.0,
        terrain_path=None, terrain_sampler=None, working_crs="EPSG:32633",
    )

    assert (instances[0].projected_x, instances[0].projected_y) == pytest.approx(
        (198643.41497553675, 4853099.8229988525), abs=0.001
    )
    assert (instances[0].local_x, instances[0].local_y) == pytest.approx(
        (43.41497553675, 99.8229988525), abs=0.001
    )
    payload = placement_payload(instances, working_crs="EPSG:32633")
    assert payload["crs"]["properties"]["name"] == "EPSG:32633"
    assert payload["features"][0]["geometry"]["coordinates"] == pytest.approx(
        [198643.41497553675, 4853099.8229988525]
    )


def test_resolve_instances_rejects_duplicate_purifier_ids() -> None:
    model = AirPurifierModel(
        name="tower",
        kind="octagonal",
        source_path=Path("tower.stl"),
        native_width_m=1.0,
        native_depth_m=1.0,
        native_height_m=2.0,
        linear_tolerance_m=0.01,
        mesh={"inlet": [], "outlet": [], "tower": []},
    )
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [11.2, 43.7]},
        "properties": {
            "purifier_id": "AP-1",
            "model": "tower",
            "urban_planning_input_id": "fixture",
            "source": "fixture",
            "source_crs": "EPSG:4326",
            "source_feature_index": 0,
            "roi_zone": "inner",
            "source_properties": {},
        },
    }

    with pytest.raises(ConfigError, match="duplicate air-purifier ID 'AP-1'"):
        geometry.resolve_instances(
            [feature, feature],
            {"tower": model},
            origin_x=0.0,
            origin_y=0.0,
            terrain_path=None,
            terrain_sampler=None,
        )
