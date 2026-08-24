from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError
from cities_reconstruction.stages.air_purifiers import geometry
from cities_reconstruction.stages.air_purifiers.models import AirPurifierModel


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
