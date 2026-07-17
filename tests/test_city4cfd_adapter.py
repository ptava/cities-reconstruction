from __future__ import annotations

from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest

from cities_reconstruction.adapters import city4cfd
from cities_reconstruction.config import ConfigError


def _request(tmp_path: Path, docker_image: str | None = None) -> city4cfd.City4CFDExecutionRequest:
    working = tmp_path / "stage with spaces"
    working.mkdir()
    config = working / "config name.json"
    config.write_text("{}", encoding="utf-8")
    return city4cfd.City4CFDExecutionRequest(
        config_path=config,
        working_directory=working,
        output_directory_name="city4cfd output",
        docker_image=docker_image,
    )


def test_native_adapter_uses_exact_argv_and_bounded_logs(tmp_path: Path, monkeypatch) -> None:
    request = _request(tmp_path)
    captured = {}
    monkeypatch.setattr(city4cfd.shutil, "which", lambda name: "/opt/bin/city4cfd" if name == "city4cfd" else None)

    def fake_run(argv, **kwargs):
        captured["argv"] = tuple(argv)
        captured.update(kwargs)
        kwargs["stdout"].write(b"x" * (city4cfd.MAX_CAPTURE_BYTES + 20))
        kwargs["stderr"].write(b"warning\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(city4cfd.subprocess, "run", fake_run)

    result = city4cfd.SubprocessCity4CFDExecutor().execute(request)

    assert captured["argv"] == (
        "/opt/bin/city4cfd",
        str(request.config_path),
        "--output_dir",
        "city4cfd output",
    )
    assert captured["cwd"] == request.working_directory
    assert captured["shell"] is False
    assert captured["check"] is False
    assert result.status == "native_succeeded"
    assert result.stdout_truncated is True
    assert len(result.stdout) == city4cfd.MAX_CAPTURE_BYTES


def test_docker_adapter_uses_pinned_default_and_exact_mount(tmp_path: Path, monkeypatch) -> None:
    request = _request(tmp_path)
    captured = {}
    monkeypatch.delenv("CITY4CFD_DOCKER_IMAGE", raising=False)
    monkeypatch.setattr(
        city4cfd.shutil,
        "which",
        lambda name: None if name == "city4cfd" else "/usr/bin/docker",
    )

    def fake_run(argv, **kwargs):
        captured["argv"] = tuple(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(city4cfd.subprocess, "run", fake_run)

    result = city4cfd.SubprocessCity4CFDExecutor().execute(request)

    assert result.status == "docker_succeeded"
    assert result.backend == "docker"
    assert city4cfd.DEFAULT_CITY4CFD_DOCKER_IMAGE == "tudelft3d/city4cfd:0.8.0"
    assert city4cfd.DEFAULT_CITY4CFD_DOCKER_IMAGE in captured["argv"]
    assert captured["argv"][0:3] == ("/usr/bin/docker", "run", "--rm")
    assert captured["argv"][-2:] == ("--output_dir", "city4cfd output")


def test_unavailable_adapter_does_not_start_process(tmp_path: Path, monkeypatch) -> None:
    request = _request(tmp_path)
    monkeypatch.setattr(city4cfd.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        city4cfd.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    result = city4cfd.SubprocessCity4CFDExecutor().execute(request)

    assert result.status == "unavailable_handoff"
    assert result.backend is None
    assert result.argv == ()


def test_docker_adapter_rejects_whitespace_environment_image(tmp_path: Path, monkeypatch) -> None:
    request = _request(tmp_path)
    monkeypatch.setenv("CITY4CFD_DOCKER_IMAGE", "   ")
    monkeypatch.setattr(
        city4cfd.shutil,
        "which",
        lambda name: None if name == "city4cfd" else "/usr/bin/docker",
    )

    with pytest.raises(ConfigError, match="city_models.docker_image must be a non-empty string"):
        city4cfd.SubprocessCity4CFDExecutor().execute(request)


def test_docker_adapter_rejects_option_like_environment_image(tmp_path: Path, monkeypatch) -> None:
    request = _request(tmp_path)
    monkeypatch.setenv("CITY4CFD_DOCKER_IMAGE", "--privileged")
    monkeypatch.setattr(
        city4cfd.shutil,
        "which",
        lambda name: None if name == "city4cfd" else "/usr/bin/docker",
    )
    monkeypatch.setattr(
        city4cfd.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    with pytest.raises(ConfigError, match="docker_image must not begin with '-'"):
        city4cfd.SubprocessCity4CFDExecutor().execute(request)


def test_handoff_script_quotes_metacharacters(tmp_path: Path) -> None:
    image = "registry.example/city4cfd:tag; touch /tmp/not-created"
    request = _request(tmp_path, docker_image=image)

    script = city4cfd.render_handoff_script(request)

    assert shlex.quote(image) in script
    assert f"cd {shlex.quote(str(request.working_directory.resolve()))}" in script
    assert shlex.quote(request.output_directory_name) in script
    assert "shell=True" not in script
