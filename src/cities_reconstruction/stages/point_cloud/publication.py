"""Artifact assembly and manifest publication for the point-cloud stage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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
class PointCloudPublicationInput:
    """Completed point-cloud artifacts and metadata ready for publication."""

    output_directory: Path
    report_path: Path
    preview_path: Path
    input_state_fingerprint: Mapping[str, JsonValue]
    projected_footprints_path: Path
    ground_points_path: Path
    building_points_path: Path
    tree_points_path: Path | None
    unclassified_points_path: Path
    diagnostics_path: Path
    ground_point_count: int
    building_point_count: int
    tree_point_count: int
    unclassified_point_count: int
    alignment_status: str
    source_building_footprints: Path
    crs: str
    tree_filter: Mapping[str, JsonValue]


def publish_point_cloud_manifest(publication: PointCloudPublicationInput) -> StageManifest:
    """Assemble the stable artifact contract and publish its manifest last."""

    artifacts = [
        ArtifactReference(
            "projected-building-footprints",
            publication.projected_footprints_path,
            ArtifactKind.HANDOFF,
        ),
        ArtifactReference("ground-points", publication.ground_points_path, ArtifactKind.HANDOFF),
        ArtifactReference("building-points", publication.building_points_path, ArtifactKind.HANDOFF),
    ]
    if publication.tree_points_path is not None:
        artifacts.append(
            ArtifactReference(
                "tree-points",
                publication.tree_points_path,
                ArtifactKind.HANDOFF,
                required=False,
            )
        )
    artifacts.extend(
        (
            ArtifactReference(
                "unclassified-points",
                publication.unclassified_points_path,
                ArtifactKind.DIAGNOSTIC,
            ),
            ArtifactReference(
                "alignment-diagnostics",
                publication.diagnostics_path,
                ArtifactKind.DIAGNOSTIC,
            ),
            ArtifactReference("report", publication.report_path, ArtifactKind.REPORT),
            ArtifactReference("preview", publication.preview_path, ArtifactKind.PREVIEW),
        )
    )
    return publish_stage_manifest(
        stage=StageId.POINT_CLOUD.value,
        status=StageStatus.COMPLETED,
        output_directory=publication.output_directory,
        report_path=publication.report_path,
        preview_path=publication.preview_path,
        input_state_fingerprint=dict(publication.input_state_fingerprint),
        artifacts=tuple(artifacts),
        metrics={
            "ground_point_count": publication.ground_point_count,
            "building_point_count": publication.building_point_count,
            "tree_point_count": publication.tree_point_count,
            "unclassified_point_count": publication.unclassified_point_count,
            "alignment_status": publication.alignment_status,
        },
        details={
            "source_building_footprints": str(publication.source_building_footprints),
            "crs": publication.crs,
            "tree_filter": dict(publication.tree_filter),
        },
    )
