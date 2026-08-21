from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest

from cities_reconstruction.config import SupplementalShapefileConfig, load_config
from tests.config_helpers import write_complete_config


def _supplemental_module() -> ModuleType:
    try:
        return import_module("cities_reconstruction.stages.shapefiles.supplemental")
    except ModuleNotFoundError:
        pytest.fail("the focused shapefiles supplemental module is missing")


def test_tree_attribute_mapping_accepts_new_alias_groups() -> None:
    supplemental = _supplemental_module()
    mappings = (
        *supplemental.TREE_ATTRIBUTE_MAPPINGS,
        supplemental.TreeAttributeMapping(
            tag="health",
            aliases=("health", "stato_salute"),
        ),
    )

    tags = supplemental.tree_tags_from_attributes(
        {
            "SPECIE": "Tilia",
            "DBH": 32.0,
            "CIRCONF_CM": 100.5,
            "STATO_SALUTE": "good",
        },
        mappings=mappings,
    )

    assert tags == {
        "natural": "tree",
        "species": "Tilia",
        "dbh": 32.0,
        "diameter": 0.32,
        "source_circumference": 100.5,
        "circumference": 1.005,
        "health": "good",
    }


@pytest.mark.parametrize(
    ("attributes", "metric_tag", "expected"),
    (
        ({"DBH_CM": 1}, "diameter", 0.01),
        ({"DBH_MM": 10}, "diameter", 0.01),
        ({"DIAMETER_M": 32}, "diameter", 32.0),
        ({"DBH": "1 cm"}, "diameter", 0.01),
        ({"DBH_CM": "1 cm"}, "diameter", 0.01),
        ({"DBH": "32 m"}, "diameter", 32.0),
        ({"DBH": "10 mm"}, "diameter", 0.01),
        ({"DBH": 1}, "diameter", 0.01),
        ({"CIRCUMFERENCE_CM": 1}, "circumference", 0.01),
        ({"CIRCUMFERENCE_MM": 1000}, "circumference", 1.0),
        ({"CIRCUMFERENCE": "1 cm"}, "circumference", 0.01),
        ({"CIRCUMFERENCE": 100}, "circumference", 100.0),
    ),
)
def test_metric_mapping_resolves_explicit_units_before_standard_defaults(
    attributes: dict[str, object],
    metric_tag: str,
    expected: float,
) -> None:
    supplemental = _supplemental_module()

    tags = supplemental.tree_tags_from_attributes(attributes)

    assert tags[metric_tag] == expected


def test_metric_mapping_omits_derived_value_for_conflicting_explicit_units() -> None:
    supplemental = _supplemental_module()

    tags = supplemental.tree_tags_from_attributes({"DBH_CM": "0.32 m"})

    assert tags["dbh"] == "0.32 m"
    assert "diameter" not in tags


@pytest.mark.parametrize(
    "value",
    (float("nan"), float("inf"), float("-inf")),
)
def test_metric_mapping_omits_non_finite_derived_values(value: float) -> None:
    supplemental = _supplemental_module()

    tags = supplemental.tree_tags_from_attributes({"DBH": value})

    assert "diameter" not in tags


def test_tree_feature_retains_unmapped_source_attributes(tmp_path: Path) -> None:
    supplemental = _supplemental_module()
    config = load_config(write_complete_config(tmp_path / "config.toml"))
    tree_input = SupplementalShapefileConfig(
        name="municipal_trees",
        path=tmp_path / "trees.shp",
        crs="EPSG:4326",
        category="trees",
        group_tag=None,
        enabled=True,
    )
    attributes = {
        "SPECIE": "Celtis australis",
        "HEALTH": "good",
        "IRRIGATION": "drip",
    }

    feature = supplemental._tree_shapefile_feature(
        lon=config.region.center_lon,
        lat=config.region.center_lat,
        config=config,
        tree_input=tree_input,
        path=tree_input.path,
        attributes=attributes,
        record_number=4,
        point_index=1,
        sequence_index=1,
    )

    assert feature is not None
    assert feature["properties"]["source_attributes"] == attributes
    assert feature["properties"]["tags"] == {
        "natural": "tree",
        "species": "Celtis australis",
    }
