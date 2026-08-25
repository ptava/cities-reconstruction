from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.pipeline import (
    EXECUTABLE_STAGE_NAMES,
    OPTIONAL_STAGE_NAMES,
    STAGE_BY_NAME,
    STAGE_NAMES,
    STAGE_SPECS,
    StageInputSpec,
    StageMaturity,
    StageSelection,
    dry_run,
)
from cities_reconstruction.stage_layout import STAGE_LAYOUT_BY_ID, StageId
from cities_reconstruction.stage_runtime import StageRunOptions

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def florence_config():
    return load_config(ROOT / "config/examples/florence.toml")


def test_dry_run_returns_all_stage_plans_in_order(florence_config) -> None:
    results = dry_run(florence_config)

    assert [result.stage for result in results] == list(STAGE_NAMES)
    assert len(results) == 7
    assert all(result.planned_actions for result in results)


def test_stage_registry_defines_order_and_executability() -> None:
    assert STAGE_NAMES == (
        "shapefiles",
        "visual-enrichment",
        "point-cloud",
        "city-models",
        "trees",
        "air-purifiers",
        "openfoam",
    )
    assert EXECUTABLE_STAGE_NAMES == STAGE_NAMES[:-1]
    assert tuple(spec.stage_id for spec in STAGE_SPECS) == tuple(StageId)
    assert tuple(spec.number for spec in STAGE_SPECS) == tuple(range(1, 8))
    assert all(spec.layout is STAGE_LAYOUT_BY_ID[spec.stage_id] for spec in STAGE_SPECS)
    assert all(spec.manifest_filename == "manifest.json" for spec in STAGE_SPECS if spec.executable)
    assert STAGE_BY_NAME["openfoam"].manifest_filename is None


def test_registry_defines_automatic_run_selection() -> None:
    assert {
        spec.name: spec.selection
        for spec in STAGE_SPECS
    } == {
        "shapefiles": StageSelection.DEFAULT,
        "visual-enrichment": StageSelection.EXPLICIT,
        "point-cloud": StageSelection.DEFAULT,
        "city-models": StageSelection.DEFAULT,
        "trees": StageSelection.EXPLICIT,
        "air-purifiers": StageSelection.OPTIONAL,
        "openfoam": StageSelection.EXPLICIT,
    }
    assert OPTIONAL_STAGE_NAMES == ("air-purifiers",)


def test_stage_registry_owns_executable_runner_bindings() -> None:
    executable_specs = tuple(spec for spec in STAGE_SPECS if spec.executable)

    assert executable_specs
    assert all(callable(spec.runner) for spec in executable_specs)
    assert STAGE_BY_NAME["openfoam"].runner is None


def test_stage_registry_assigns_each_runtime_override_to_one_stage() -> None:
    destinations = [option.destination for spec in STAGE_SPECS for option in spec.cli_options]

    assert set(destinations) == {field.name for field in fields(StageRunOptions)}
    assert len(destinations) == len(set(destinations))


def test_point_cloud_registry_uses_shapefiles_as_default_not_dependency() -> None:
    spec = STAGE_BY_NAME["point-cloud"]

    assert spec.hard_dependencies == ()
    assert spec.input("building-footprints") == StageInputSpec(
        name="building-footprints",
        required=True,
        default_producer=StageId.SHAPEFILES,
        override="--building-footprints-geojson",
    )
    assert spec.input("stage-1-tree-points").required is False


def test_registry_records_hard_dependencies_and_maturity() -> None:
    assert STAGE_BY_NAME["city-models"].hard_dependencies == (
        StageId.SHAPEFILES,
        StageId.POINT_CLOUD,
    )
    assert STAGE_BY_NAME["visual-enrichment"].maturity is StageMaturity.REVIEW_ONLY
    assert STAGE_BY_NAME["trees"].maturity is StageMaturity.INCOMPLETE
    assert STAGE_BY_NAME["openfoam"].maturity is StageMaturity.PLANNED


def test_registry_output_directories_match_stage_planners(florence_config) -> None:
    for spec in STAGE_SPECS:
        plan = spec.planner(florence_config)
        assert plan.expected_outputs == (
            florence_config.output.root_directory / spec.number_name,
        )


def test_dry_run_includes_air_purifiers_between_trees_and_openfoam(florence_config) -> None:
    results = dry_run(florence_config)
    names = [result.stage for result in results]

    tree_index = names.index("trees")
    assert names[tree_index : tree_index + 3] == ["trees", "air-purifiers", "openfoam"]
    assert results[tree_index + 1].expected_outputs == (
        florence_config.output.root_directory / "06_air_purifiers",
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
