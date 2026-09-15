from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from cities_reconstruction.config import AppConfig, ConfigError
from cities_reconstruction.geometry.terrain import validate_completed_city_models_terrain
from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    JsonValue,
    StageStatus,
    publish_stage_manifest,
)

_FLORENCE_ORIGIN = (681557.2487172568, 4848756.385454708)


def _config(tmp_path: Path) -> AppConfig:
    return cast(
        AppConfig,
        SimpleNamespace(
            output=SimpleNamespace(root_directory=tmp_path / "outputs"),
            region=SimpleNamespace(center_lon=11.2558, center_lat=43.7696),
            working_crs="EPSG:25832",
        ),
    )


def _publish_terrain_manifest(config: AppConfig, *, local_origin: JsonValue) -> Path:
    stage_dir = config.output.root_directory / "04_city_models"
    terrain_path = stage_dir / "city4cfd_output" / "Mesh_Terrain_Combined.obj"
    terrain_path.parent.mkdir(parents=True)
    terrain_path.write_text("v 0 0 0\n", encoding="utf-8")
    publish_stage_manifest(
        stage="city-models",
        status=StageStatus.COMPLETED,
        output_directory=stage_dir,
        report_path=stage_dir / "city_models_report.md",
        preview_path=stage_dir / "city_models_preview.html",
        input_state_fingerprint={"fixture": "terrain-frame"},
        artifacts=(ArtifactReference("terrain", terrain_path, ArtifactKind.HANDOFF),),
        metrics={},
        details={"crs": "EPSG:25832", "local_origin": local_origin},
    )
    return terrain_path


def test_accepts_city_models_terrain_from_matching_local_frame(tmp_path: Path) -> None:
    config = _config(tmp_path)
    terrain_path = _publish_terrain_manifest(
        config,
        local_origin={"x": _FLORENCE_ORIGIN[0], "y": _FLORENCE_ORIGIN[1]},
    )

    validate_completed_city_models_terrain(config, terrain_path)


def test_accepts_city_models_terrain_origin_within_one_millimetre(tmp_path: Path) -> None:
    config = _config(tmp_path)
    terrain_path = _publish_terrain_manifest(
        config,
        local_origin={"x": _FLORENCE_ORIGIN[0] + 0.0009, "y": _FLORENCE_ORIGIN[1] - 0.0009},
    )

    validate_completed_city_models_terrain(config, terrain_path)


def test_rejects_city_models_terrain_from_changed_local_origin(tmp_path: Path) -> None:
    config = _config(tmp_path)
    terrain_path = _publish_terrain_manifest(
        config,
        local_origin={"x": _FLORENCE_ORIGIN[0] + 1.0, "y": _FLORENCE_ORIGIN[1]},
    )

    with pytest.raises(ConfigError, match=r"terrain.*local origin.*rerun"):
        validate_completed_city_models_terrain(config, terrain_path)


@pytest.mark.parametrize(
    "local_origin",
    [
        None,
        {},
        {"x": "681557.25", "y": _FLORENCE_ORIGIN[1]},
        {"x": None, "y": _FLORENCE_ORIGIN[1]},
    ],
)
def test_rejects_missing_or_malformed_city_models_local_origin(
    tmp_path: Path,
    local_origin: JsonValue,
) -> None:
    config = _config(tmp_path)
    terrain_path = _publish_terrain_manifest(config, local_origin=local_origin)

    with pytest.raises(ConfigError, match=r"terrain.*local origin.*rerun"):
        validate_completed_city_models_terrain(config, terrain_path)
