from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from cities_reconstruction.cli import _stage_exit_code, main
from cities_reconstruction.config import ConfigError
from cities_reconstruction.pipeline import STAGE_BY_NAME
from cities_reconstruction.stage_contract import ArtifactReference, JsonValue, StageManifest, StageOutput, StageStatus
from cities_reconstruction.stage_runtime import StageRunOptions
from cities_reconstruction.stages import air_purifiers, point_cloud
from tests.config_helpers import write_complete_config

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class FakeStageOutput:
    """Small schema-v2 output used by CLI boundary tests."""

    manifest: StageManifest

    @property
    def stage(self) -> str:
        return self.manifest.stage

    @property
    def status(self) -> StageStatus:
        return self.manifest.status

    @property
    def output_directory(self) -> Path:
        return self.manifest.output_directory

    @property
    def manifest_path(self) -> Path:
        return self.manifest.manifest_path

    @property
    def report_path(self) -> Path:
        return self.manifest.report_path

    @property
    def preview_path(self) -> Path:
        return self.manifest.preview_path

    @property
    def artifacts(self) -> tuple[ArtifactReference, ...]:
        return self.manifest.artifacts

    @property
    def metrics(self) -> dict[str, JsonValue]:
        return self.manifest.metrics

    @property
    def details(self) -> dict[str, JsonValue]:
        return self.manifest.details

    def to_dict(self) -> dict[str, JsonValue]:
        return self.manifest.to_dict()


def _fake_stage_output(
    status: StageStatus = StageStatus.COMPLETED,
    *,
    metrics: dict[str, JsonValue] | None = None,
    stage: str = "fake-stage",
    output_directory: Path = Path("stage-output"),
) -> FakeStageOutput:
    return FakeStageOutput(
        StageManifest(
            schema_version=2,
            application_version="test",
            stage=stage,
            status=status,
            output_directory=output_directory,
            manifest_path=output_directory / "manifest.json",
            report_path=output_directory / "report.md",
            preview_path=output_directory / "preview.html",
            finished_at_utc="2026-08-19T00:00:00+00:00",
            input_state_fingerprint={},
            artifacts=(),
            metrics={} if metrics is None else metrics,
            details={},
        )
    )


def test_validate_config_command(capsys) -> None:
    exit_code = main(["validate-config", "--config", str(ROOT / "config/examples/florence.toml")])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Configuration is valid" in captured.out


def test_dry_run_json_command(capsys) -> None:
    exit_code = main(
        [
            "dry-run",
            "--config",
            str(ROOT / "config/examples/florence.toml"),
            "--stage",
            "openfoam",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"stage": "openfoam"' in captured.out


def test_missing_config_returns_configuration_error(capsys) -> None:
    exit_code = main(["validate-config", "--config", "missing.toml"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Configuration error" in captured.err


def test_stage_exit_code_uses_shared_status() -> None:
    completed_output = _fake_stage_output(StageStatus.COMPLETED)
    failed_output = _fake_stage_output(StageStatus.FAILED_EXTERNAL_EXECUTION)

    assert isinstance(completed_output, StageOutput)
    assert _stage_exit_code(completed_output) == 0
    assert _stage_exit_code(failed_output) == 1


def test_run_stage_json_emits_shared_manifest_mapping(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    result = _fake_stage_output()
    monkeypatch.setattr(
        "cities_reconstruction.stage_runtime.city_models.run",
        lambda _config: result,
    )

    exit_code = main(["run-stage", "--config", str(config_path), "city-models", "--json"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == result.to_dict()


def test_run_stage_dispatches_runner_owned_by_registry(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    result = _fake_stage_output()
    received_options: list[StageRunOptions] = []

    def registry_runner(_config, options):
        received_options.append(options)
        return result

    monkeypatch.setitem(
        STAGE_BY_NAME,
        "trees",
        replace(STAGE_BY_NAME["trees"], runner=registry_runner),
    )

    exit_code = main(
        ["run-stage", "--config", str(config_path), "trees", "--json"]
    )

    assert exit_code == 0
    assert received_options == [StageRunOptions()]
    assert json.loads(capsys.readouterr().out) == result.to_dict()


def test_pipeline_run_executes_default_chain_and_prints_plan_first(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    calls: list[str] = []

    for stage_name in ("shapefiles", "point-cloud", "city-models"):
        output_directory = tmp_path / stage_name
        output_directory.mkdir()
        result = _fake_stage_output(stage=stage_name, output_directory=output_directory)
        result.report_path.write_text(f"report for {stage_name}\n", encoding="utf-8")

        def runner(_config, _options, *, name=stage_name, output=result):
            calls.append(name)
            return output

        monkeypatch.setitem(
            STAGE_BY_NAME,
            stage_name,
            replace(STAGE_BY_NAME[stage_name], runner=runner),
        )

    exit_code = main(["run", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls == ["shapefiles", "point-cloud", "city-models"]
    assert captured.out.splitlines()[0] == "Execution plan: shapefiles -> point-cloud -> city-models"
    assert captured.out.count("report for") == 3


def test_pipeline_run_json_includes_optional_air_purifiers(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    calls: list[str] = []

    for stage_name in ("shapefiles", "point-cloud", "city-models", "air-purifiers"):
        result = _fake_stage_output(stage=stage_name)

        def runner(_config, _options, *, name=stage_name, output=result):
            calls.append(name)
            return output

        monkeypatch.setitem(
            STAGE_BY_NAME,
            stage_name,
            replace(STAGE_BY_NAME[stage_name], runner=runner),
        )

    exit_code = main(
        [
            "run",
            "--config",
            str(config_path),
            "--include",
            "air-purifiers",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert calls == ["shapefiles", "point-cloud", "city-models", "air-purifiers"]
    assert payload["plan"] == calls
    assert [result["stage"] for result in payload["results"]] == calls
    assert captured.err == (
        "Execution plan: shapefiles -> point-cloud -> city-models -> air-purifiers\n"
    )


def test_pipeline_run_target_respects_explicit_footprint_replacement(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    footprints_path = tmp_path / "accepted.geojson"
    calls: list[StageRunOptions] = []
    result = _fake_stage_output(stage="point-cloud")

    def runner(_config, options):
        calls.append(options)
        return result

    monkeypatch.setitem(
        STAGE_BY_NAME,
        "point-cloud",
        replace(STAGE_BY_NAME["point-cloud"], runner=runner),
    )

    exit_code = main(
        [
            "run",
            "--config",
            str(config_path),
            "--target",
            "point-cloud",
            "--building-footprints-geojson",
            str(footprints_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls == [StageRunOptions(building_footprints_geojson=footprints_path)]
    assert json.loads(captured.out)["plan"] == ["point-cloud"]


def test_pipeline_run_stops_after_failure_and_returns_one(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    calls: list[str] = []

    for stage_name, status in (
        ("shapefiles", StageStatus.COMPLETED),
        ("point-cloud", StageStatus.FAILED_EXTERNAL_EXECUTION),
        ("city-models", StageStatus.COMPLETED),
    ):
        result = _fake_stage_output(status, stage=stage_name)

        def runner(_config, _options, *, name=stage_name, output=result):
            calls.append(name)
            return output

        monkeypatch.setitem(
            STAGE_BY_NAME,
            stage_name,
            replace(STAGE_BY_NAME[stage_name], runner=runner),
        )

    exit_code = main(["run", "--config", str(config_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert calls == ["shapefiles", "point-cloud"]
    assert [result["stage"] for result in payload["results"]] == calls


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--target", "shapefiles", "--model-library", "models.json"], "--model-library"),
        (
            ["--target", "shapefiles", "--building-footprints-geojson", "buildings.geojson"],
            "--building-footprints-geojson",
        ),
    ],
)
def test_pipeline_run_rejects_overrides_for_unselected_stages(
    tmp_path: Path,
    capsys,
    arguments: list[str],
    message: str,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")

    exit_code = main(["run", "--config", str(config_path), *arguments])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert message in captured.err


def test_run_stage_translates_stage_config_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")

    def fail_stage(*_args, **_kwargs):
        raise ConfigError("point-cloud fixture failure")

    monkeypatch.setattr(point_cloud, "run", fail_stage)

    exit_code = main(
        ["run-stage", "--config", str(config_path), "point-cloud"]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err == "Configuration error: point-cloud fixture failure\n"


def test_incomplete_config_returns_configuration_error(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "incomplete.toml"
    config_path.write_text(
        """
[region]
name = "Incomplete"
center_lat = 43.7696
center_lon = 11.2558
crs = "EPSG:25832"
inner_diameter_m = 200.0
outer_diameter_m = 400.0

[output]
root_directory = "outputs"
""".strip(),
        encoding="utf-8",
    )

    exit_code = main(["validate-config", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Configuration error" in captured.err
    assert "missing required [inputs] table" in captured.err


def test_config_argument_is_required(capsys) -> None:
    try:
        main(["validate-config"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("validate-config without --config should exit")

    captured = capsys.readouterr()
    assert "--config" in captured.err


def test_run_stage_shapefiles_with_cached_overpass_json(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(config_path, output_root=tmp_path / "outputs", name="CLI Fixture")
    raw_path.write_text('{"elements": []}', encoding="utf-8")
    captured_paths: list[Path | None] = []
    result = _fake_stage_output()

    def fake_run(_config, overpass_json_path=None):
        captured_paths.append(overpass_json_path)
        return result

    monkeypatch.setattr(
        "cities_reconstruction.stage_runtime.shapefiles.run",
        fake_run,
    )

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "shapefiles",
            "--overpass-json",
            str(raw_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured_paths == [raw_path]
    assert json.loads(captured.out) == result.to_dict()


def test_run_stage_keeps_common_and_owned_options_position_independent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    raw_path = tmp_path / "overpass.json"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    raw_path.write_text('{"elements": []}', encoding="utf-8")
    captured_paths: list[Path | None] = []
    result = _fake_stage_output()

    def fake_run(_config, overpass_json_path=None):
        captured_paths.append(overpass_json_path)
        return result

    monkeypatch.setattr(
        "cities_reconstruction.stage_runtime.shapefiles.run",
        fake_run,
    )

    exit_code = main(
        [
            "run-stage",
            "--overpass-json",
            str(raw_path),
            "shapefiles",
            f"--config={config_path}",
            "--json",
        ]
    )

    assert exit_code == 0
    assert captured_paths == [raw_path]
    assert json.loads(capsys.readouterr().out) == result.to_dict()


def test_run_stage_shapefiles_accepts_supplemental_shapefile_overrides(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    streets_path = tmp_path / "streets.shp"
    green_path = tmp_path / "green.shp"
    write_complete_config(config_path, output_root=tmp_path / "outputs", name="CLI Surface Fixture")
    captured_configs: list[object] = []

    def fake_run(config, overpass_json_path=None):
        captured_configs.append(config)
        return _fake_stage_output()

    monkeypatch.setattr(
        "cities_reconstruction.stage_runtime.shapefiles.run",
        fake_run,
    )

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "shapefiles",
            "--streets-shapefile",
            streets_path.name,
            "--streets-shapefile-crs",
            "EPSG:3003",
            "--green-areas-shapefile",
            green_path.name,
            "--green-areas-shapefile-crs",
            "EPSG:4326",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"status": "completed"' in captured.out
    shapefiles_config = captured_configs[0].shapefiles
    surfaces = {surface.name: surface for surface in shapefiles_config.supplemental}
    assert surfaces["streets"].path == streets_path
    assert surfaces["streets"].crs == "EPSG:3003"
    assert surfaces["green_areas"].path == green_path
    assert surfaces["green_areas"].crs == "EPSG:4326"
    assert shapefiles_config.surface_precedence.index("supplemental:green_areas") < shapefiles_config.surface_precedence.index("green_areas")
    assert shapefiles_config.surface_precedence.index("supplemental:streets") < shapefiles_config.surface_precedence.index("roads")


def test_run_stage_visual_enrichment_with_segmentation_geojson(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    stage1_dir = output_root / "01_shapefiles"
    stage1_dir.mkdir(parents=True)
    (stage1_dir / "all_features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}),
        encoding="utf-8",
    )
    (stage1_dir / "imagery_diagnostics.json").write_text(
        json.dumps({"bbox_lon_lat": [11.254, 43.768, 11.258, 43.772], "sources": []}),
        encoding="utf-8",
    )
    segmentation_path = tmp_path / "segmentation.geojson"
    segmentation_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": []}),
        encoding="utf-8",
    )
    sat2lod2_path = tmp_path / "sat2lod2.geojson"
    sat2lod2_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": []}),
        encoding="utf-8",
    )
    write_complete_config(config_path, output_root=output_root, name="CLI Visual Fixture")
    captured_paths: dict[str, Path] = {}
    result = _fake_stage_output(metrics={"candidate_count": 0, "sat2lod2_feature_count": 0})

    def fake_run(config, *, segmentation_geojson_path, sat2lod2_geojson_path):
        captured_paths["segmentation"] = segmentation_geojson_path
        captured_paths["sat2lod2"] = sat2lod2_geojson_path
        return result

    monkeypatch.setattr(
        "cities_reconstruction.stage_runtime.visual_enrichment.run",
        fake_run,
    )

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "visual-enrichment",
            "--segmentation-geojson",
            str(segmentation_path),
            "--sat2lod2-geojson",
            str(sat2lod2_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured_paths == {"segmentation": segmentation_path, "sat2lod2": sat2lod2_path}
    assert json.loads(captured.out) == result.to_dict()


def test_run_stage_point_cloud_accepts_tree_overlay_argument(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    overlay_path = tmp_path / "tree_overlay.png"
    _write_png(overlay_path, width=5, height=5, rgba=(10, 160, 35, 255))
    write_complete_config(
        config_path,
        output_root=output_root,
        name="CLI Point Cloud Fixture",
    )
    captured_configs: list[object] = []
    result = _fake_stage_output(metrics={"tree_point_count": 1})

    def fake_run(config, *, building_footprints_path=None):
        captured_configs.append(config)
        return result

    monkeypatch.setattr(point_cloud, "run", fake_run)

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "point-cloud",
            "--tree-canopy-overlay",
            str(overlay_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured_configs[0].inputs.tree_canopy_overlay_path == overlay_path
    assert json.loads(captured.out) == result.to_dict()


def test_run_stage_point_cloud_resolves_explicit_footprints_from_config_directory(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_dir = tmp_path / "scenario"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    output_root = tmp_path / "outputs"
    override_path = config_dir / "inputs" / "accepted_buildings.geojson"
    override_path.parent.mkdir()
    override_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    write_complete_config(config_path, output_root=output_root, name="CLI Footprint Override Fixture")
    captured_paths: list[Path | None] = []

    def fake_run(config, *, building_footprints_path=None):
        captured_paths.append(building_footprints_path)
        return _fake_stage_output()

    monkeypatch.setattr(point_cloud, "run", fake_run)

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "point-cloud",
            "--building-footprints-geojson",
            "inputs/accepted_buildings.geojson",
            "--json",
        ]
    )

    assert exit_code == 0
    assert captured_paths == [override_path.resolve()]
    assert '"status": "completed"' in capsys.readouterr().out


def test_rejects_building_footprint_override_for_unrelated_stage(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    override_path = tmp_path / "buildings.geojson"
    override_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    write_complete_config(config_path, output_root=tmp_path / "outputs")

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "trees",
            "--building-footprints-geojson",
            str(override_path),
        ]
    )

    assert exit_code == 2
    assert "valid only for the point-cloud stage" in capsys.readouterr().err


def test_run_stage_reports_configuration_error_before_wrong_stage_override(
    tmp_path: Path,
    capsys,
) -> None:
    missing_config = tmp_path / "missing.toml"

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(missing_config),
            "trees",
            "--model-library",
            "models.json",
        ]
    )

    assert exit_code == 2
    error = capsys.readouterr().err
    assert error.startswith("Configuration error:")
    assert "valid only" not in error


def test_run_stage_attached_wrong_stage_override_keeps_error_precedence(
    tmp_path: Path,
    capsys,
) -> None:
    missing_config = tmp_path / "missing.toml"

    exit_code = main(
        [
            "run-stage",
            "trees",
            f"--config={missing_config}",
            "--city-models-top-height=410",
        ]
    )

    assert exit_code == 2
    error = capsys.readouterr().err
    assert error.startswith("Configuration error:")
    assert "unrecognized arguments" not in error


def test_run_stage_trees_json(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs", name="CLI Trees Fixture")
    result = _fake_stage_output(metrics={"tree_count": 1})
    monkeypatch.setattr(
        "cities_reconstruction.stage_runtime.trees.run",
        lambda _config: result,
    )

    exit_code = main(["run-stage", "--config", str(config_path), "trees", "--json"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == result.to_dict()


def test_run_stage_trees_accepts_terrain_geometry_override(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    terrain_path = tmp_path / "terrain.obj"
    write_complete_config(config_path, output_root=output_root, name="CLI Trees Terrain Fixture")
    captured_configs: list[object] = []

    def fake_run(config):
        captured_configs.append(config)
        return _fake_stage_output()

    monkeypatch.setattr("cities_reconstruction.stage_runtime.trees.run", fake_run)

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "trees",
            "--tree-terrain-geometry",
            terrain_path.name,
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"status": "completed"' in captured.out
    assert captured_configs[0].inputs.tree_terrain_geometry_path == terrain_path


def test_cli_air_purifier_path_overrides_are_config_relative(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    captured: dict[str, object] = {}

    def fake_run(config, **kwargs):
        captured.update(kwargs)
        return _fake_stage_output()

    monkeypatch.setattr(air_purifiers, "run", fake_run)

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "air-purifiers",
            "--model-library",
            "assets/parameters.json",
            "--terrain-geometry",
            "outputs/terrain.obj",
            "--json",
        ]
    )

    assert exit_code == 0
    assert captured == {
        "model_library_path": tmp_path / "assets/parameters.json",
        "terrain_geometry_path": tmp_path / "outputs/terrain.obj",
    }
    assert '"status": "completed"' in capsys.readouterr().out


def test_cli_air_purifiers_uses_config_paths_when_overrides_are_absent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        air_purifiers_block="""
[air_purifiers]
model_library_path = "assets/configured.json"
terrain_geometry_path = "outputs/configured.obj"
""",
    )
    captured: dict[str, object] = {}

    def fake_run(config, **kwargs):
        captured["configured_model_library"] = config.air_purifiers.model_library_path
        captured["configured_terrain_geometry"] = config.air_purifiers.terrain_geometry_path
        captured.update(kwargs)
        return _fake_stage_output()

    monkeypatch.setattr(air_purifiers, "run", fake_run)

    exit_code = main(
        ["run-stage", "--config", str(config_path), "air-purifiers", "--json"]
    )

    assert exit_code == 0
    assert captured == {
        "configured_model_library": tmp_path / "assets/configured.json",
        "configured_terrain_geometry": tmp_path / "outputs/configured.obj",
        "model_library_path": None,
        "terrain_geometry_path": None,
    }
    assert '"status": "completed"' in capsys.readouterr().out


@pytest.mark.parametrize(
    "stage",
    ["shapefiles", "visual-enrichment", "point-cloud", "city-models", "trees"],
)
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--model-library", "assets/parameters.json"),
        ("--terrain-geometry", "outputs/terrain.obj"),
    ],
)
def test_cli_rejects_air_purifier_overrides_for_every_other_stage(
    stage: str,
    option: str,
    value: str,
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")

    exit_code = main(
        ["run-stage", "--config", str(config_path), stage, option, value]
    )

    assert exit_code == 2
    assert capsys.readouterr().err == (
        f"{option} is valid only for the air-purifiers stage\n"
    )


def test_cli_air_purifiers_reports_unresolved_model_library(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")

    exit_code = main(["run-stage", "--config", str(config_path), "air-purifiers"])

    assert exit_code == 2
    assert capsys.readouterr().err == (
        "Configuration error: air-purifier model library is unresolved; "
        "configure model_library_path or provide an override\n"
    )


def test_run_stage_help_lists_executable_stages_without_unscoped_overrides(
    capsys,
) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["run-stage", "--help"])

    output = capsys.readouterr().out
    assert "air-purifiers" in output
    assert "STAGE --help" in output
    assert "--model-library" not in output
    assert "--city-models-top-height" not in output


def test_run_stage_air_purifiers_help_lists_only_its_overrides(capsys) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["run-stage", "air-purifiers", "--help"])

    output = capsys.readouterr().out
    assert "--model-library" in output
    assert "--terrain-geometry" in output
    assert "--overpass-json" not in output
    assert "--building-footprints-geojson" not in output
    assert "--city-models-top-height" not in output


def test_run_stage_city_models_accepts_argument_overrides(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.toml"
    output_root = tmp_path / "outputs"
    write_complete_config(config_path, output_root=output_root, name="CLI City Models Fixture")
    captured_configs: list[object] = []

    def fake_run(config):
        captured_configs.append(config)
        return _fake_stage_output()

    monkeypatch.setattr(
        "cities_reconstruction.stage_runtime.city_models.run",
        fake_run,
    )

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "city-models",
            "--city-models-top-height",
            "410",
            "--city-models-flow-direction",
            "4",
            "5",
            "--city-models-reconstruction-influence-region",
            "88",
            "--no-city-models-reconstruction-validate",
            "--city-models-docker-image",
            "example/city4cfd:cli",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"status": "completed"' in captured.out
    config = captured_configs[0]
    assert config.city_models.top_height == 410.0
    assert config.city_models.flow_direction == (4.0, 5.0)
    assert config.city_models.reconstruction_region.influence_region_m == 88.0
    assert config.city_models.reconstruction_region.validate is False
    assert config.city_models.docker_image == "example/city4cfd:cli"


def test_run_stage_city_models_returns_one_after_printing_external_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    result = _fake_stage_output(StageStatus.FAILED_EXTERNAL_EXECUTION)
    monkeypatch.setattr(
        "cities_reconstruction.stage_runtime.city_models.run",
        lambda _config: result,
    )

    exit_code = main(["run-stage", "--config", str(config_path), "city-models", "--json"])

    assert exit_code == 1
    assert '"status": "failed_external_execution"' in capsys.readouterr().out


def test_city_models_toml_and_cli_validation_have_identical_errors_and_do_not_run_stage(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    valid_path = tmp_path / "valid.toml"
    invalid_path = tmp_path / "invalid.toml"
    write_complete_config(valid_path, output_root=tmp_path / "outputs")
    invalid_path.write_text(
        valid_path.read_text(encoding="utf-8").replace(
            "influence_region_m = 150.0",
            "influence_region_m = -1.0",
        ),
        encoding="utf-8",
    )

    toml_exit = main(["validate-config", "--config", str(invalid_path)])
    toml_error = capsys.readouterr().err
    stage_called = False

    def fail_if_called(_config):
        nonlocal stage_called
        stage_called = True
        raise AssertionError("invalid effective config must not run the stage")

    monkeypatch.setattr(
        "cities_reconstruction.stage_runtime.city_models.run",
        fail_if_called,
    )
    cli_exit = main(
        [
            "run-stage",
            "--config",
            str(valid_path),
            "city-models",
            "--city-models-reconstruction-influence-region",
            "-1",
        ]
    )
    cli_error = capsys.readouterr().err

    assert toml_exit == cli_exit == 2
    assert toml_error == cli_error
    assert toml_error == (
        "Configuration error: city_models.reconstruction_region.influence_region_m "
        "must be positive\n"
    )
    assert stage_called is False


def test_city_models_cli_rejects_non_finite_override_before_stage(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    monkeypatch.setattr(
        "cities_reconstruction.stage_runtime.city_models.run",
        lambda _config: (_ for _ in ()).throw(AssertionError("stage must not run")),
    )

    exit_code = main(
        [
            "run-stage",
            "--config",
            str(config_path),
            "city-models",
            "--city-models-top-height",
            "nan",
        ]
    )

    assert exit_code == 2
    assert "city_models.top_height must be finite" in capsys.readouterr().err


def _write_png(path: Path, width: int, height: int, rgba: tuple[int, int, int, int]) -> None:
    row = bytes([0] + list(rgba) * width)
    raw = row * height
    chunks = [
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
        _png_chunk(b"IDAT", zlib.compress(raw)),
        _png_chunk(b"IEND", b""),
    ]
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"".join(chunks))


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)
