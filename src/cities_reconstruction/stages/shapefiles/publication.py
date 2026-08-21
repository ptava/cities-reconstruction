"""Artifact assembly and manifest publication for the shapefiles stage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    JsonValue,
    StageManifest,
    StageStatus,
    publish_stage_manifest,
)
from cities_reconstruction.stage_layout import StageId

from .inputs import imagery_source_slug


@dataclass(frozen=True)
class ShapefilesPublicationInput:
    """Completed shapefiles artifacts and metadata ready for publication."""

    output_directory: Path
    report_path: Path
    preview_path: Path
    input_state_fingerprint: Mapping[str, JsonValue]
    all_features_path: Path
    urban_planning_path: Path
    air_purifiers_path: Path
    category_paths: Mapping[str, Path]
    region_paths: Mapping[str, Path]
    tag_inventory_query_path: Path
    tag_inventory_raw_path: Path
    tag_inventory_path: Path
    query_path: Path
    raw_path: Path
    diagnostics_path: Path
    diagnostics_geojson_path: Path
    imagery_diagnostics_path: Path
    imagery_overlay_path: Path
    imagery_diagnostics: Mapping[str, Any]
    summary_path: Path
    raw_element_count: int
    accepted_feature_count: int
    skipped_feature_count: int
    source: str


def publish_shapefiles_manifest(publication: ShapefilesPublicationInput) -> StageManifest:
    """Assemble the stable artifact contract and publish its manifest last."""

    artifacts = (
        ArtifactReference("all-features", publication.all_features_path, ArtifactKind.HANDOFF),
        ArtifactReference("urban-planning", publication.urban_planning_path, ArtifactKind.HANDOFF),
        ArtifactReference("air-purifiers", publication.air_purifiers_path, ArtifactKind.HANDOFF),
        *(
            ArtifactReference(
                f"category-{category.replace('_', '-')}",
                path,
                ArtifactKind.HANDOFF,
            )
            for category, path in sorted(publication.category_paths.items())
        ),
        *(
            ArtifactReference(
                f"region-{region.replace('_', '-')}",
                path,
                ArtifactKind.HANDOFF,
            )
            for region, path in sorted(publication.region_paths.items())
        ),
        ArtifactReference(
            "tag-inventory-query",
            publication.tag_inventory_query_path,
            ArtifactKind.DIAGNOSTIC,
        ),
        ArtifactReference(
            "tag-inventory-raw",
            publication.tag_inventory_raw_path,
            ArtifactKind.DIAGNOSTIC,
        ),
        ArtifactReference("tag-inventory", publication.tag_inventory_path, ArtifactKind.SUPPORTING),
        ArtifactReference("overpass-query", publication.query_path, ArtifactKind.DIAGNOSTIC),
        ArtifactReference("overpass-raw", publication.raw_path, ArtifactKind.DIAGNOSTIC),
        ArtifactReference(
            "geometry-diagnostics",
            publication.diagnostics_path,
            ArtifactKind.DIAGNOSTIC,
        ),
        ArtifactReference(
            "non-contributing-features",
            publication.diagnostics_geojson_path,
            ArtifactKind.DIAGNOSTIC,
        ),
        ArtifactReference(
            "imagery-diagnostics",
            publication.imagery_diagnostics_path,
            ArtifactKind.DIAGNOSTIC,
        ),
        ArtifactReference(
            "imagery-overlay",
            publication.imagery_overlay_path,
            ArtifactKind.DIAGNOSTIC,
        ),
        *_imagery_evidence_artifacts(publication.imagery_diagnostics),
        ArtifactReference("summary", publication.summary_path, ArtifactKind.SUPPORTING),
        ArtifactReference("report", publication.report_path, ArtifactKind.REPORT),
        ArtifactReference("preview", publication.preview_path, ArtifactKind.PREVIEW),
    )
    return publish_stage_manifest(
        stage=StageId.SHAPEFILES.value,
        status=StageStatus.COMPLETED,
        output_directory=publication.output_directory,
        report_path=publication.report_path,
        preview_path=publication.preview_path,
        input_state_fingerprint=dict(publication.input_state_fingerprint),
        artifacts=artifacts,
        metrics={
            "raw_element_count": publication.raw_element_count,
            "accepted_feature_count": publication.accepted_feature_count,
            "skipped_feature_count": publication.skipped_feature_count,
        },
        details={
            "source": publication.source,
            "categories": list[JsonValue](sorted(publication.category_paths)),
            "regions": list[JsonValue](sorted(publication.region_paths)),
        },
    )


def _imagery_evidence_artifacts(
    imagery_diagnostics: Mapping[str, Any],
) -> tuple[ArtifactReference, ...]:
    """List only imagery evidence files that this run actually generated."""

    records = imagery_diagnostics.get("sources")
    if not isinstance(records, list):
        return ()
    artifacts: list[ArtifactReference] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        source_name = imagery_source_slug(str(record.get("name", f"source-{index}")))
        candidates: list[tuple[str, str, ArtifactKind]] = [
            ("request", "request_url_path", ArtifactKind.DIAGNOSTIC),
        ]
        if record.get("status") == "fetched":
            candidates.append(("image", "image_path", ArtifactKind.SUPPORTING))
        elif record.get("status") == "error":
            candidates.append(("error", "error_path", ArtifactKind.DIAGNOSTIC))
        for role, field, kind in candidates:
            raw_path = record.get(field)
            if not isinstance(raw_path, str):
                continue
            path = Path(raw_path)
            if path.is_file():
                artifacts.append(
                    ArtifactReference(
                        f"imagery-{source_name}-{index}-{role}",
                        path,
                        kind,
                    )
                )
    return tuple(artifacts)
