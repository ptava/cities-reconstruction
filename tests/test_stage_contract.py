from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError
from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    StageManifest,
    StageStatus,
    invalidate_stage_manifests,
    load_stage_manifest,
    publish_stage_manifest,
    require_completed_manifest,
    require_manifest_artifact,
)


def test_artifact_reference_serializes_path() -> None:
    artifact = ArtifactReference(
        name="ground-points",
        path=Path("outputs/ground_points.ply"),
        kind=ArtifactKind.HANDOFF,
        required=True,
    )

    assert artifact.to_dict() == {
        "name": "ground-points",
        "path": "outputs/ground_points.ply",
        "kind": "handoff",
        "required": True,
    }


def test_status_values_are_stable() -> None:
    assert StageStatus.COMPLETED == "completed"
    assert StageStatus.FAILED_EXTERNAL_EXECUTION == "failed_external_execution"


def test_artifact_reference_rejects_blank_names_and_malformed_payloads() -> None:
    with pytest.raises(ConfigError, match="artifact name"):
        ArtifactReference(name=" ", path=Path("artifact.txt"), kind=ArtifactKind.HANDOFF)

    with pytest.raises(ConfigError, match="required"):
        ArtifactReference.from_dict(
            {"name": "artifact", "path": "artifact.txt", "kind": "handoff", "required": "yes"}
        )


def test_stage_manifest_round_trips_as_schema_v2(tmp_path: Path) -> None:
    manifest = _complete_manifest(tmp_path)

    payload = manifest.to_dict()

    assert payload["schema_version"] == 2
    assert payload["output_directory"] == str(manifest.output_directory)
    assert payload["artifacts"] == [
        {"name": "ground-points", "path": str(manifest.artifacts[0].path), "kind": "handoff", "required": True}
    ]
    assert StageManifest.from_dict(payload, manifest_path=manifest.manifest_path) == manifest


def test_stage_manifest_rejects_duplicate_artifact_names(tmp_path: Path) -> None:
    manifest = _complete_manifest(tmp_path)
    duplicate = ArtifactReference(
        name=manifest.artifacts[0].name,
        path=manifest.output_directory / "other.ply",
        kind=ArtifactKind.DIAGNOSTIC,
    )
    duplicate.path.write_text("points\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="duplicate artifact"):
        replace(manifest, artifacts=(*manifest.artifacts, duplicate))


def test_stage_manifest_rejects_missing_required_artifacts(tmp_path: Path) -> None:
    manifest = _complete_manifest(tmp_path)
    missing = ArtifactReference(
        name="missing-required",
        path=manifest.output_directory / "missing.ply",
        kind=ArtifactKind.HANDOFF,
    )

    with pytest.raises(ConfigError, match="missing required artifact"):
        replace(manifest, artifacts=(missing,))


def test_stage_manifest_allows_missing_optional_artifacts(tmp_path: Path) -> None:
    manifest = _complete_manifest(tmp_path)
    missing = ArtifactReference(
        name="missing-optional",
        path=manifest.output_directory / "missing-preview.png",
        kind=ArtifactKind.PREVIEW,
        required=False,
    )

    assert replace(manifest, artifacts=(missing,)).artifacts == (missing,)


def test_stage_manifest_copies_constructor_json_payloads(tmp_path: Path) -> None:
    input_state_fingerprint = {"inputs": [{"name": "original"}]}
    metrics = {"counts": [1]}
    details = {"source": {"name": "original"}}
    manifest = replace(
        _complete_manifest(tmp_path),
        input_state_fingerprint=input_state_fingerprint,
        metrics=metrics,
        details=details,
    )

    input_state_fingerprint["inputs"][0]["name"] = "changed"
    metrics["counts"].append(2)
    details["source"]["name"] = "changed"

    assert manifest.input_state_fingerprint == {"inputs": [{"name": "original"}]}
    assert manifest.metrics == {"counts": [1]}
    assert manifest.details == {"source": {"name": "original"}}


def test_stage_manifest_serialization_returns_defensive_json_payloads(tmp_path: Path) -> None:
    manifest = replace(
        _complete_manifest(tmp_path),
        input_state_fingerprint={"inputs": [{"name": "original"}]},
        metrics={"counts": [1]},
        details={"source": {"name": "original"}},
    )

    payload = manifest.to_dict()
    payload["input_state_fingerprint"]["inputs"][0]["name"] = "changed"
    payload["metrics"]["counts"].append(2)
    payload["details"]["source"]["name"] = "changed"

    assert manifest.input_state_fingerprint == {"inputs": [{"name": "original"}]}
    assert manifest.metrics == {"counts": [1]}
    assert manifest.details == {"source": {"name": "original"}}


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda payload: payload.update(schema_version=99), "schema"),
        (lambda payload: payload.update(status="unknown"), "status"),
        (
            lambda payload: payload["artifacts"][0].update(kind="unknown"),
            "kind",
        ),
        (lambda payload: payload.update(metrics=[]), "metrics"),
    ],
)
def test_load_stage_manifest_rejects_invalid_fields_with_manifest_path(
    tmp_path: Path, mutation: object, expected: str
) -> None:
    manifest = _complete_manifest(tmp_path)
    payload = manifest.to_dict()
    mutation(payload)  # type: ignore[operator]
    manifest.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match=str(manifest.manifest_path)) as error:
        load_stage_manifest(manifest.manifest_path)

    assert expected in str(error.value)


def test_load_stage_manifest_rejects_invalid_json_with_manifest_path(tmp_path: Path) -> None:
    manifest = _complete_manifest(tmp_path)
    manifest.manifest_path.write_text("{", encoding="utf-8")

    with pytest.raises(ConfigError, match=str(manifest.manifest_path)):
        load_stage_manifest(manifest.manifest_path)


def test_publish_and_load_stage_manifest(tmp_path: Path) -> None:
    output_directory = tmp_path / "stage-output"
    output_directory.mkdir()
    artifact_path = output_directory / "ground_points.ply"
    artifact_path.write_text("points\n", encoding="utf-8")

    manifest = publish_stage_manifest(
        stage="point-cloud",
        status=StageStatus.COMPLETED,
        output_directory=output_directory,
        report_path=output_directory / "report.md",
        preview_path=output_directory / "preview.png",
        input_state_fingerprint={"value": "abc"},
        artifacts=(ArtifactReference("ground-points", artifact_path, ArtifactKind.HANDOFF),),
        metrics={"points": 3},
        details={"source": "fixture"},
    )

    assert manifest.manifest_path == output_directory / "manifest.json"
    assert load_stage_manifest(manifest.manifest_path, expected_stage="point-cloud") == manifest

    with pytest.raises(ConfigError, match="expected stage"):
        load_stage_manifest(manifest.manifest_path, expected_stage="trees")


def test_load_stage_manifest_rejects_relocated_manifest(tmp_path: Path) -> None:
    manifest = _complete_manifest(tmp_path)
    manifest.manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    relocated_path = tmp_path / "relocated" / "manifest.json"
    relocated_path.parent.mkdir()
    relocated_path.write_text(manifest.manifest_path.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(ConfigError, match=str(relocated_path)):
        load_stage_manifest(relocated_path)


def test_load_stage_manifest_rejects_declared_output_directory_mismatch(tmp_path: Path) -> None:
    manifest = _complete_manifest(tmp_path)
    payload = manifest.to_dict()
    payload["output_directory"] = str(tmp_path / "other-output")
    manifest.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match=str(manifest.manifest_path)):
        load_stage_manifest(manifest.manifest_path)


@pytest.mark.parametrize("path_field", ["report_path", "preview_path", "artifact"])
def test_load_stage_manifest_rejects_paths_outside_output_directory(
    tmp_path: Path,
    path_field: str,
) -> None:
    manifest = _complete_manifest(tmp_path)
    outside_path = tmp_path / f"outside-{path_field}.txt"
    outside_path.write_text("outside\n", encoding="utf-8")
    payload = manifest.to_dict()
    if path_field == "artifact":
        payload["artifacts"][0]["path"] = str(manifest.output_directory / ".." / outside_path.name)
    else:
        payload[path_field] = str(manifest.output_directory / ".." / outside_path.name)
    manifest.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match=str(manifest.manifest_path)):
        load_stage_manifest(manifest.manifest_path)


def test_load_stage_manifest_rejects_artifact_symlink_escape(tmp_path: Path) -> None:
    manifest = _complete_manifest(tmp_path)
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    outside_artifact = outside_directory / "escaped.ply"
    outside_artifact.write_text("points\n", encoding="utf-8")
    symlink_directory = manifest.output_directory / "linked-outside"
    symlink_directory.symlink_to(outside_directory, target_is_directory=True)
    payload = manifest.to_dict()
    payload["artifacts"][0]["path"] = str(symlink_directory / outside_artifact.name)
    manifest.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match=str(manifest.manifest_path)):
        load_stage_manifest(manifest.manifest_path)


def test_require_completed_manifest_rejects_failed_external_execution(tmp_path: Path) -> None:
    output_directory = tmp_path / "stage-output"
    failed_manifest = publish_stage_manifest(
        stage="city-models",
        status=StageStatus.FAILED_EXTERNAL_EXECUTION,
        output_directory=output_directory,
        report_path=output_directory / "report.md",
        preview_path=output_directory / "preview.png",
        input_state_fingerprint={"value": "abc"},
        artifacts=(),
        metrics={},
        details={"error": "container exited 1"},
    )

    with pytest.raises(ConfigError, match="not completed"):
        require_completed_manifest(failed_manifest.manifest_path, expected_stage="city-models")


def test_require_manifest_artifact_returns_typed_named_handoff(tmp_path: Path) -> None:
    manifest = _complete_manifest(tmp_path)

    artifact = require_manifest_artifact(
        manifest,
        name="ground-points",
        kind=ArtifactKind.HANDOFF,
    )

    assert artifact is manifest.artifacts[0]


def test_require_manifest_artifact_rejects_unlisted_or_wrong_kind_with_manifest_path(
    tmp_path: Path,
) -> None:
    manifest = _complete_manifest(tmp_path)

    with pytest.raises(ConfigError, match=str(manifest.manifest_path)):
        require_manifest_artifact(manifest, name="missing", kind=ArtifactKind.HANDOFF)
    with pytest.raises(ConfigError, match=str(manifest.manifest_path)):
        require_manifest_artifact(manifest, name="ground-points", kind=ArtifactKind.DIAGNOSTIC)


def test_invalidate_stage_manifests_removes_only_allowlisted_files(tmp_path: Path) -> None:
    output_directory = tmp_path / "stage-output"
    output_directory.mkdir()
    manifest_path = output_directory / "manifest.json"
    legacy_path = output_directory / "legacy-summary.json"
    unrelated_path = output_directory / "user-notes.txt"
    manifest_path.write_text("manifest", encoding="utf-8")
    legacy_path.write_text("legacy", encoding="utf-8")
    unrelated_path.write_text("keep", encoding="utf-8")

    returned_path = invalidate_stage_manifests(output_directory, legacy_names=(legacy_path.name,))

    assert returned_path == manifest_path
    assert not manifest_path.exists()
    assert not legacy_path.exists()
    assert unrelated_path.read_text(encoding="utf-8") == "keep"


def test_invalidate_stage_manifests_rejects_non_filename_legacy_names(tmp_path: Path) -> None:
    output_directory = tmp_path / "stage-output"
    output_directory.mkdir()

    with pytest.raises(ConfigError, match="invalid manifest filename"):
        invalidate_stage_manifests(output_directory, legacy_names=("..",))


def _complete_manifest(tmp_path: Path) -> StageManifest:
    output_directory = tmp_path / "stage-output"
    output_directory.mkdir()
    artifact_path = output_directory / "ground_points.ply"
    artifact_path.write_text("points\n", encoding="utf-8")
    return StageManifest(
        schema_version=2,
        application_version="0.1.0",
        stage="point-cloud",
        status=StageStatus.COMPLETED,
        output_directory=output_directory,
        manifest_path=output_directory / "manifest.json",
        report_path=output_directory / "report.md",
        preview_path=output_directory / "preview.png",
        finished_at_utc="2026-08-19T10:00:00+00:00",
        input_state_fingerprint={"value": "abc"},
        artifacts=(ArtifactReference("ground-points", artifact_path, ArtifactKind.HANDOFF),),
        metrics={"points": 3},
        details={"source": "fixture"},
    )
