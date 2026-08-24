from __future__ import annotations

from pathlib import Path

from cities_reconstruction.adapters.city4cfd import City4CFDExecutionResult
from cities_reconstruction.stage_contract import ArtifactKind, StageStatus
from cities_reconstruction.stages.city_models.publication import (
    CityModelsPublicationInput,
    publish_city_models_manifest,
)


def _artifact(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name, encoding="utf-8")
    return path


def _publication(
    tmp_path: Path,
    *,
    execution: City4CFDExecutionResult,
    status: StageStatus,
) -> CityModelsPublicationInput:
    paths = {
        name: _artifact(tmp_path, name)
        for name in (
            "city4cfd_config.json",
            "footprint_diagnostics.json",
            "run_city4cfd.sh",
            "surfaces/buildings_lod22.stl",
            "surfaces/terrain.stl",
            "city4cfd_output/Mesh_Buildings.obj",
            "city4cfd_output/Mesh_Terrain.obj",
            "city4cfd_output/Mesh_Terrain_Combined.obj",
            "city4cfd_output/Mesh_GreenAreas.obj",
            "city4cfd_output/Mesh_Roads.obj",
            "stdout.log",
            "stderr.log",
            "city_models_report.md",
            "city_models_preview.html",
        )
    }
    return CityModelsPublicationInput(
        status=status,
        output_directory=tmp_path,
        report_path=paths["city_models_report.md"],
        preview_path=paths["city_models_preview.html"],
        input_state_fingerprint={"config": "fixture"},
        city4cfd_config_path=paths["city4cfd_config.json"],
        footprint_diagnostics_path=paths["footprint_diagnostics.json"],
        run_script_path=paths["run_city4cfd.sh"],
        building_preview_path=paths["surfaces/buildings_lod22.stl"],
        terrain_preview_path=paths["surfaces/terrain.stl"],
        building_mesh_path=paths["city4cfd_output/Mesh_Buildings.obj"],
        terrain_mesh_path=paths["city4cfd_output/Mesh_Terrain.obj"],
        combined_terrain_mesh_path=paths["city4cfd_output/Mesh_Terrain_Combined.obj"],
        surface_mesh_paths={
            "roads": paths["city4cfd_output/Mesh_Roads.obj"],
            "green_areas": paths["city4cfd_output/Mesh_GreenAreas.obj"],
        },
        city_mesh_path=None,
        uses_separate_mesh_outputs=True,
        stdout_log_path=paths["stdout.log"],
        stderr_log_path=paths["stderr.log"],
        building_count=2,
        alignment_status="passed",
        footprint_overlap_status="warning",
        region="Florence",
        crs="EPSG:25832",
        point_cloud_manifest_path=tmp_path / "03_point_cloud" / "manifest.json",
        surface_layers=(
            {
                "category": "roads",
                "layer_name": "Roads",
                "source_path": "roads-source.geojson",
                "layer_path": "roads.geojson",
                "config_path": "roads.geojson",
                "feature_count": 3,
            },
            {
                "category": "green_areas",
                "layer_name": "GreenAreas",
                "source_path": "green-source.geojson",
                "layer_path": "green.geojson",
                "config_path": "green.geojson",
                "feature_count": 1,
            },
        ),
        execution=execution,
    )


def test_publish_city_models_manifest_preserves_successful_split_output_contract(tmp_path: Path) -> None:
    publication = _publication(
        tmp_path,
        execution=City4CFDExecutionResult(
            status="native_succeeded",
            backend="native",
            argv=("city4cfd", "config.json"),
            return_code=0,
            stdout="",
            stderr="",
        ),
        status=StageStatus.COMPLETED,
    )

    manifest = publish_city_models_manifest(publication)

    assert manifest.metrics == {
        "building_count": 2,
        "alignment_status": "passed",
        "footprint_overlap_status": "warning",
    }
    assert [(artifact.name, artifact.kind, artifact.required) for artifact in manifest.artifacts] == [
        ("city4cfd-config", ArtifactKind.SUPPORTING, True),
        ("footprint-diagnostics", ArtifactKind.DIAGNOSTIC, True),
        ("run-script", ArtifactKind.LOG, True),
        ("building-preview-surface", ArtifactKind.PREVIEW, True),
        ("terrain-preview-surface", ArtifactKind.PREVIEW, True),
        ("building-mesh", ArtifactKind.HANDOFF, True),
        ("terrain-mesh", ArtifactKind.HANDOFF, True),
        ("combined-terrain-mesh", ArtifactKind.HANDOFF, True),
        ("surface-mesh-green_areas", ArtifactKind.HANDOFF, False),
        ("surface-mesh-roads", ArtifactKind.HANDOFF, False),
        ("stdout-log", ArtifactKind.LOG, True),
        ("stderr-log", ArtifactKind.LOG, True),
        ("report", ArtifactKind.REPORT, True),
        ("preview", ArtifactKind.PREVIEW, True),
    ]
    assert manifest.details["surface_layers"] == [
        {
            "category": "roads",
            "layer_name": "Roads",
            "source_path": "roads-source.geojson",
            "layer_path": "roads.geojson",
            "config_path": "roads.geojson",
            "feature_count": 3,
            "mesh_path": str(tmp_path / "city4cfd_output/Mesh_Roads.obj"),
            "mesh_exists": True,
        },
        {
            "category": "green_areas",
            "layer_name": "GreenAreas",
            "source_path": "green-source.geojson",
            "layer_path": "green.geojson",
            "config_path": "green.geojson",
            "feature_count": 1,
            "mesh_path": str(tmp_path / "city4cfd_output/Mesh_GreenAreas.obj"),
            "mesh_exists": True,
        },
    ]
    assert manifest.details["city4cfd_execution"] == {
        "status": "native_succeeded",
        "backend": "native",
        "argv": ["city4cfd", "config.json"],
        "return_code": 0,
        "stdout_log": str(tmp_path / "stdout.log"),
        "stderr_log": str(tmp_path / "stderr.log"),
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    assert manifest.manifest_path.is_file()


def test_publish_city_models_manifest_omits_failed_external_handoffs(tmp_path: Path) -> None:
    publication = _publication(
        tmp_path,
        execution=City4CFDExecutionResult(
            status="external_failed",
            backend="native",
            argv=("city4cfd", "config.json"),
            return_code=17,
            stdout="partial output",
            stderr="reconstruction failed",
        ),
        status=StageStatus.FAILED_EXTERNAL_EXECUTION,
    )

    manifest = publish_city_models_manifest(publication)

    assert manifest.status is StageStatus.FAILED_EXTERNAL_EXECUTION
    assert not any(artifact.kind is ArtifactKind.HANDOFF for artifact in manifest.artifacts)
    assert manifest.details["city4cfd_execution"]["return_code"] == 17
