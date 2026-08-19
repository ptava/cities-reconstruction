"""Shared schema-v2 contract for pipeline stage outputs."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol, Self, SupportsIndex, TypeAlias, TypeVar, cast, overload, runtime_checkable

from cities_reconstruction import __version__
from cities_reconstruction.artifacts import atomic_write_json
from cities_reconstruction.config import ConfigError

MANIFEST_SCHEMA_VERSION = 2
_T = TypeVar("_T")

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class StageStatus(StrEnum):
    """Terminal status recorded by a pipeline stage."""

    COMPLETED = "completed"
    FAILED_EXTERNAL_EXECUTION = "failed_external_execution"


class ArtifactKind(StrEnum):
    """The role an artifact serves in a stage output."""

    HANDOFF = "handoff"
    REPORT = "report"
    PREVIEW = "preview"
    DIAGNOSTIC = "diagnostic"
    LOG = "log"
    SUPPORTING = "supporting"


@dataclass(frozen=True)
class ArtifactReference:
    """A named file produced by a stage."""

    name: str
    path: Path
    kind: ArtifactKind
    required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConfigError("artifact name must be a non-blank string")
        if not isinstance(self.path, Path):
            raise ConfigError("artifact path must be a Path")
        if not isinstance(self.kind, ArtifactKind):
            raise ConfigError("artifact kind must be an ArtifactKind")
        if type(self.required) is not bool:
            raise ConfigError("artifact required must be a boolean")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "path": str(self.path),
            "kind": self.kind.value,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, payload: object, *, context: str = "artifact reference") -> ArtifactReference:
        data = _expect_mapping(payload, context)
        _expect_exact_keys(data, {"name", "path", "kind", "required"}, context)
        name = _expect_nonblank_string(data["name"], f"{context} name")
        path = Path(_expect_nonblank_string(data["path"], f"{context} path"))
        kind_value = _expect_nonblank_string(data["kind"], f"{context} kind")
        try:
            kind = ArtifactKind(kind_value)
        except ValueError as exc:
            raise ConfigError(f"{context} kind is invalid: {kind_value!r}") from exc
        required = data["required"]
        if type(required) is not bool:
            raise ConfigError(f"{context} required must be a boolean")
        return cls(name=name, path=path, kind=kind, required=required)


@dataclass(frozen=True)
class StageManifest:
    """Immutable schema-v2 record for one stage publication."""

    schema_version: int
    application_version: str
    stage: str
    status: StageStatus
    output_directory: Path
    manifest_path: Path
    report_path: Path
    preview_path: Path
    finished_at_utc: str
    input_state_fingerprint: dict[str, JsonValue]
    artifacts: tuple[ArtifactReference, ...]
    metrics: dict[str, JsonValue]
    details: dict[str, JsonValue]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ConfigError(f"manifest schema_version must be {MANIFEST_SCHEMA_VERSION}")
        _expect_nonblank_string(self.application_version, "manifest application_version")
        _expect_nonblank_string(self.stage, "manifest stage")
        if not isinstance(self.status, StageStatus):
            raise ConfigError("manifest status must be a StageStatus")
        for field_name, value in (
            ("output_directory", self.output_directory),
            ("manifest_path", self.manifest_path),
            ("report_path", self.report_path),
            ("preview_path", self.preview_path),
        ):
            if not isinstance(value, Path):
                raise ConfigError(f"manifest {field_name} must be a Path")
        _validate_utc_timestamp(self.finished_at_utc)
        _validate_json_mapping(self.input_state_fingerprint, "manifest input_state_fingerprint")
        _validate_json_mapping(self.metrics, "manifest metrics")
        _validate_json_mapping(self.details, "manifest details")
        object.__setattr__(
            self,
            "input_state_fingerprint",
            _freeze_json_mapping(self.input_state_fingerprint),
        )
        object.__setattr__(self, "metrics", _freeze_json_mapping(self.metrics))
        object.__setattr__(self, "details", _freeze_json_mapping(self.details))
        if not isinstance(self.artifacts, tuple):
            raise ConfigError("manifest artifacts must be a tuple")
        artifact_names: set[str] = set()
        for artifact in self.artifacts:
            if not isinstance(artifact, ArtifactReference):
                raise ConfigError("manifest artifacts must contain ArtifactReference values")
            if artifact.name in artifact_names:
                raise ConfigError(f"manifest has duplicate artifact name: {artifact.name}")
            artifact_names.add(artifact.name)
            if artifact.required and not artifact.path.is_file():
                raise ConfigError(f"manifest missing required artifact: {artifact.path}")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "application_version": self.application_version,
            "stage": self.stage,
            "status": self.status.value,
            "output_directory": str(self.output_directory),
            "manifest_path": str(self.manifest_path),
            "report_path": str(self.report_path),
            "preview_path": str(self.preview_path),
            "finished_at_utc": self.finished_at_utc,
            "input_state_fingerprint": _thaw_json_mapping(self.input_state_fingerprint),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "metrics": _thaw_json_mapping(self.metrics),
            "details": _thaw_json_mapping(self.details),
        }

    @classmethod
    def from_dict(cls, payload: object, *, manifest_path: Path) -> StageManifest:
        context = f"stage manifest {manifest_path}"
        data = _expect_mapping(payload, context)
        _expect_exact_keys(
            data,
            {
                "schema_version",
                "application_version",
                "stage",
                "status",
                "output_directory",
                "manifest_path",
                "report_path",
                "preview_path",
                "finished_at_utc",
                "input_state_fingerprint",
                "artifacts",
                "metrics",
                "details",
            },
            context,
        )
        schema_version = data["schema_version"]
        if type(schema_version) is not int:
            raise ConfigError(f"{context} schema_version must be an integer")
        status_value = _expect_nonblank_string(data["status"], f"{context} status")
        try:
            status = StageStatus(status_value)
        except ValueError as exc:
            raise ConfigError(f"{context} status is invalid: {status_value!r}") from exc
        artifacts_data = data["artifacts"]
        if not isinstance(artifacts_data, list):
            raise ConfigError(f"{context} artifacts must be an array")
        artifacts = tuple(
            ArtifactReference.from_dict(item, context=f"{context} artifacts[{index}]")
            for index, item in enumerate(artifacts_data)
        )
        try:
            return cls(
                schema_version=schema_version,
                application_version=_expect_nonblank_string(
                    data["application_version"], f"{context} application_version"
                ),
                stage=_expect_nonblank_string(data["stage"], f"{context} stage"),
                status=status,
                output_directory=Path(
                    _expect_nonblank_string(data["output_directory"], f"{context} output_directory")
                ),
                manifest_path=Path(_expect_nonblank_string(data["manifest_path"], f"{context} manifest_path")),
                report_path=Path(_expect_nonblank_string(data["report_path"], f"{context} report_path")),
                preview_path=Path(_expect_nonblank_string(data["preview_path"], f"{context} preview_path")),
                finished_at_utc=_expect_nonblank_string(data["finished_at_utc"], f"{context} finished_at_utc"),
                input_state_fingerprint=_expect_json_mapping(
                    data["input_state_fingerprint"], f"{context} input_state_fingerprint"
                ),
                artifacts=artifacts,
                metrics=_expect_json_mapping(data["metrics"], f"{context} metrics"),
                details=_expect_json_mapping(data["details"], f"{context} details"),
            )
        except ConfigError as exc:
            if str(manifest_path) in str(exc):
                raise
            raise ConfigError(f"{context} is invalid: {exc}") from exc


@runtime_checkable
class StageOutput(Protocol):
    """Structural result contract implemented by each pipeline stage."""

    @property
    def manifest(self) -> StageManifest: ...

    @property
    def stage(self) -> str: ...

    @property
    def status(self) -> StageStatus: ...

    @property
    def output_directory(self) -> Path: ...

    @property
    def manifest_path(self) -> Path: ...

    @property
    def report_path(self) -> Path: ...

    @property
    def preview_path(self) -> Path: ...

    @property
    def artifacts(self) -> tuple[ArtifactReference, ...]: ...

    @property
    def metrics(self) -> dict[str, JsonValue]: ...

    @property
    def details(self) -> dict[str, JsonValue]: ...

    def to_dict(self) -> dict[str, JsonValue]: ...


def publish_stage_manifest(
    *,
    stage: str,
    status: StageStatus,
    output_directory: Path,
    report_path: Path,
    preview_path: Path,
    input_state_fingerprint: dict[str, JsonValue],
    artifacts: tuple[ArtifactReference, ...],
    metrics: dict[str, JsonValue],
    details: dict[str, JsonValue],
) -> StageManifest:
    """Validate and atomically publish a schema-v2 stage manifest."""

    manifest = StageManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        application_version=__version__,
        stage=stage,
        status=status,
        output_directory=output_directory,
        manifest_path=output_directory / "manifest.json",
        report_path=report_path,
        preview_path=preview_path,
        finished_at_utc=datetime.now(UTC).isoformat(),
        input_state_fingerprint=input_state_fingerprint,
        artifacts=artifacts,
        metrics=metrics,
        details=details,
    )
    atomic_write_json(manifest.manifest_path, manifest.to_dict())
    return manifest


def load_stage_manifest(path: Path, *, expected_stage: str | None = None) -> StageManifest:
    """Load one schema-v2 manifest, including the source path in validation errors."""

    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle, parse_constant=_reject_invalid_json_constant)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read stage manifest {path}: {exc}") from exc
    manifest = StageManifest.from_dict(payload, manifest_path=path)
    _validate_loaded_manifest_paths(manifest, path)
    if expected_stage is not None and manifest.stage != expected_stage:
        raise ConfigError(
            f"stage manifest {path} has stage {manifest.stage!r}; expected stage {expected_stage!r}"
        )
    return manifest


def _validate_loaded_manifest_paths(manifest: StageManifest, loaded_path: Path) -> None:
    """Bind a loaded manifest and all of its published files to one stage directory."""

    context = f"stage manifest {loaded_path}"
    loaded_resolved = _resolve_manifest_member_path(loaded_path, context)
    declared_manifest_resolved = _resolve_manifest_member_path(manifest.manifest_path, context)
    if declared_manifest_resolved != loaded_resolved:
        raise ConfigError(
            f"{context} declares manifest_path {manifest.manifest_path}, which does not resolve to the loaded path"
        )

    loaded_parent_resolved = _resolve_manifest_member_path(loaded_path.parent, context)
    output_resolved = _resolve_manifest_member_path(manifest.output_directory, context)
    if output_resolved != loaded_parent_resolved:
        raise ConfigError(
            f"{context} declares output_directory {manifest.output_directory}, "
            "which does not resolve to the loaded manifest parent"
        )

    confined_paths = (
        ("report_path", manifest.report_path),
        ("preview_path", manifest.preview_path),
        *((f"artifact {artifact.name!r}", artifact.path) for artifact in manifest.artifacts),
    )
    for label, candidate in confined_paths:
        if ".." in candidate.parts:
            raise ConfigError(f"{context} {label} contains parent traversal outside the stage contract: {candidate}")
        candidate_resolved = _resolve_manifest_member_path(candidate, context)
        if candidate_resolved == output_resolved or not candidate_resolved.is_relative_to(output_resolved):
            raise ConfigError(
                f"{context} {label} must resolve beneath output_directory {manifest.output_directory}: {candidate}"
            )


def _resolve_manifest_member_path(path: Path, context: str) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ConfigError(f"{context} cannot resolve path {path}: {exc}") from exc


def require_completed_manifest(path: Path, *, expected_stage: str) -> StageManifest:
    """Load a specific stage handoff and require successful completion."""

    manifest = load_stage_manifest(path, expected_stage=expected_stage)
    if manifest.status is not StageStatus.COMPLETED:
        raise ConfigError(f"stage manifest {path} is not completed: {manifest.status.value}")
    return manifest


def require_manifest_artifact(
    manifest: StageManifest,
    *,
    name: str,
    kind: ArtifactKind,
) -> ArtifactReference:
    """Select one declared artifact by stable name and expected typed role."""

    for artifact in manifest.artifacts:
        if artifact.name != name:
            continue
        if artifact.kind is not kind:
            raise ConfigError(
                f"stage manifest {manifest.manifest_path} artifact {name!r} has kind "
                f"{artifact.kind.value!r}; expected {kind.value!r}"
            )
        if not artifact.path.is_file():
            raise ConfigError(
                f"stage manifest {manifest.manifest_path} artifact {name!r} does not exist: {artifact.path}"
            )
        return artifact
    raise ConfigError(
        f"stage manifest {manifest.manifest_path} does not declare {kind.value} artifact {name!r}"
    )


def require_manifest_artifact_path(
    manifest: StageManifest,
    *,
    path: Path,
    kind: ArtifactKind,
) -> ArtifactReference:
    """Require an exact resolved artifact path with the expected typed role."""

    requested_path = _resolve_manifest_member_path(path, f"stage manifest {manifest.manifest_path}")
    for artifact in manifest.artifacts:
        artifact_path = _resolve_manifest_member_path(
            artifact.path,
            f"stage manifest {manifest.manifest_path}",
        )
        if artifact_path != requested_path:
            continue
        if artifact.kind is not kind:
            raise ConfigError(
                f"stage manifest {manifest.manifest_path} declares selected path {path} as "
                f"{artifact.kind.value}, not as a declared {kind.value}"
            )
        if not artifact.path.is_file():
            raise ConfigError(
                f"stage manifest {manifest.manifest_path} selected {kind.value} does not exist: {artifact.path}"
            )
        return artifact
    raise ConfigError(
        f"stage manifest {manifest.manifest_path} does not list selected path as a declared {kind.value}: {path}"
    )


def invalidate_stage_manifests(output_directory: Path, *, legacy_names: tuple[str, ...] = ()) -> Path:
    """Remove the current manifest and explicitly named legacy manifest files."""

    manifest_path = output_directory / "manifest.json"
    for name in (manifest_path.name, *legacy_names):
        _validate_manifest_filename(name)
        candidate = output_directory / name
        if candidate.is_file() or candidate.is_symlink():
            candidate.unlink()
    return manifest_path


def _expect_mapping(payload: object, context: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ConfigError(f"{context} must be an object")
    if any(not isinstance(key, str) for key in payload):
        raise ConfigError(f"{context} keys must be strings")
    return payload


def _expect_exact_keys(data: dict[str, object], expected: set[str], context: str) -> None:
    unknown = sorted(set(data) - expected)
    missing = sorted(expected - set(data))
    if unknown or missing:
        parts = []
        if unknown:
            parts.append(f"unknown fields: {', '.join(unknown)}")
        if missing:
            parts.append(f"missing fields: {', '.join(missing)}")
        raise ConfigError(f"{context} has invalid fields ({'; '.join(parts)})")


def _expect_nonblank_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} must be a non-blank string")
    return value


def _expect_json_mapping(value: object, context: str) -> dict[str, JsonValue]:
    data = _expect_mapping(value, context)
    _validate_json_mapping(data, context)
    return cast(dict[str, JsonValue], data)


def _validate_json_mapping(value: object, context: str) -> None:
    data = _expect_mapping(value, context)
    for key, item in data.items():
        _validate_json_value(item, f"{context}.{key}")


def _validate_json_value(value: object, context: str) -> None:
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ConfigError(f"{context} must be a valid JSON value")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{context}[{index}]")
        return
    if isinstance(value, dict):
        _validate_json_mapping(value, context)
        return
    raise ConfigError(f"{context} must be a valid JSON value")


def _freeze_json_mapping(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return _FrozenJsonDict(value)


def _freeze_json_value(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return _FrozenJsonDict(value)
    if isinstance(value, list):
        return _FrozenJsonList(value)
    return value


def _thaw_json_mapping(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: _thaw_json_value(item) for key, item in value.items()}


def _thaw_json_value(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return _thaw_json_mapping(value)
    if isinstance(value, list):
        return [_thaw_json_value(item) for item in value]
    return value


def _validate_utc_timestamp(value: object) -> None:
    timestamp = _expect_nonblank_string(value, "manifest finished_at_utc")
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ConfigError("manifest finished_at_utc must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ConfigError("manifest finished_at_utc must be an ISO-8601 UTC timestamp")


def _reject_invalid_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_manifest_filename(name: str) -> None:
    if not isinstance(name, str) or name in {"", ".", ".."} or Path(name).name != name:
        raise ConfigError(f"invalid manifest filename: {name!r}")


class _FrozenJsonDict(dict[str, JsonValue]):
    """Dict-compatible immutable storage for a validated JSON object."""

    def __init__(self, value: dict[str, JsonValue]) -> None:
        super().__init__((key, _freeze_json_value(item)) for key, item in value.items())

    def __setitem__(self, key: str, value: JsonValue) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")

    def __delitem__(self, key: str) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")

    def clear(self) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")

    @overload
    def pop(self, key: str, /) -> JsonValue: ...

    @overload
    def pop(self, key: str, default: _T, /) -> JsonValue | _T: ...

    def pop(self, key: str, default: object = None, /) -> object:
        raise TypeError("stage manifest JSON payloads are immutable")

    def popitem(self) -> tuple[str, JsonValue]:
        raise TypeError("stage manifest JSON payloads are immutable")

    @overload
    def setdefault(self, key: str, default: None = None, /) -> None: ...

    @overload
    def setdefault(self, key: str, default: JsonValue, /) -> JsonValue: ...

    def setdefault(self, key: str, default: JsonValue | None = None, /) -> JsonValue | None:
        raise TypeError("stage manifest JSON payloads are immutable")

    def update(self, *args: object, **kwargs: JsonValue) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")

    def __ior__(  # type: ignore[override, misc]
        self, value: Mapping[str, JsonValue] | Iterable[tuple[str, JsonValue]]
    ) -> Self:
        raise TypeError("stage manifest JSON payloads are immutable")


class _FrozenJsonList(list[JsonValue]):
    """List-compatible immutable storage for a validated JSON array."""

    def __init__(self, value: list[JsonValue]) -> None:
        super().__init__(_freeze_json_value(item) for item in value)

    def __setitem__(self, index: object, value: object) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")

    def __delitem__(self, index: object) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")

    def __iadd__(self, value: Iterable[JsonValue]) -> Self:  # type: ignore[override, misc]
        raise TypeError("stage manifest JSON payloads are immutable")

    def __imul__(self, value: SupportsIndex) -> Self:
        raise TypeError("stage manifest JSON payloads are immutable")

    def append(self, value: JsonValue) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")

    def clear(self) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")

    def extend(self, values: object) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")

    def insert(self, index: SupportsIndex, value: JsonValue) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")

    def pop(self, index: SupportsIndex = -1) -> JsonValue:
        raise TypeError("stage manifest JSON payloads are immutable")

    def remove(self, value: JsonValue) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")

    def reverse(self) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")

    def sort(self, *, key: object = None, reverse: bool = False) -> None:
        raise TypeError("stage manifest JSON payloads are immutable")
