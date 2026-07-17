"""Dry-run planning for OpenFOAM case generation."""

from __future__ import annotations

from cities_reconstruction.config import AppConfig
from cities_reconstruction.stage_result import StageResult


def plan(config: AppConfig) -> StageResult:
    output = config.output.root_directory / "05_openfoam_case"
    return StageResult(
        stage="openfoam",
        summary="Plan OpenFOAM Foundation v13 mesh-generation inputs.",
        planned_actions=(
            "Collect city and tree STL surfaces.",
            "Create future OpenFOAM dictionaries for blockMesh, surfaceFeatures, snappyHexMesh, and topoSet.",
            "Represent porous tree crown regions in the generated case plan.",
            "Prepare shell commands without running OpenFOAM.",
        ),
        expected_outputs=(output,),
    )
