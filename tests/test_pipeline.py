from __future__ import annotations

from pathlib import Path

import pytest


from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.pipeline import STAGE_NAMES, dry_run


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def florence_config():
    return load_config(ROOT / "config/examples/florence.toml")


def test_dry_run_returns_all_stage_plans_in_order(florence_config) -> None:
    results = dry_run(florence_config)

    assert [result.stage for result in results] == list(STAGE_NAMES)
    assert len(results) == 7
    assert all(result.planned_actions for result in results)


def test_dry_run_includes_air_purifiers_between_trees_and_openfoam(florence_config) -> None:
    results = dry_run(florence_config)
    names = [result.stage for result in results]

    tree_index = names.index("trees")
    assert names[tree_index : tree_index + 3] == ["trees", "air-purifiers", "openfoam"]
    assert results[tree_index + 1].expected_outputs == (
        florence_config.output.root_directory / "05_air_purifiers",
    )


def test_dry_run_can_select_single_stage(florence_config) -> None:
    results = dry_run(florence_config, stages=["trees"])

    assert len(results) == 1
    assert results[0].stage == "trees"
    assert "Tilia" in " ".join(results[0].planned_actions)


def test_point_cloud_dry_run_names_default_stage1_footprints(florence_config) -> None:
    result = dry_run(florence_config, stages=["point-cloud"])[0]
    actions = " ".join(result.planned_actions)

    assert str(florence_config.output.root_directory / "01_shapefiles" / "buildings.geojson") in actions
    assert "visual enrichment when present" not in actions


def test_dry_run_rejects_unknown_stage(florence_config) -> None:
    with pytest.raises(ConfigError, match="unknown pipeline stage"):
        dry_run(florence_config, stages=["missing"])
