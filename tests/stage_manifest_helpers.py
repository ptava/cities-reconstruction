from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    StageManifest,
    StageStatus,
    publish_stage_manifest,
)


def publish_test_stage_manifest(
    output_directory: Path,
    *,
    stage: str,
    named_artifacts: Mapping[str, tuple[Path, ArtifactKind]],
    status: StageStatus = StageStatus.COMPLETED,
) -> StageManifest:
    output_directory.mkdir(parents=True, exist_ok=True)
    report_path = output_directory / "fixture_report.md"
    preview_path = output_directory / "fixture_preview.html"
    report_path.write_text("fixture report\n", encoding="utf-8")
    preview_path.write_text("<p>fixture preview</p>\n", encoding="utf-8")
    return publish_stage_manifest(
        stage=stage,
        status=status,
        output_directory=output_directory,
        report_path=report_path,
        preview_path=preview_path,
        input_state_fingerprint={"fixture": stage},
        artifacts=tuple(
            ArtifactReference(name, path, kind)
            for name, (path, kind) in named_artifacts.items()
        ),
        metrics={},
        details={},
    )
