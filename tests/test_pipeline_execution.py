from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.pipeline import STAGE_BY_NAME
from cities_reconstruction.pipeline_execution import (
    execute_pipeline,
    resolve_execution_plan,
)
from cities_reconstruction.stage_contract import (
    ArtifactReference,
    JsonValue,
    StageManifest,
    StageStatus,
)
from cities_reconstruction.stage_layout import StageId
from cities_reconstruction.stage_runtime import StageRunOptions

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class FakeStageOutput:
    manifest: StageManifest

    @property
    def stage(self) -> str:
        return self.manifest.stage

    @property
    def status(self) -> StageStatus:
        return self.manifest.status

    @property
    def output_directory(self) -> Path:
        return self.manifest.output_directory

    @property
    def manifest_path(self) -> Path:
        return self.manifest.manifest_path

    @property
    def report_path(self) -> Path:
        return self.manifest.report_path

    @property
    def preview_path(self) -> Path:
        return self.manifest.preview_path

    @property
    def artifacts(self) -> tuple[ArtifactReference, ...]:
        return self.manifest.artifacts

    @property
    def metrics(self) -> dict[str, JsonValue]:
        return self.manifest.metrics

    @property
    def details(self) -> dict[str, JsonValue]:
        return self.manifest.details

    def to_dict(self) -> dict[str, JsonValue]:
        return self.manifest.to_dict()


def _stage_output(stage: str, status: StageStatus = StageStatus.COMPLETED) -> FakeStageOutput:
    output_directory = Path("outputs") / stage
    return FakeStageOutput(
        StageManifest(
            schema_version=2,
            application_version="test",
            stage=stage,
            status=status,
            output_directory=output_directory,
            manifest_path=output_directory / "manifest.json",
            report_path=output_directory / "report.md",
            preview_path=output_directory / "preview.html",
            finished_at_utc="2026-08-20T00:00:00+00:00",
            input_state_fingerprint={},
            artifacts=(),
            metrics={},
            details={},
        )
    )


def test_default_execution_plan_contains_only_the_core_chain() -> None:
    assert resolve_execution_plan().stage_names == (
        "shapefiles",
        "point-cloud",
        "city-models",
    )


def test_optional_air_purifiers_extend_the_default_plan() -> None:
    assert resolve_execution_plan(includes=["air-purifiers"]).stage_names == (
        "shapefiles",
        "point-cloud",
        "city-models",
        "air-purifiers",
    )


def test_target_adds_required_default_producer() -> None:
    assert resolve_execution_plan(target="point-cloud").stage_names == (
        "shapefiles",
        "point-cloud",
    )


def test_explicit_input_replaces_required_default_producer() -> None:
    assert resolve_execution_plan(
        target="point-cloud",
        supplied_overrides=["--building-footprints-geojson"],
    ).stage_names == ("point-cloud",)


def test_optional_input_does_not_restore_replaced_default_producer() -> None:
    plan = resolve_execution_plan(
        target="point-cloud",
        supplied_overrides=["--building-footprints-geojson"],
    )

    assert StageId.SHAPEFILES not in plan.stage_ids


def test_explicit_review_only_target_resolves_its_dependency() -> None:
    assert resolve_execution_plan(target="visual-enrichment").stage_names == (
        "shapefiles",
        "visual-enrichment",
    )


def test_unknown_optional_stage_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown optional pipeline stage: missing"):
        resolve_execution_plan(includes=["missing"])


def test_non_optional_include_is_rejected() -> None:
    with pytest.raises(ConfigError, match="pipeline stage is not optional: trees"):
        resolve_execution_plan(includes=["trees"])


def test_non_executable_target_is_rejected() -> None:
    with pytest.raises(ConfigError, match="pipeline stage is not executable: openfoam"):
        resolve_execution_plan(target="openfoam")


def test_dependency_cycle_is_rejected(monkeypatch) -> None:
    monkeypatch.setitem(
        STAGE_BY_NAME,
        "shapefiles",
        replace(
            STAGE_BY_NAME["shapefiles"],
            hard_dependencies=(StageId.POINT_CLOUD,),
        ),
    )

    with pytest.raises(ConfigError, match="pipeline dependency cycle") as exc_info:
        resolve_execution_plan(target="point-cloud")

    assert getattr(exc_info.value, "category", None) == "planning"


def test_execute_pipeline_runs_in_order_and_aggregates_typed_results(monkeypatch) -> None:
    config = load_config(ROOT / "config/examples/florence.toml")
    plan = resolve_execution_plan()
    options = StageRunOptions(tree_canopy_overlay=Path("canopy.png"))
    calls: list[tuple[str, StageRunOptions]] = []

    for stage_name in plan.stage_names:
        def runner(_config, received_options, *, name=stage_name):
            calls.append((name, received_options))
            return _stage_output(name)

        monkeypatch.setitem(
            STAGE_BY_NAME,
            stage_name,
            replace(STAGE_BY_NAME[stage_name], runner=runner),
        )

    execution = execute_pipeline(config, plan, options)

    assert calls == [(stage_name, options) for stage_name in plan.stage_names]
    assert execution.completed is True
    assert execution.to_dict() == {
        "plan": ["shapefiles", "point-cloud", "city-models"],
        "results": [
            _stage_output("shapefiles").to_dict(),
            _stage_output("point-cloud").to_dict(),
            _stage_output("city-models").to_dict(),
        ],
    }


def test_execute_pipeline_stops_after_non_completed_result(monkeypatch) -> None:
    config = load_config(ROOT / "config/examples/florence.toml")
    plan = resolve_execution_plan()
    calls: list[str] = []

    def completed_runner(_config, _options):
        calls.append("shapefiles")
        return _stage_output("shapefiles")

    def failed_runner(_config, _options):
        calls.append("point-cloud")
        return _stage_output("point-cloud", StageStatus.FAILED_EXTERNAL_EXECUTION)

    def uncalled_runner(_config, _options):
        calls.append("city-models")
        return _stage_output("city-models")

    for stage_name, runner in (
        ("shapefiles", completed_runner),
        ("point-cloud", failed_runner),
        ("city-models", uncalled_runner),
    ):
        monkeypatch.setitem(
            STAGE_BY_NAME,
            stage_name,
            replace(STAGE_BY_NAME[stage_name], runner=runner),
        )

    execution = execute_pipeline(config, plan, StageRunOptions())

    assert calls == ["shapefiles", "point-cloud"]
    assert [result.stage for result in execution.results] == ["shapefiles", "point-cloud"]
    assert execution.completed is False


def test_execute_pipeline_propagates_configuration_errors(monkeypatch) -> None:
    config = load_config(ROOT / "config/examples/florence.toml")
    plan = resolve_execution_plan(target="shapefiles")

    def fail(_config, _options):
        raise ConfigError("runner configuration failed")

    monkeypatch.setitem(
        STAGE_BY_NAME,
        "shapefiles",
        replace(STAGE_BY_NAME["shapefiles"], runner=fail),
    )

    with pytest.raises(ConfigError, match="runner configuration failed"):
        execute_pipeline(config, plan, StageRunOptions())
