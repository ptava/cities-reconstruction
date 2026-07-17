"""Isolated City4CFD process discovery and execution."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from typing import Protocol

from cities_reconstruction.config import ConfigError, validate_city4cfd_docker_image


DEFAULT_CITY4CFD_DOCKER_IMAGE = "tudelft3d/city4cfd:0.8.0"
MAX_CAPTURE_BYTES = 1_048_576


@dataclass(frozen=True)
class City4CFDExecutionRequest:
    config_path: Path
    working_directory: Path
    output_directory_name: str
    docker_image: str | None


@dataclass(frozen=True)
class City4CFDExecutionResult:
    status: str
    backend: str | None
    argv: tuple[str, ...]
    return_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status in {"native_succeeded", "docker_succeeded"}


class City4CFDExecutor(Protocol):
    def execute(self, request: City4CFDExecutionRequest) -> City4CFDExecutionResult:
        """Discover and execute one backend, returning a structured result."""


class SubprocessCity4CFDExecutor:
    """Default executor; all process discovery and subprocess calls live here."""

    def execute(self, request: City4CFDExecutionRequest) -> City4CFDExecutionResult:
        executable = shutil.which("city4cfd")
        if executable is not None:
            argv = (
                executable,
                str(request.config_path),
                "--output_dir",
                request.output_directory_name,
            )
            return self._run("native", argv, request.working_directory)

        docker = shutil.which("docker")
        if docker is None:
            return City4CFDExecutionResult(
                status="unavailable_handoff",
                backend=None,
                argv=(),
                return_code=None,
                stdout="",
                stderr="city4cfd and docker are both unavailable\n",
            )

        working_directory = request.working_directory.resolve()
        mount_root = working_directory.parent
        try:
            relative_config = request.config_path.resolve().relative_to(mount_root)
        except ValueError as exc:
            raise ConfigError(
                f"city4cfd config path {request.config_path} must be inside "
                f"the execution directory {request.working_directory}"
            ) from exc
        docker_workdir = f"/work/{working_directory.name}"
        image = validate_city4cfd_docker_image(
            request.docker_image
            or os.environ.get("CITY4CFD_DOCKER_IMAGE")
            or DEFAULT_CITY4CFD_DOCKER_IMAGE
        )
        argv = (
            docker,
            "run",
            "--rm",
            "-v",
            f"{mount_root}:/work",
            "-w",
            docker_workdir,
            image,
            "city4cfd",
            f"/work/{relative_config.as_posix()}",
            "--output_dir",
            request.output_directory_name,
        )
        return self._run("docker", argv, working_directory)

    def _run(
        self,
        backend: str,
        argv: tuple[str, ...],
        cwd: Path,
    ) -> City4CFDExecutionResult:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                completed = subprocess.run(
                    argv,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    check=False,
                )
                return_code = completed.returncode
                execution_error = ""
            except OSError as exc:
                return_code = None
                execution_error = f"unable to execute {backend} backend: {exc}\n"
            stdout, stdout_truncated = _read_bounded(stdout_file)
            stderr, stderr_truncated = _read_bounded(stderr_file)

        if execution_error:
            stderr += execution_error
        succeeded_status = "native_succeeded" if backend == "native" else "docker_succeeded"
        return City4CFDExecutionResult(
            status=succeeded_status if return_code == 0 else "external_failed",
            backend=backend,
            argv=argv,
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


def _read_bounded(handle) -> tuple[str, bool]:
    handle.seek(0)
    data = handle.read(MAX_CAPTURE_BYTES + 1)
    truncated = len(data) > MAX_CAPTURE_BYTES
    return data[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace"), truncated


def render_handoff_script(request: City4CFDExecutionRequest) -> str:
    """Render a reproducible POSIX shell handoff with quoted dynamic values."""

    working_directory = request.working_directory.resolve()
    mount_root = working_directory.parent
    try:
        config_name = request.config_path.resolve().relative_to(working_directory).as_posix()
    except ValueError as exc:
        raise ConfigError(
            f"city4cfd config path {request.config_path} must be inside "
            f"the execution directory {request.working_directory}"
        ) from exc
    image = validate_city4cfd_docker_image(
        request.docker_image
        or os.environ.get("CITY4CFD_DOCKER_IMAGE")
        or DEFAULT_CITY4CFD_DOCKER_IMAGE
    )
    docker_workdir = f"/work/{working_directory.name}"
    q = shlex.quote
    native = " ".join(
        q(value)
        for value in ("city4cfd", config_name, "--output_dir", request.output_directory_name)
    )
    docker = " ".join(
        q(value)
        for value in (
            "docker",
            "run",
            "--rm",
            "-v",
            f"{mount_root}:/work",
            "-w",
            docker_workdir,
            image,
            "city4cfd",
            f"{docker_workdir}/{config_name}",
            "--output_dir",
            request.output_directory_name,
        )
    )
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"cd {q(str(working_directory))}\n"
        "if command -v city4cfd >/dev/null 2>&1; then\n"
        f"  mkdir -p {q(request.output_directory_name)}\n"
        f"  {native}\n"
        "elif command -v docker >/dev/null 2>&1; then\n"
        f"  mkdir -p {q(request.output_directory_name)}\n"
        f"  {docker}\n"
        "else\n"
        "  echo 'city4cfd and docker are both unavailable' >&2\n"
        "  exit 1\n"
        "fi\n"
    )
