"""Dry-run pipeline orchestration."""

from __future__ import annotations

from typing import Callable, Iterable

from .config import AppConfig, ConfigError
from .stage_result import StageResult
from .stages import air_purifiers, city_models, openfoam, point_cloud, shapefiles, trees, visual_enrichment


StagePlanner = Callable[[AppConfig], StageResult]

STAGE_PLANNERS: dict[str, StagePlanner] = {
    "shapefiles": shapefiles.plan,
    "visual-enrichment": visual_enrichment.plan,
    "point-cloud": point_cloud.plan,
    "city-models": city_models.plan,
    "trees": trees.plan,
    "air-purifiers": air_purifiers.plan,
    "openfoam": openfoam.plan,
}

STAGE_NAMES = tuple(STAGE_PLANNERS)


def dry_run(config: AppConfig, stages: Iterable[str] | None = None) -> list[StageResult]:
    """Return planned work for the requested pipeline stages."""

    requested = STAGE_NAMES if stages is None else tuple(stages)
    unknown = [stage for stage in requested if stage not in STAGE_PLANNERS]
    if unknown:
        raise ConfigError(f"unknown pipeline stage: {', '.join(unknown)}")
    return [STAGE_PLANNERS[stage](config) for stage in requested]
