from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from cities_reconstruction.adapters.city4cfd import City4CFDExecutionResult
from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    StageStatus,
    publish_stage_manifest,
)
from cities_reconstruction.stages import city_models, point_cloud
from tests.config_helpers import write_complete_config
from tests.stage_manifest_helpers import publish_test_stage_manifest


class FakeExecutor:
    def __init__(self, result: City4CFDExecutionResult, callback=None) -> None:
        self.result = result
        self.callback = callback
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        if self.callback is not None:
            self.callback(request)
        return self.result


def _execution_result(
    status: str = "unavailable_handoff",
    *,
    backend: str | None = None,
    argv: tuple[str, ...] = (),
    return_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
) -> City4CFDExecutionResult:
    return City4CFDExecutionResult(
        status=status,
        backend=backend,
        argv=argv,
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
    )


def test_prepares_city4cfd_lod22_handoff_from_point_cloud_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    stale_mesh = config_path.parent / "outputs" / "03_city_models" / "Mesh_Buildings.obj"
    stale_mesh.parent.mkdir(parents=True, exist_ok=True)
    _write_obj_mesh(stale_mesh, kind="building")
    executor = FakeExecutor(_execution_result())

    result = city_models.run(load_config(config_path), executor=executor)

    assert result.building_count == 3
    assert result.alignment_status == "passed"
    assert result.city4cfd_status == "unavailable_handoff"
    assert not stale_mesh.exists()
    assert not result.building_mesh_path.exists()
    assert result.city4cfd_config_path.exists()
    assert result.footprint_diagnostics_path.exists()
    assert result.run_script_path.exists()
    assert (result.surfaces_directory / "buildings_lod22_preview.stl").exists()
    building_vertices = _stl_vertices(result.surfaces_directory / "buildings_lod22_preview.stl")
    terrain_vertices = _stl_vertices(result.surfaces_directory / "terrain_preview.stl")
    building_min_x = min(vertex[0] for vertex in building_vertices)
    building_max_x = max(vertex[0] for vertex in building_vertices)
    building_min_y = min(vertex[1] for vertex in building_vertices)
    building_max_y = max(vertex[1] for vertex in building_vertices)
    building_zs = {round(vertex[2], 3) for vertex in building_vertices}
    terrain_min_x = min(vertex[0] for vertex in terrain_vertices)
    terrain_max_x = max(vertex[0] for vertex in terrain_vertices)
    terrain_min_y = min(vertex[1] for vertex in terrain_vertices)
    terrain_max_y = max(vertex[1] for vertex in terrain_vertices)
    terrain_zs = {round(vertex[2], 3) for vertex in terrain_vertices}
    assert terrain_min_x <= building_min_x <= building_max_x <= terrain_max_x
    assert terrain_min_y <= building_min_y <= building_max_y <= terrain_max_y
    assert 10.0 in terrain_zs
    assert 10.0 in building_zs
    assert 15.0 in building_zs
    config = json.loads(result.city4cfd_config_path.read_text(encoding="utf-8"))
    assert config["point_clouds"]["ground"].endswith("ground_points.ply")
    assert config["point_clouds"]["buildings"].endswith("building_points.ply")
    assert config["polygons"][0]["type"] == "Building"
    assert config["polygons"][0]["unique_id"] == "osm_id"
    assert config["polygons"][0]["building_base_height_attribute"] == "building_base_height_m"
    surface_layers = config["polygons"][1:]
    assert {layer["type"] for layer in surface_layers} == {"SurfaceLayer"}
    assert {layer["layer_name"] for layer in surface_layers} == {
        "roads",
        "green_areas",
        "concrete",
        "water",
        "other_terrain",
        "gap_fill",
    }
    assert all(layer["path"].startswith("surface_layers/") for layer in surface_layers)
    roads = json.loads((result.output_directory / "surface_layers" / "roads.geojson").read_text(encoding="utf-8"))
    assert roads["crs"]["properties"]["name"] == "EPSG:25832"
    road_coordinate = roads["features"][0]["geometry"]["coordinates"][0][0]
    assert abs(road_coordinate[0] - point_cloud._lonlat_to_epsg25832(11.2558, 43.7696)[0]) < 2.0
    assert abs(road_coordinate[1] - point_cloud._lonlat_to_epsg25832(11.2558, 43.7696)[1]) < 2.0
    assert roads["features"][0]["properties"]["source_crs"] == "EPSG:4326"
    assert roads["features"][0]["properties"]["projected_crs"] == "EPSG:25832"
    assert roads["features"][0]["properties"]["clipped_to_outer_region"] is True
    assert config["top_height"] == 300.0
    assert config["domain_bnd"] == 200.0
    assert config["smooth_terrain"]["iterations"] == 1
    assert config["reconstruction_regions"][0]["influence_region"] == 6.0
    assert config["reconstruction_regions"][0]["lod"] == "2.2"
    assert config["bnd_type_bpg"] == "Rectangle"
    assert "output_dir" not in config
    legacy_manifest_path = result.output_directory / "city4cfd_reconstruction_manifest.json"
    assert result.manifest_path == result.output_directory / "manifest.json"
    assert not legacy_manifest_path.exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["stage"] == "city-models"
    assert manifest["status"] == "completed"
    assert manifest["input_state_fingerprint"]["limitation"].startswith("Lightweight change detector")
    artifacts = {artifact["name"]: artifact for artifact in manifest["artifacts"]}
    assert artifacts["run-script"] == {
        "name": "run-script",
        "path": str(result.run_script_path),
        "kind": "log",
        "required": True,
    }
    run_script = result.run_script_path.read_text(encoding="utf-8")
    assert "if command -v city4cfd" in run_script
    assert "elif command -v docker" in run_script
    assert "docker run --rm" in run_script
    assert "--output_dir city4cfd_output" in run_script
    preview = result.preview_path.read_text(encoding="utf-8")
    assert "<canvas" in preview
    assert "City4CFD generated surfaces" in preview
    assert "generated building mesh" in preview
    assert "bounded preview sample focused on the 3D objects" in preview
    assert "Drag to rotate the generated surface preview" in preview
    assert "mouse wheel or zoom buttons" in preview
    assert "footprintScene" not in preview
    assert "Zoom in" in preview
    assert "Reset zoom" in preview
    assert 'id="meshOverlay"' in preview
    assert 'view.overlayCanvas.getContext("2d")' in preview
    assert 'const matrix = mat4Multiply(projection, modelView)' in preview
    assert 'out[12] = b30 * a00 + b31 * a10 + b32 * a20 + b33 * a30' in preview
    diagnostics = json.loads(result.footprint_diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["inner_ring_count"] == 1
    assert diagnostics["overlap_status"] == "warning"
    assert diagnostics["overlap_pair_count"] == 1
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["details"]["city4cfd_execution"]["status"] == "unavailable_handoff"
    assert manifest["details"]["city4cfd_generated_surfaces"]["buildings"].endswith("Mesh_Buildings.obj")
    assert manifest["details"]["city4cfd_generated_surfaces"]["terrain"].endswith("Mesh_Terrain.obj")
    assert {layer["category"] for layer in manifest["details"]["surface_layers"]} == {
        "roads",
        "green_areas",
        "concrete",
        "water",
        "other_terrain",
        "gap_fill",
    }
    assert all(
        layer["layer_path"].endswith(f"{layer['category']}.geojson")
        for layer in manifest["details"]["surface_layers"]
    )
    assert set(manifest["details"]["city4cfd_generated_surfaces"]["surface_layers"]) == {
        "roads",
        "green_areas",
        "concrete",
        "water",
        "other_terrain",
        "gap_fill",
    }
    report = result.report_path.read_text(encoding="utf-8")
    assert "Stage 1 Surface Layers" in report
    assert "SurfaceLayer polygon imports" in report
    assert "`roads`" in report
    assert result.to_dict() == manifest


def test_city_models_surface_layers_reject_wrong_manifest_artifact_kind(tmp_path: Path) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    config = load_config(config_path)
    stage1_dir = config.output.root_directory / "01_shapefiles"
    manifest = publish_test_stage_manifest(
        stage1_dir,
        stage="shapefiles",
        named_artifacts={
            "summary": (stage1_dir / "summary.json", ArtifactKind.SUPPORTING),
            "category-roads": (stage1_dir / "roads.geojson", ArtifactKind.SUPPORTING),
        },
    )
    surface_layers_dir = tmp_path / "surface-layers"
    surface_layers_dir.mkdir()

    with pytest.raises(ConfigError, match="category-roads") as error:
        city_models._prepare_stage1_surface_layers(config, surface_layers_dir, tmp_path)

    assert str(manifest.manifest_path) in str(error.value)


def test_city_models_rejects_failed_alignment(tmp_path: Path) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="failed")
    stage_dir = config_path.parent / "outputs" / "03_city_models"
    manifest_path = stage_dir / "manifest.json"
    legacy_manifest_path = stage_dir / "city4cfd_reconstruction_manifest.json"
    stage_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text('{"stale":true}', encoding="utf-8")
    legacy_manifest_path.write_text('{"stale":true}', encoding="utf-8")
    stdout_log_path = manifest_path.parent / "city4cfd_stdout.log"
    stderr_log_path = manifest_path.parent / "city4cfd_stderr.log"
    stdout_log_path.write_text("stale stdout", encoding="utf-8")
    stderr_log_path.write_text("stale stderr", encoding="utf-8")

    with pytest.raises(ConfigError, match="alignment failed"):
        city_models.run(load_config(config_path))

    assert not manifest_path.exists()
    assert not legacy_manifest_path.exists()
    assert not stdout_log_path.exists()
    assert not stderr_log_path.exists()


def test_city_models_rejects_wrong_stage_point_cloud_manifest(tmp_path: Path) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    manifest_path = config_path.parent / "outputs" / "02_point_cloud" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["stage"] = "shapefiles"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="expected stage 'point-cloud'"):
        city_models.run(load_config(config_path), executor=FakeExecutor(_execution_result()))


def test_city_models_rejects_failed_point_cloud_manifest(tmp_path: Path) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    manifest_path = config_path.parent / "outputs" / "02_point_cloud" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["status"] = "failed_external_execution"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="not completed"):
        city_models.run(load_config(config_path), executor=FakeExecutor(_execution_result()))


def test_city_models_rejects_malformed_point_cloud_manifest(tmp_path: Path) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    manifest_path = config_path.parent / "outputs" / "02_point_cloud" / "manifest.json"
    manifest_path.write_text("{", encoding="utf-8")

    with pytest.raises(ConfigError, match="cannot read stage manifest"):
        city_models.run(load_config(config_path), executor=FakeExecutor(_execution_result()))


def test_city_models_rejects_point_cloud_manifest_missing_required_handoff(tmp_path: Path) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    manifest_path = config_path.parent / "outputs" / "02_point_cloud" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"] = [
        artifact
        for artifact in payload["artifacts"]
        if artifact["name"] != "projected-building-footprints"
    ]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="does not declare handoff artifact.*projected-building-footprints"):
        city_models.run(load_config(config_path), executor=FakeExecutor(_execution_result()))


def test_failed_city_models_qa_does_not_publish_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    output_dir = config_path.parent / "outputs" / "03_city_models"
    manifest_path = output_dir / "manifest.json"
    legacy_manifest_path = output_dir / "city4cfd_reconstruction_manifest.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text('{"stale":true}', encoding="utf-8")
    legacy_manifest_path.write_text('{"stale":true}', encoding="utf-8")
    executor = FakeExecutor(_execution_result())
    monkeypatch.setattr(
        city_models,
        "_render_preview",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("preview failed")),
    )

    with pytest.raises(RuntimeError, match="preview failed"):
        city_models.run(load_config(config_path), executor=executor)

    assert not manifest_path.exists()
    assert not legacy_manifest_path.exists()
    assert not (output_dir / ".stage.lock").exists()


def test_external_failure_publishes_failed_handoff_and_discards_partial_meshes(
    tmp_path: Path,
) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    output_dir = config_path.parent / "outputs" / "03_city_models"

    def write_partial_mesh(_request) -> None:
        partial_dir = output_dir / "city4cfd_output"
        partial_dir.mkdir(parents=True, exist_ok=True)
        _write_obj_mesh(partial_dir / "Mesh_Buildings.obj", kind="building")

    execution = City4CFDExecutionResult(
        status="external_failed",
        backend="native",
        argv=("/usr/bin/city4cfd", "config.json"),
        return_code=17,
        stdout="partial output\n",
        stderr="reconstruction failed\n",
    )

    result = city_models.run(
        load_config(config_path),
        executor=FakeExecutor(execution, write_partial_mesh),
    )

    assert result.status is StageStatus.FAILED_EXTERNAL_EXECUTION
    assert result.city4cfd_return_code == 17
    assert not result.building_mesh_path.exists()
    assert result.stdout_log_path.read_text(encoding="utf-8") == "partial output\n"
    assert result.stderr_log_path.read_text(encoding="utf-8") == "reconstruction failed\n"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed_external_execution"
    assert manifest["details"]["city4cfd_execution"]["return_code"] == 17
    assert manifest["details"]["city4cfd_execution"]["stdout_truncated"] is False
    assert manifest["details"]["city4cfd_generated_surfaces"]["buildings"].endswith("Mesh_Buildings.obj")
    assert result.to_dict() == manifest
    assert "qa-stl-preview" in result.preview_path.read_text(encoding="utf-8")


def test_city_models_runs_city4cfd_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    calls: list[list[str]] = []
    output_dir = config_path.parent / "outputs" / "03_city_models"

    def fake_run(request) -> None:
        calls.append(["/usr/bin/city4cfd", str(request.config_path), "--output_dir", request.output_directory_name])
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_obj_mesh(output_dir / "Mesh_Buildings.obj", kind="building")
        _write_obj_mesh(output_dir / "Mesh_Terrain.obj", kind="terrain")
    executor = FakeExecutor(
        _execution_result(
            "native_succeeded",
            backend="native",
            argv=("/usr/bin/city4cfd", "config", "--output_dir", "city4cfd_output"),
            return_code=0,
        ),
        fake_run,
    )

    result = city_models.run(load_config(config_path), executor=executor)

    assert result.city4cfd_status == "native_succeeded"
    assert calls and calls[0][0] == "/usr/bin/city4cfd"
    assert calls[0][1].endswith("city4cfd_config.json")
    assert calls[0][-2:] == ["--output_dir", "city4cfd_output"]
    assert result.combined_terrain_mesh_path.exists()
    preview = result.preview_path.read_text(encoding="utf-8")
    assert "City4CFD OBJ triangles" in preview
    assert 'id="meshOverlay"' in preview
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["details"]["city4cfd_execution"]["status"] == "native_succeeded"
    artifacts = {artifact["name"]: artifact for artifact in manifest["artifacts"]}
    assert artifacts["building-mesh"]["required"] is True
    assert artifacts["terrain-mesh"]["required"] is True


@pytest.mark.parametrize("status", ["native_succeeded", "docker_succeeded"])
def test_successful_city4cfd_without_core_geometry_does_not_publish_manifest(
    tmp_path: Path,
    status: str,
) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    manifest_path = config_path.parent / "outputs" / "03_city_models" / "manifest.json"

    with pytest.raises(ConfigError, match="reported success.*required generated geometry"):
        city_models.run(
            load_config(config_path),
            executor=FakeExecutor(
                _execution_result(status, backend=status.removesuffix("_succeeded"), return_code=0)
            ),
        )

    assert not manifest_path.exists()


def test_successful_nonseparate_city4cfd_requires_configured_aggregate_geometry(
    tmp_path: Path,
) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    config = load_config(config_path)
    config = replace(
        config,
        city_models=replace(config.city_models, output_separately=False),
    )
    output_dir = config.output.root_directory / "03_city_models"

    def write_aggregate_mesh(_request) -> None:
        generated_dir = output_dir / "city4cfd_output"
        generated_dir.mkdir(parents=True, exist_ok=True)
        _write_obj_mesh(generated_dir / "Mesh.obj", kind="city")

    result = city_models.run(
        config,
        executor=FakeExecutor(
            _execution_result("native_succeeded", backend="native", return_code=0),
            write_aggregate_mesh,
        ),
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    artifacts = {artifact["name"]: artifact for artifact in manifest["artifacts"]}
    assert artifacts["city-mesh"] == {
        "name": "city-mesh",
        "path": str(output_dir / "city4cfd_output" / "Mesh.obj"),
        "kind": "handoff",
        "required": True,
    }
    assert "building-mesh" not in artifacts
    assert "terrain-mesh" not in artifacts


@pytest.mark.parametrize("second_status", ["native_succeeded", "external_failed"])
def test_switching_from_split_to_aggregate_does_not_publish_stale_split_meshes(
    tmp_path: Path,
    second_status: str,
) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    split_config = load_config(config_path)
    output_dir = split_config.output.root_directory / "03_city_models"
    generated_dir = output_dir / "city4cfd_output"

    def write_split_meshes(_request) -> None:
        generated_dir.mkdir(parents=True, exist_ok=True)
        _write_obj_mesh(generated_dir / "Mesh_Buildings.obj", kind="building")
        _write_obj_mesh(generated_dir / "Mesh_Terrain.obj", kind="terrain")
        _write_semantic_obj_meshes(generated_dir)

    city_models.run(
        split_config,
        executor=FakeExecutor(
            _execution_result("native_succeeded", backend="native", return_code=0),
            write_split_meshes,
        ),
    )
    stale_split_paths = (
        generated_dir / "Mesh_Buildings.obj",
        generated_dir / "Mesh_Terrain.obj",
        generated_dir / "Mesh_roads.obj",
        generated_dir / "Mesh_Terrain_Combined.obj",
    )
    assert all(path.exists() for path in stale_split_paths)

    aggregate_config = replace(
        split_config,
        city_models=replace(split_config.city_models, output_separately=False),
    )

    def write_aggregate_mesh(_request) -> None:
        generated_dir.mkdir(parents=True, exist_ok=True)
        _write_obj_mesh(generated_dir / "Mesh.obj", kind="city")

    callback = write_aggregate_mesh if second_status == "native_succeeded" else None
    result = city_models.run(
        aggregate_config,
        executor=FakeExecutor(
            _execution_result(
                second_status,
                backend="native",
                return_code=0 if second_status == "native_succeeded" else 1,
            ),
            callback,
        ),
    )

    assert all(not path.exists() for path in stale_split_paths)
    artifact_names = {artifact.name for artifact in result.artifacts}
    assert not {
        "building-mesh",
        "terrain-mesh",
        "combined-terrain-mesh",
        "surface-mesh-roads",
    } & artifact_names
    if second_status == "native_succeeded":
        assert "city-mesh" in artifact_names
    else:
        assert "city-mesh" not in artifact_names
    assert "City4CFD OBJ triangles" not in result.preview_path.read_text(encoding="utf-8")


def test_city_models_preview_loads_meshes_from_configured_city4cfd_output_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    output_dir = config_path.parent / "outputs" / "03_city_models"

    def fake_run(request) -> None:
        assert request.working_directory == output_dir
        generated_dir = output_dir / "city4cfd_output"
        generated_dir.mkdir(parents=True, exist_ok=True)
        _write_obj_mesh(generated_dir / "Mesh_Buildings.obj", kind="building")
        _write_obj_mesh(generated_dir / "Mesh_Terrain.obj", kind="terrain")
        _write_semantic_obj_meshes(generated_dir)

    executor = FakeExecutor(
        _execution_result("native_succeeded", backend="native", return_code=0),
        fake_run,
    )

    result = city_models.run(load_config(config_path), executor=executor)

    assert result.building_mesh_path == output_dir / "city4cfd_output" / "Mesh_Buildings.obj"
    assert result.terrain_mesh_path == output_dir / "city4cfd_output" / "Mesh_Terrain.obj"
    assert result.combined_terrain_mesh_path == output_dir / "city4cfd_output" / "Mesh_Terrain_Combined.obj"
    assert all(path.exists() for path in result.surface_mesh_paths.values())
    combined_lines = result.combined_terrain_mesh_path.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("v ") for line in combined_lines) == 21
    assert sum(line.startswith("f ") for line in combined_lines) == 7
    preview = result.preview_path.read_text(encoding="utf-8")
    assert "City4CFD OBJ triangles" in preview
    assert "roads surface layer" in preview
    assert '"totalSurfaceLayerTriangles":6' in preview
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["details"]["city4cfd_generated_surfaces"]["buildings"].endswith(
        "city4cfd_output/Mesh_Buildings.obj"
    )
    assert manifest["details"]["city4cfd_generated_surfaces"]["combined_terrain"].endswith(
        "city4cfd_output/Mesh_Terrain_Combined.obj"
    )
    assert manifest["details"]["city4cfd_generated_surfaces"]["surface_layers"]["roads"].endswith(
        "city4cfd_output/Mesh_roads.obj"
    )
    assert all(layer["mesh_exists"] for layer in manifest["details"]["surface_layers"])


def test_city4cfd_mesh_scene_recenters_elevated_obj_z(tmp_path: Path) -> None:
    building_mesh = tmp_path / "Mesh_Buildings.obj"
    terrain_mesh = tmp_path / "Mesh_Terrain.obj"
    building_mesh.write_text(
        "\n".join(
            [
                "v 10 20 44",
                "v 11 20 47",
                "v 10 21 58",
                "f 1 2 3",
            ]
        ),
        encoding="utf-8",
    )
    terrain_mesh.write_text(
        "\n".join(
            [
                "v 8 18 43",
                "v 9 18 43",
                "v 8 19 43",
                "f 1 2 3",
            ]
        ),
        encoding="utf-8",
    )

    scene = city_models._city4cfd_mesh_scene_data(building_mesh, terrain_mesh)
    z_values = [point[2] for triangle in scene["triangles"] for point in triangle["points"]]

    assert min(z_values) == 0
    assert max(z_values) == 15


def test_city_models_rejects_projected_stage1_surface_coordinates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    roads_path = tmp_path / "outputs" / "01_shapefiles" / "roads.geojson"
    roads = json.loads(roads_path.read_text(encoding="utf-8"))
    roads["features"][0]["geometry"]["coordinates"][0][0] = [681000.0, 4849000.0]
    roads_path.write_text(json.dumps(roads), encoding="utf-8")
    executor = FakeExecutor(_execution_result())

    with pytest.raises(ConfigError, match="must be EPSG:4326 lon/lat"):
        city_models.run(load_config(config_path), executor=executor)


def test_city_models_runs_city4cfd_with_docker_when_binary_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        if name == "city4cfd":
            return None
        if name == "docker":
            return "/usr/bin/docker"
        return None

    output_dir = config_path.parent / "outputs" / "03_city_models"

    def fake_run(request) -> None:
        calls.append(["/usr/bin/docker", "run", request.docker_image or "tudelft3d/city4cfd:0.8.0"])
        generated_dir = output_dir / "city4cfd_output"
        generated_dir.mkdir(parents=True, exist_ok=True)
        _write_obj_mesh(generated_dir / "Mesh_Buildings.obj", kind="building")
        _write_obj_mesh(generated_dir / "Mesh_Terrain.obj", kind="terrain")

    executor = FakeExecutor(
        _execution_result(
            "docker_succeeded",
            backend="docker",
            argv=("/usr/bin/docker", "run", "tudelft3d/city4cfd:0.8.0"),
            return_code=0,
        ),
        fake_run,
    )

    result = city_models.run(load_config(config_path), executor=executor)

    assert result.city4cfd_status == "docker_succeeded"
    assert calls and calls[0][0] == "/usr/bin/docker"
    assert "tudelft3d/city4cfd:0.8.0" in calls[0]
    preview = result.preview_path.read_text(encoding="utf-8")
    assert "City4CFD OBJ triangles" in preview


def test_city_models_uses_toml_overrides_in_config_and_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _prepare_point_cloud_fixture(tmp_path, alignment_status="passed")
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        name="City4CFD Fixture",
        center_lat=43.7696,
        center_lon=11.2558,
        inner_diameter_m=12.0,
        outer_diameter_m=16.0,
        city_models_block="\n".join(
            [
                "[city_models]",
                'lod = "1.3"',
                "domain_bnd = 345.0",
                "building_roof_default_base_height_m = 2.5",
                "top_height = 420.0",
                'bnd_type_bpg = "Round"',
                "bpg_blockage_ratio = true",
                "flow_direction = [2.0, 3.0]",
                "buffer_region = 33.0",
                "reconstruct_boundaries = false",
                "terrain_thinning = 7.5",
                "building_percentile = 95.0",
                "edge_max_len = 8.0",
                'output_file_name = "CustomMesh"',
                'output_format = "stl"',
                "output_separately = false",
                "output_log = false",
                'log_file = "custom.log"',
                'docker_image = "example/city4cfd:custom"',
                "",
                "[city_models.smooth_terrain]",
                "iterations = 4",
                "max_pts = 25000",
                "",
                "[city_models.reconstruction_region]",
                "influence_region_m = 77.0",
                "complexity_factor = 0.9",
                "validate = false",
                "",
                "[city_models.filters]",
                "min_area = 12.5",
                "min_height = 4.5",
            ]
        ),
    )
    executor = FakeExecutor(_execution_result())

    result = city_models.run(load_config(config_path), executor=executor)

    config = json.loads(result.city4cfd_config_path.read_text(encoding="utf-8"))
    assert config["domain_bnd"] == 345.0
    assert config["top_height"] == 420.0
    assert config["bnd_type_bpg"] == "Round"
    assert config["bpg_blockage_ratio"] is True
    assert config["flow_direction"] == [2.0, 3.0]
    assert config["buffer_region"] == 33.0
    assert config["reconstruct_boundaries"] is False
    assert config["terrain_thinning"] == 7.5
    assert config["smooth_terrain"]["iterations"] == 4
    assert config["smooth_terrain"]["max_pts"] == 25000
    assert config["building_percentile"] == 95.0
    assert config["edge_max_len"] == 8.0
    assert config["reconstruction_regions"][0]["influence_region"] == 77.0
    assert config["reconstruction_regions"][0]["lod"] == "1.3"
    assert config["reconstruction_regions"][0]["complexity_factor"] == 0.9
    assert config["reconstruction_regions"][0]["validate"] is False
    assert config["filters"] == {"min_area": 12.5, "min_height": 4.5}
    assert config["output_file_name"] == "CustomMesh"
    assert config["output_format"] == "stl"
    assert config["output_separately"] is False
    assert config["output_log"] is False
    assert config["log_file"] == "custom.log"

    run_script = result.run_script_path.read_text(encoding="utf-8")
    assert "example/city4cfd:custom" in run_script
    assert "docker run --rm" in run_script


def _write_obj_mesh(path: Path, kind: str) -> None:
    if kind == "building":
        content = "\n".join(
            [
                "v 0 0 0",
                "v 1 0 0",
                "v 0 1 0",
                "f 1 2 3",
            ]
        )
    else:
        content = "\n".join(
            [
                "v 0 0 0",
                "v 1 0 0",
                "v 0 1 0",
                "f 1 2 3",
            ]
        )
    path.write_text(content + "\n", encoding="utf-8")


def _write_semantic_obj_meshes(output_dir: Path) -> None:
    for category in ("roads", "green_areas", "concrete", "water", "other_terrain", "gap_fill"):
        _write_obj_mesh(output_dir / f"Mesh_{category}.obj", kind=category)


def _stl_vertices(path: Path) -> list[tuple[float, float, float]]:
    vertices: list[tuple[float, float, float]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) == 4 and parts[0] == "vertex":
            vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
    return vertices


def _prepare_point_cloud_fixture(tmp_path: Path, alignment_status: str) -> Path:
    config_path = tmp_path / "config.toml"
    outputs = tmp_path / "outputs"
    shapefiles_dir = outputs / "01_shapefiles"
    point_dir = outputs / "02_point_cloud"
    shapefiles_dir.mkdir(parents=True)
    point_dir.mkdir(parents=True)
    center_lon = 11.2558
    center_lat = 43.7696
    center_x, center_y = point_cloud._lonlat_to_epsg25832(center_lon, center_lat)
    _write_stage1_surface_fixture(shapefiles_dir, center_lon, center_lat)
    footprint_path = point_dir / "building_footprints_epsg25832.geojson"
    footprint_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [center_x - 3, center_y - 3],
                                    [center_x + 3, center_y - 3],
                                    [center_x + 3, center_y + 3],
                                    [center_x - 3, center_y + 3],
                                    [center_x - 3, center_y - 3],
                                ],
                                [
                                    [center_x - 1, center_y - 1],
                                    [center_x - 1, center_y + 1],
                                    [center_x + 1, center_y + 1],
                                    [center_x + 1, center_y - 1],
                                    [center_x - 1, center_y - 1],
                                ]
                            ],
                        },
                        "properties": {
                            "height_m": 9.0,
                            "roof_shape": "hipped",
                            "projected_crs": "EPSG:25832",
                        },
                    },
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [center_x + 2, center_y - 2],
                                    [center_x + 5, center_y - 2],
                                    [center_x + 5, center_y + 2],
                                    [center_x + 2, center_y + 2],
                                    [center_x + 2, center_y - 2],
                                ]
                            ],
                        },
                        "properties": {
                            "height_m": 6.0,
                            "projected_crs": "EPSG:25832",
                        },
                    },
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [center_x + 10, center_y - 1],
                                    [center_x + 12, center_y - 1],
                                    [center_x + 12, center_y + 1],
                                    [center_x + 10, center_y + 1],
                                    [center_x + 10, center_y - 1],
                                ]
                            ],
                        },
                        "properties": {
                            "height_m": 5.0,
                            "projected_crs": "EPSG:25832",
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    ground_path = point_dir / "ground_points.ply"
    building_path = point_dir / "building_points.ply"
    ground_path.write_text(
        "\n".join(
            [
                "ply",
                "format ascii 1.0",
                "element vertex 4",
                "property double x",
                "property double y",
                "property double z",
                "end_header",
                f"{center_x - 1:.3f} {center_y - 1:.3f} 10.000",
                f"{center_x + 1:.3f} {center_y - 1:.3f} 10.000",
                f"{center_x - 1:.3f} {center_y + 1:.3f} 10.000",
                f"{center_x + 1:.3f} {center_y + 1:.3f} 10.000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    building_path.write_text(
        "\n".join(
            [
                "ply",
                "format ascii 1.0",
                "element vertex 4",
                "property double x",
                "property double y",
                "property double z",
                "end_header",
                f"{center_x - 1:.3f} {center_y - 1:.3f} 15.000",
                f"{center_x + 1:.3f} {center_y - 1:.3f} 15.000",
                f"{center_x - 1:.3f} {center_y + 1:.3f} 15.000",
                f"{center_x + 1:.3f} {center_y + 1:.3f} 15.000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    diagnostics_path = point_dir / "alignment_diagnostics.json"
    diagnostics_path.write_text(
        json.dumps({"alignment_status": alignment_status, "estimated_horizontal_shift_m": 0.0}),
        encoding="utf-8",
    )
    publish_stage_manifest(
        stage="point-cloud",
        status=StageStatus.COMPLETED,
        output_directory=point_dir,
        report_path=point_dir / "point_cloud_report.md",
        preview_path=point_dir / "point_cloud_alignment_preview.html",
        input_state_fingerprint={"fixture": "city-models-tests"},
        artifacts=(
            ArtifactReference(
                "projected-building-footprints",
                footprint_path,
                ArtifactKind.HANDOFF,
            ),
            ArtifactReference("ground-points", ground_path, ArtifactKind.HANDOFF),
            ArtifactReference("building-points", building_path, ArtifactKind.HANDOFF),
            ArtifactReference(
                "alignment-diagnostics",
                diagnostics_path,
                ArtifactKind.DIAGNOSTIC,
            ),
        ),
        metrics={
            "ground_point_count": 4,
            "building_point_count": 4,
            "tree_point_count": 0,
            "unclassified_point_count": 0,
            "alignment_status": alignment_status,
        },
        details={"crs": "EPSG:25832"},
    )
    write_complete_config(
        config_path,
        output_root=outputs,
        name="City4CFD Fixture",
        center_lat=center_lat,
        center_lon=center_lon,
        inner_diameter_m=12.0,
        outer_diameter_m=16.0,
        reconstruction_influence_region_m=6.0,
    )
    return config_path


def _write_stage1_surface_fixture(shapefiles_dir: Path, center_lon: float, center_lat: float) -> None:
    summary = {
        "feature_counts": {
            "by_category": {
                "buildings": 2,
                "roads": 1,
                "green_areas": 1,
                "concrete": 1,
                "water": 1,
                "other_terrain": 1,
                "gap_fill": 1,
                "trees": 1,
            }
        }
    }
    (shapefiles_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    offsets = {
        "roads": (0.0, 0.0),
        "green_areas": (0.000005, 0.0),
        "concrete": (0.000010, 0.0),
        "water": (0.000015, 0.0),
        "other_terrain": (0.000020, 0.0),
        "gap_fill": (0.000025, 0.0),
    }
    for category, (dx, dy) in offsets.items():
        _write_polygon_feature_collection(
            shapefiles_dir / f"{category}.geojson",
            center_lon + dx,
            center_lat + dy,
            category,
        )
    (shapefiles_dir / "buildings.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [center_lon - 0.00001, center_lat - 0.00001],
                                    [center_lon + 0.00001, center_lat - 0.00001],
                                    [center_lon + 0.00001, center_lat + 0.00001],
                                    [center_lon - 0.00001, center_lat + 0.00001],
                                    [center_lon - 0.00001, center_lat - 0.00001],
                                ]
                            ],
                        },
                        "properties": {"category": "buildings", "building_base_height_m": 0.0},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (shapefiles_dir / "trees.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [center_lon, center_lat]},
                        "properties": {"category": "trees"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    publish_test_stage_manifest(
        shapefiles_dir,
        stage="shapefiles",
        named_artifacts={
            "summary": (shapefiles_dir / "summary.json", ArtifactKind.SUPPORTING),
            "category-buildings": (
                shapefiles_dir / "buildings.geojson",
                ArtifactKind.HANDOFF,
            ),
            "category-trees": (shapefiles_dir / "trees.geojson", ArtifactKind.HANDOFF),
            **{
                f"category-{category.replace('_', '-')}": (
                    shapefiles_dir / f"{category}.geojson",
                    ArtifactKind.HANDOFF,
                )
                for category in offsets
            },
        },
    )


def _write_polygon_feature_collection(path: Path, center_x: float, center_y: float, category: str) -> None:
    radius = 0.000004
    square = [
        [center_x - radius, center_y - radius],
        [center_x + radius, center_y - radius],
        [center_x + radius, center_y + radius],
        [center_x - radius, center_y + radius],
        [center_x - radius, center_y - radius],
    ]
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": [square]},
                        "properties": {"category": category, "source_tag": category},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
