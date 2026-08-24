"""Artifact assembly and manifest publication for the City4CFD stage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cities_reconstruction.adapters.city4cfd import City4CFDExecutionResult
from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    JsonValue,
    StageManifest,
    StageStatus,
    publish_stage_manifest,
)
from cities_reconstruction.stage_layout import StageId


@dataclass(frozen=True)
class CityModelsPublicationInput:
    """Completed City4CFD artifacts and metadata ready for publication."""

    status: StageStatus
    output_directory: Path
    report_path: Path
    preview_path: Path
    input_state_fingerprint: Mapping[str, JsonValue]
    city4cfd_config_path: Path
    footprint_diagnostics_path: Path
    run_script_path: Path
    building_preview_path: Path
    terrain_preview_path: Path
    building_mesh_path: Path
    terrain_mesh_path: Path
    combined_terrain_mesh_path: Path
    surface_mesh_paths: Mapping[str, Path]
    city_mesh_path: Path | None
    uses_separate_mesh_outputs: bool
    stdout_log_path: Path
    stderr_log_path: Path
    building_count: int
    alignment_status: str
    footprint_overlap_status: str
    region: str
    crs: str
    point_cloud_manifest_path: Path
    surface_layers: Sequence[Mapping[str, Any]]
    execution: City4CFDExecutionResult


def publish_city_models_manifest(publication: CityModelsPublicationInput) -> StageManifest:
    """Assemble the stable artifact contract and publish its manifest last."""

    surface_layer_details: list[JsonValue] = [
        {
            "category": layer["category"],
            "layer_name": layer["layer_name"],
            "source_path": layer["source_path"],
            "layer_path": layer["layer_path"],
            "config_path": layer["config_path"],
            "feature_count": layer["feature_count"],
            "mesh_path": (
                str(publication.surface_mesh_paths[layer["category"]])
                if layer["category"] in publication.surface_mesh_paths
                else None
            ),
            "mesh_exists": (
                publication.surface_mesh_paths[layer["category"]].exists()
                if layer["category"] in publication.surface_mesh_paths
                else False
            ),
        }
        for layer in publication.surface_layers
    ]
    artifacts = [
        ArtifactReference(
            "city4cfd-config",
            publication.city4cfd_config_path,
            ArtifactKind.SUPPORTING,
        ),
        ArtifactReference(
            "footprint-diagnostics",
            publication.footprint_diagnostics_path,
            ArtifactKind.DIAGNOSTIC,
        ),
        ArtifactReference("run-script", publication.run_script_path, ArtifactKind.LOG),
        ArtifactReference(
            "building-preview-surface",
            publication.building_preview_path,
            ArtifactKind.PREVIEW,
        ),
        ArtifactReference(
            "terrain-preview-surface",
            publication.terrain_preview_path,
            ArtifactKind.PREVIEW,
        ),
        *(
            (
                ArtifactReference(
                    "building-mesh",
                    publication.building_mesh_path,
                    ArtifactKind.HANDOFF,
                    required=publication.execution.succeeded,
                ),
                ArtifactReference(
                    "terrain-mesh",
                    publication.terrain_mesh_path,
                    ArtifactKind.HANDOFF,
                    required=publication.execution.succeeded,
                ),
                ArtifactReference(
                    "combined-terrain-mesh",
                    publication.combined_terrain_mesh_path,
                    ArtifactKind.HANDOFF,
                    required=(publication.execution.succeeded and publication.combined_terrain_mesh_path.is_file()),
                ),
                *(
                    ArtifactReference(
                        f"surface-mesh-{category}",
                        path,
                        ArtifactKind.HANDOFF,
                        required=False,
                    )
                    for category, path in sorted(publication.surface_mesh_paths.items())
                ),
            )
            if publication.uses_separate_mesh_outputs and publication.execution.status != "external_failed"
            else ()
        ),
        *(
            (
                ArtifactReference(
                    "city-mesh",
                    publication.city_mesh_path,
                    ArtifactKind.HANDOFF,
                    required=publication.execution.succeeded,
                ),
            )
            if publication.city_mesh_path is not None and publication.execution.status != "external_failed"
            else ()
        ),
        ArtifactReference("stdout-log", publication.stdout_log_path, ArtifactKind.LOG),
        ArtifactReference("stderr-log", publication.stderr_log_path, ArtifactKind.LOG),
        ArtifactReference("report", publication.report_path, ArtifactKind.REPORT),
        ArtifactReference("preview", publication.preview_path, ArtifactKind.PREVIEW),
    ]
    return publish_stage_manifest(
        stage=StageId.CITY_MODELS.value,
        status=publication.status,
        output_directory=publication.output_directory,
        report_path=publication.report_path,
        preview_path=publication.preview_path,
        input_state_fingerprint=dict(publication.input_state_fingerprint),
        artifacts=tuple(artifacts),
        metrics={
            "building_count": publication.building_count,
            "alignment_status": publication.alignment_status,
            "footprint_overlap_status": publication.footprint_overlap_status,
        },
        details={
            "region": publication.region,
            "crs": publication.crs,
            "point_cloud_manifest": str(publication.point_cloud_manifest_path),
            "required_external_tool": "City4CFD with OpenFOAM-compatible dependencies",
            "surface_layers": surface_layer_details,
            "city4cfd_generated_surfaces": {
                "city": str(publication.city_mesh_path) if publication.city_mesh_path is not None else None,
                "buildings": (str(publication.building_mesh_path) if publication.uses_separate_mesh_outputs else None),
                "terrain": (str(publication.terrain_mesh_path) if publication.uses_separate_mesh_outputs else None),
                "combined_terrain": (
                    str(publication.combined_terrain_mesh_path) if publication.uses_separate_mesh_outputs else None
                ),
                "surface_layers": {category: str(path) for category, path in publication.surface_mesh_paths.items()},
            },
            "city4cfd_execution": {
                "status": publication.execution.status,
                "backend": publication.execution.backend,
                "argv": list(publication.execution.argv),
                "return_code": publication.execution.return_code,
                "stdout_log": str(publication.stdout_log_path),
                "stderr_log": str(publication.stderr_log_path),
                "stdout_truncated": publication.execution.stdout_truncated,
                "stderr_truncated": publication.execution.stderr_truncated,
            },
        },
    )
