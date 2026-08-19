"""Dry-run pipeline orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from .config import AppConfig, ConfigError
from .stage_result import StageResult
from .stages import air_purifiers, city_models, openfoam, point_cloud, shapefiles, trees, visual_enrichment

StagePlanner = Callable[[AppConfig], StageResult]


class StageMaturity(StrEnum):
    """Current implementation state exposed to users and tooling."""

    IMPLEMENTED = "implemented"
    REVIEW_ONLY = "review_only"
    INCOMPLETE = "incomplete"
    PLANNED = "planned"


@dataclass(frozen=True)
class StageInputSpec:
    """Describe one stage input without imposing a runtime dependency."""

    name: str
    required: bool
    default_producer: str | None = None
    override: str | None = None


@dataclass(frozen=True)
class StageSpec:
    """Authoritative metadata for one pipeline stage."""

    name: str
    order: int
    maturity: StageMaturity
    output_directory: str
    planner: StagePlanner
    executable: bool
    hard_dependencies: tuple[str, ...] = ()
    inputs: tuple[StageInputSpec, ...] = ()

    def input(self, name: str) -> StageInputSpec:
        """Return a named input contract or raise ``KeyError``."""

        for item in self.inputs:
            if item.name == name:
                return item
        raise KeyError(name)


STAGE_SPECS = (
    StageSpec(
        name="shapefiles",
        order=1,
        maturity=StageMaturity.IMPLEMENTED,
        output_directory="01_shapefiles",
        planner=shapefiles.plan,
        executable=True,
        inputs=(
            StageInputSpec("feature-data", required=True, override="--overpass-json"),
        ),
    ),
    StageSpec(
        name="visual-enrichment",
        order=2,
        maturity=StageMaturity.REVIEW_ONLY,
        output_directory="02_visual_enrichment",
        planner=visual_enrichment.plan,
        executable=True,
        hard_dependencies=("shapefiles",),
        inputs=(
            StageInputSpec("stage-1-features", required=True, default_producer="shapefiles"),
            StageInputSpec("segmentation-polygons", required=False, override="--segmentation-geojson"),
            StageInputSpec("sat2lod2-polygons", required=False, override="--sat2lod2-geojson"),
        ),
    ),
    StageSpec(
        name="point-cloud",
        order=3,
        maturity=StageMaturity.IMPLEMENTED,
        output_directory="02_point_cloud",
        planner=point_cloud.plan,
        executable=True,
        inputs=(
            StageInputSpec("dtm-directory", required=True, override="inputs.dtm_directory"),
            StageInputSpec("dsm-directory", required=True, override="inputs.dsm_directory"),
            StageInputSpec(
                "building-footprints",
                required=True,
                default_producer="shapefiles",
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
                default_producer="shapefiles",
            ),
        ),
    ),
    StageSpec(
        name="city-models",
        order=4,
        maturity=StageMaturity.IMPLEMENTED,
        output_directory="03_city_models",
        planner=city_models.plan,
        executable=True,
        hard_dependencies=("shapefiles", "point-cloud"),
        inputs=(
            StageInputSpec("stage-1-surfaces", required=True, default_producer="shapefiles"),
            StageInputSpec("point-cloud-manifest", required=True, default_producer="point-cloud"),
        ),
    ),
    StageSpec(
        name="trees",
        order=5,
        maturity=StageMaturity.INCOMPLETE,
        output_directory="04_trees",
        planner=trees.plan,
        executable=True,
        hard_dependencies=("shapefiles",),
        inputs=(
            StageInputSpec("tree-features", required=True, default_producer="shapefiles"),
            StageInputSpec("terrain-geometry", required=False, override="--tree-terrain-geometry"),
        ),
    ),
    StageSpec(
        name="air-purifiers",
        order=6,
        maturity=StageMaturity.IMPLEMENTED,
        output_directory="05_air_purifiers",
        planner=air_purifiers.plan,
        executable=True,
        hard_dependencies=("shapefiles",),
        inputs=(
            StageInputSpec("purifier-features", required=True, default_producer="shapefiles"),
            StageInputSpec("model-library", required=True, override="--model-library"),
            StageInputSpec("terrain-geometry", required=False, override="--terrain-geometry"),
        ),
    ),
    StageSpec(
        name="openfoam",
        order=7,
        maturity=StageMaturity.PLANNED,
        output_directory="05_openfoam_case",
        planner=openfoam.plan,
        executable=False,
        hard_dependencies=("city-models", "trees", "air-purifiers"),
    ),
)

STAGE_BY_NAME = {spec.name: spec for spec in STAGE_SPECS}
STAGE_PLANNERS: dict[str, StagePlanner] = {
    spec.name: spec.planner for spec in STAGE_SPECS
}
STAGE_NAMES = tuple(spec.name for spec in STAGE_SPECS)
EXECUTABLE_STAGE_NAMES = tuple(spec.name for spec in STAGE_SPECS if spec.executable)


def dry_run(config: AppConfig, stages: Iterable[str] | None = None) -> list[StageResult]:
    """Return planned work for the requested pipeline stages."""

    requested = STAGE_NAMES if stages is None else tuple(stages)
    unknown = [stage for stage in requested if stage not in STAGE_BY_NAME]
    if unknown:
        raise ConfigError(f"unknown pipeline stage: {', '.join(unknown)}")
    return [STAGE_BY_NAME[stage].planner(config) for stage in requested]
