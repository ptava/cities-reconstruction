"""Safe stage-output publication and lightweight provenance helpers."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator, TextIO
from uuid import uuid4

from cities_reconstruction import __version__
from cities_reconstruction.config import ConfigError


MANIFEST_SCHEMA_VERSION = 1
FINGERPRINT_KIND = "sha256-canonical-path-size-mtime-ns"


@contextmanager
def stage_output_lock(output_dir: Path, stage: str) -> Iterator[None]:
    """Reject concurrent writers for one stage output directory."""

    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / ".stage.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise ConfigError(f"{stage} output is locked by another run: {lock_path}") from exc
    lock_inode = os.fstat(descriptor).st_ino
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\nstage={stage}\n")
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            if lock_path.lstat().st_ino == lock_inode:
                lock_path.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def atomic_text_writer(path: Path) -> Iterator[TextIO]:
    """Write and fsync a same-directory temporary file before atomic replacement."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary_path.open("x", encoding="utf-8") as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str, *, mode: int | None = None) -> None:
    with atomic_text_writer(path) as handle:
        handle.write(content)
    if mode is not None:
        path.chmod(mode)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def lightweight_state_fingerprint(payload: dict[str, Any], paths: list[Path]) -> dict[str, Any]:
    """Fingerprint canonical metadata, not full potentially large raster contents."""

    path_state = []
    for path in sorted({item.resolve(strict=True) for item in paths}, key=str):
        stat = path.stat()
        path_state.append(
            {
                "path": str(path),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    canonical = json.dumps(
        {"payload": payload, "paths": path_state},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "kind": FINGERPRINT_KIND,
        "value": hashlib.sha256(canonical).hexdigest(),
        "path_count": len(path_state),
        "limitation": "Lightweight change detector; input file contents are not hashed.",
    }


def manifest_provenance(fingerprint: dict[str, Any]) -> dict[str, Any]:
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "application_version": __version__,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_state_fingerprint": fingerprint,
    }
