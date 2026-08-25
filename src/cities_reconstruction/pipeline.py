"""Dry-run pipeline orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from . import stage_runtime
from .cli_options import (
    AIR_PURIFIERS_CLI_OPTIONS,
    CITY_MODELS_CLI_OPTIONS,
    POINT_CLOUD_CLI_OPTIONS,
    SHAPEFILES_CLI_OPTIONS,
    TREES_CLI_OPTIONS,
    VISUAL_ENRICHMENT_CLI_OPTIONS,
    StageCliOption,
)
from .config import AppConfig, ConfigError
from .stage_layout import STAGE_LAYOUT_BY_ID, StageId, StageLayout
from .stage_result import StageResult
from .stage_runtime import StageRunner
from .stages import air_purifiers, city_models, openfoam, point_cloud, shapefiles, trees, visual_enrichment

StagePlanner = Callable[[AppConfig], StageResult]


class StageMaturity(StrEnum):
    """Current implementation state exposed to users and tooling."""

    IMPLEMENTED = "implemented"
    REVIEW_ONLY = "review_only"
    INCOMPLETE = "incomplete"
    PLANNED = "planned"


class StageSelection(StrEnum):
    """Describe how a stage participates in multi-stage execution."""

    DEFAULT = "default"
    OPTIONAL = "optional"
    EXPLICIT = "explicit"


@dataclass(frozen=True)
class StageInputSpec:
    """Describe one stage input without imposing a runtime dependency."""

    name: str
    required: bool
    default_producer: StageId | None = None
    override: str | None = None


@dataclass(frozen=True)
class StageSpec:
    """Authoritative metadata for one pipeline stage."""

    stage_id: StageId
    maturity: StageMaturity
    selection: StageSelection
    planner: StagePlanner
    runner: StageRunner | None
    manifest_filename: str | None = None
    hard_dependencies: tuple[StageId, ...] = ()
    inputs: tuple[StageInputSpec, ...] = ()
    cli_options: tuple[StageCliOption, ...] = ()

    @property
    def layout(self) -> StageLayout:
        """Return dependency-neutral layout metadata for this stage."""

        return STAGE_LAYOUT_BY_ID[self.stage_id]

    @property
    def name(self) -> str:
        """Return the stable CLI and manifest stage name."""

        return self.stage_id.value

    @property
    def number(self) -> int:
        """Return the stage's current presentation number."""

        return self.layout.number

    @property
    def number_name(self) -> str:
        """Return the derived numbered output-directory name."""

        return self.layout.number_name

    @property
    def executable(self) -> bool:
        """Return whether this stage has an execution adapter."""

        return self.runner is not None

    def input(self, name: str) -> StageInputSpec:
        """Return a named input contract or raise ``KeyError``."""

        for item in self.inputs:
            if item.name == name:
                return item
        raise KeyError(name)


STAGE_SPECS = (
    StageSpec(
        stage_id=StageId.SHAPEFILES,
        maturity=StageMaturity.IMPLEMENTED,
        selection=StageSelection.DEFAULT,
        planner=shapefiles.plan,
        runner=stage_runtime.run_shapefiles,
        manifest_filename="manifest.json",
        cli_options=SHAPEFILES_CLI_OPTIONS,
        inputs=(
            StageInputSpec("feature-data", required=True, override="--overpass-json"),
        ),
    ),
    StageSpec(
        stage_id=StageId.VISUAL_ENRICHMENT,
        maturity=StageMaturity.REVIEW_ONLY,
        selection=StageSelection.EXPLICIT,
        planner=visual_enrichment.plan,
        runner=stage_runtime.run_visual_enrichment,
        manifest_filename="manifest.json",
        hard_dependencies=(StageId.SHAPEFILES,),
        cli_options=VISUAL_ENRICHMENT_CLI_OPTIONS,
        inputs=(
            StageInputSpec("stage-1-features", required=True, default_producer=StageId.SHAPEFILES),
            StageInputSpec("segmentation-polygons", required=False, override="--segmentation-geojson"),
            StageInputSpec("sat2lod2-polygons", required=False, override="--sat2lod2-geojson"),
        ),
    ),
    StageSpec(
        stage_id=StageId.POINT_CLOUD,
        maturity=StageMaturity.IMPLEMENTED,
        selection=StageSelection.DEFAULT,
        planner=point_cloud.plan,
        runner=stage_runtime.run_point_cloud,
        manifest_filename="manifest.json",
        cli_options=POINT_CLOUD_CLI_OPTIONS,
        inputs=(
            StageInputSpec("dtm-directory", required=True, override="inputs.dtm_directory"),
            StageInputSpec("dsm-directory", required=True, override="inputs.dsm_directory"),
            StageInputSpec(
                "building-footprints",
                required=True,
                default_producer=StageId.SHAPEFILES,
                override="--building-footprints-geojson",
            ),
            StageInputSpec(
                "tree-canopy-overlay",
                required=False,
                override="--tree-canopy-overlay",
            ),
            StageInputSpec(
                "stage-1-tree-points",
                required=False,
                default_producer=StageId.SHAPEFILES,
            ),
        ),
    ),
    StageSpec(
        stage_id=StageId.CITY_MODELS,
        maturity=StageMaturity.IMPLEMENTED,
        selection=StageSelection.DEFAULT,
        planner=city_models.plan,
        runner=stage_runtime.run_city_models,
        manifest_filename="manifest.json",
        hard_dependencies=(StageId.SHAPEFILES, StageId.POINT_CLOUD),
        cli_options=CITY_MODELS_CLI_OPTIONS,
        inputs=(
            StageInputSpec("stage-1-surfaces", required=True, default_producer=StageId.SHAPEFILES),
            StageInputSpec("point-cloud-manifest", required=True, default_producer=StageId.POINT_CLOUD),
        ),
    ),
    StageSpec(
        stage_id=StageId.TREES,
        maturity=StageMaturity.INCOMPLETE,
        selection=StageSelection.EXPLICIT,
        planner=trees.plan,
        runner=stage_runtime.run_trees,
        manifest_filename="manifest.json",
        hard_dependencies=(StageId.SHAPEFILES,),
        cli_options=TREES_CLI_OPTIONS,
        inputs=(
            StageInputSpec("tree-features", required=True, default_producer=StageId.SHAPEFILES),
            StageInputSpec("terrain-geometry", required=False, override="--tree-terrain-geometry"),
        ),
    ),
    StageSpec(
        stage_id=StageId.AIR_PURIFIERS,
        maturity=StageMaturity.IMPLEMENTED,
        selection=StageSelection.OPTIONAL,
        planner=air_purifiers.plan,
        runner=stage_runtime.run_air_purifiers,
        manifest_filename="manifest.json",
        hard_dependencies=(StageId.SHAPEFILES,),
        cli_options=AIR_PURIFIERS_CLI_OPTIONS,
        inputs=(
            StageInputSpec("purifier-features", required=True, default_producer=StageId.SHAPEFILES),
            StageInputSpec("model-library", required=True, override="--model-library"),
            StageInputSpec("terrain-geometry", required=False, override="--terrain-geometry"),
        ),
    ),
    StageSpec(
        stage_id=StageId.OPENFOAM,
        maturity=StageMaturity.PLANNED,
        selection=StageSelection.EXPLICIT,
        planner=openfoam.plan,
        runner=None,
        manifest_filename=None,
        hard_dependencies=(StageId.CITY_MODELS, StageId.TREES, StageId.AIR_PURIFIERS),
    ),
)

STAGE_BY_NAME = {spec.name: spec for spec in STAGE_SPECS}
STAGE_PLANNERS: dict[str, StagePlanner] = {
    spec.name: spec.planner for spec in STAGE_SPECS
}
STAGE_NAMES = tuple(spec.name for spec in STAGE_SPECS)
EXECUTABLE_STAGE_NAMES = tuple(spec.name for spec in STAGE_SPECS if spec.executable)
OPTIONAL_STAGE_NAMES = tuple(
    spec.name
    for spec in STAGE_SPECS
    if spec.selection is StageSelection.OPTIONAL and spec.executable
)


def dry_run(config: AppConfig, stages: Iterable[str] | None = None) -> list[StageResult]:
    """Return planned work for the requested pipeline stages."""

    requested = STAGE_NAMES if stages is None else tuple(stages)
    unknown = [stage for stage in requested if stage not in STAGE_BY_NAME]
    if unknown:
        raise ConfigError(f"unknown pipeline stage: {', '.join(unknown)}")
    return [STAGE_BY_NAME[stage].planner(config) for stage in requested]
