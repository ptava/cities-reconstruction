"""City4CFD reconstruction handoff for the third pipeline module."""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cities_reconstruction.adapters.city4cfd import (
    City4CFDExecutionRequest,
    City4CFDExecutionResult,
    City4CFDExecutor,
    SubprocessCity4CFDExecutor,
    render_handoff_script,
)
from cities_reconstruction.artifacts import (
    atomic_text_writer,
    atomic_write_json,
    atomic_write_text,
    lightweight_state_fingerprint,
    stage_output_lock,
)
from cities_reconstruction.config import AppConfig, ConfigError
from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    JsonValue,
    StageManifest,
    StageStatus,
    invalidate_stage_manifests,
    require_completed_manifest,
    require_manifest_artifact,
)
from cities_reconstruction.stage_layout import StageId, stage_output_directory
from cities_reconstruction.stage_result import StageResult

from . import rendering, reporting
from .diagnostics import build_footprint_diagnostics
from .geometry import (
    building_preview_triangles,
    clip_surface_layer_features,
    lonlat_to_epsg25832,
    project_surface_layer_feature,
    terrain_preview_triangles,
    validate_successful_city4cfd_geometry,
)
from .inputs import point_cloud_cell_stats, read_feature_collection, read_json_object
from .publication import CityModelsPublicationInput, publish_city_models_manifest

CITY4CFD_OUTPUT_DIR_NAME = "city4cfd_output"
STAGE1_CATEGORY_EXCLUSIONS = frozenset({"buildings", "trees"})


@dataclass(frozen=True)
class CityModelsStageOutput:
    manifest: StageManifest
    city4cfd_config_path: Path
    footprint_diagnostics_path: Path
    run_script_path: Path
    surfaces_directory: Path
    building_mesh_path: Path
    terrain_mesh_path: Path
    combined_terrain_mesh_path: Path
    surface_mesh_paths: dict[str, Path]
    stdout_log_path: Path
    stderr_log_path: Path
    city4cfd_status: str
    city4cfd_backend: str | None
    city4cfd_return_code: int | None
    building_count: int
    alignment_status: str

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


STAGE_ID = StageId.CITY_MODELS


def plan(config: AppConfig) -> StageResult:
    output = stage_output_directory(config.output.root_directory, STAGE_ID)
    return StageResult(
        stage=STAGE_ID.value,
        summary="Prepare configurable City4CFD LoD2.2 reconstruction inputs, command scripts, and tagged surface handoff files.",
        planned_actions=(
            "Read the ground/building point-cloud manifest and projected building footprints.",
            "Stop with a clear error when footprint/point-cloud alignment diagnostics failed.",
            "Write a City4CFD configuration from the city_models TOML settings with separate ground/building point clouds.",
            "Run City4CFD when available, otherwise fall back to Docker, then write an executable command script and preview the generated terrain/building meshes for graphical QA.",
        ),
        expected_outputs=(output,),
    )


def run(
    config: AppConfig,
    *,
    executor: City4CFDExecutor | None = None,
) -> CityModelsStageOutput:
    """Prepare City4CFD inputs, run City4CFD when available, and write QA surfaces."""

    output_dir = stage_output_directory(config.output.root_directory, STAGE_ID)
    with stage_output_lock(output_dir, STAGE_ID.value):
        invalidate_stage_manifests(
            output_dir,
            legacy_names=("city4cfd_reconstruction_manifest.json",),
        )
        (output_dir / "city4cfd_stdout.log").unlink(missing_ok=True)
        (output_dir / "city4cfd_stderr.log").unlink(missing_ok=True)
        return _run_locked(config, executor or SubprocessCity4CFDExecutor())


def _run_locked(config: AppConfig, executor: City4CFDExecutor) -> CityModelsStageOutput:
    point_manifest_path = (
        stage_output_directory(config.output.root_directory, StageId.POINT_CLOUD)
        / "manifest.json"
    )
    point_manifest = require_completed_manifest(
        point_manifest_path,
        expected_stage=StageId.POINT_CLOUD.value,
    )
    diagnostics_path = require_manifest_artifact(
        point_manifest,
        name="alignment-diagnostics",
        kind=ArtifactKind.DIAGNOSTIC,
    ).path
    diagnostics = read_json_object(diagnostics_path)
    alignment_status = str(diagnostics.get("alignment_status", "unknown"))
    if alignment_status == "failed":
        raise ConfigError(
            "point-cloud/footprint alignment failed; review "
            f"{diagnostics_path} before running City4CFD reconstruction"
        )

    footprint_path = require_manifest_artifact(
        point_manifest,
        name="projected-building-footprints",
        kind=ArtifactKind.HANDOFF,
    ).path
    ground_point_cloud_path = require_manifest_artifact(
        point_manifest,
        name="ground-points",
        kind=ArtifactKind.HANDOFF,
    ).path
    building_point_cloud_path = require_manifest_artifact(
        point_manifest,
        name="building-points",
        kind=ArtifactKind.HANDOFF,
    ).path
    city4cfd_inputs = {
        "building_footprints": str(footprint_path),
        "ground_point_cloud": str(ground_point_cloud_path),
        "building_point_cloud": str(building_point_cloud_path),
        "crs": point_manifest.details.get("crs", config.region.crs),
    }
    footprints = read_feature_collection(footprint_path)
    ground_elevation_index = point_cloud_cell_stats(ground_point_cloud_path, prefer="min")
    building_roof_index = point_cloud_cell_stats(building_point_cloud_path, prefer="max")
    output_dir = stage_output_directory(config.output.root_directory, STAGE_ID)
    surfaces_dir = output_dir / "surfaces"
    surface_layers_dir = output_dir / "surface_layers"
    output_dir.mkdir(parents=True, exist_ok=True)
    surfaces_dir.mkdir(parents=True, exist_ok=True)
    surface_layers_dir.mkdir(parents=True, exist_ok=True)

    city4cfd_config_path = output_dir / "city4cfd_config.json"
    manifest_path = output_dir / "manifest.json"
    footprint_diagnostics_path = output_dir / "footprint_diagnostics.json"
    run_script_path = output_dir / "run_city4cfd.sh"
    preview_path = output_dir / "city_models_preview.html"
    report_path = output_dir / "city_models_report.md"
    stdout_log_path = output_dir / "city4cfd_stdout.log"
    stderr_log_path = output_dir / "city4cfd_stderr.log"
    building_stl_path = surfaces_dir / "buildings_lod22_preview.stl"
    terrain_stl_path = surfaces_dir / "terrain_preview.stl"
    stage1_surface_layers = _prepare_stage1_surface_layers(config, surface_layers_dir, output_dir)
    output_name = config.city_models.output_file_name
    output_format = config.city_models.output_format
    expected_mesh_filenames = _expected_city4cfd_mesh_filenames(
        output_name,
        output_format,
        config.city_models.output_separately,
        stage1_surface_layers,
    )
    _remove_known_city4cfd_outputs(output_dir, expected_mesh_filenames)

    city4cfd_config = _build_city4cfd_config(config, city4cfd_inputs, output_dir, stage1_surface_layers)
    atomic_write_json(city4cfd_config_path, city4cfd_config)
    city4cfd_output_dir = _city4cfd_output_dir(output_dir)
    city4cfd_output_dir.mkdir(parents=True, exist_ok=True)
    execution_request = City4CFDExecutionRequest(
        config_path=city4cfd_config_path,
        working_directory=output_dir,
        output_directory_name=CITY4CFD_OUTPUT_DIR_NAME,
        docker_image=config.city_models.docker_image,
    )
    atomic_write_text(
        run_script_path,
        render_handoff_script(execution_request),
        mode=0o755,
    )
    execution = executor.execute(execution_request)
    atomic_write_text(stdout_log_path, execution.stdout)
    atomic_write_text(stderr_log_path, execution.stderr)
    if execution.status == "external_failed":
        # A failed process may have left valid-looking partial meshes.  QA for
        # this outcome must use only deterministic local preview geometry.
        _remove_known_city4cfd_outputs(output_dir, expected_mesh_filenames)

    footprint_diagnostics = build_footprint_diagnostics(footprints)
    atomic_write_json(footprint_diagnostics_path, footprint_diagnostics)
    building_triangles = _write_building_preview_stl(
        building_stl_path,
        footprints,
        ground_elevation_index,
        building_roof_index,
    )
    terrain_triangles = _write_terrain_preview_stl(
        terrain_stl_path,
        config,
        footprints,
        ground_elevation_index,
    )
    uses_separate_mesh_outputs = _uses_separate_city4cfd_mesh_outputs(
        output_format,
        config.city_models.output_separately,
    )
    building_mesh_path = (
        _find_city4cfd_mesh_path(output_dir, f"{output_name}_Buildings.{output_format}")
        if uses_separate_mesh_outputs
        else city4cfd_output_dir / f"{output_name}_Buildings.{output_format}"
    )
    terrain_mesh_path = (
        _find_city4cfd_mesh_path(output_dir, f"{output_name}_Terrain.{output_format}")
        if uses_separate_mesh_outputs
        else city4cfd_output_dir / f"{output_name}_Terrain.{output_format}"
    )
    city_mesh_path = (
        None
        if uses_separate_mesh_outputs
        else _find_city4cfd_mesh_path(
            output_dir,
            _aggregate_city4cfd_mesh_filename(output_name, output_format),
        )
    )
    surface_mesh_paths = (
        {
            layer["category"]: _find_city4cfd_mesh_path(
                output_dir,
                f"{output_name}_{layer['layer_name']}.{output_format}",
            )
            for layer in stage1_surface_layers
        }
        if uses_separate_mesh_outputs
        else {}
    )
    required_core_mesh_paths = (
        (building_mesh_path, terrain_mesh_path)
        if uses_separate_mesh_outputs
        else (city_mesh_path,)
    )
    validate_successful_city4cfd_geometry(execution, required_core_mesh_paths)
    combined_terrain_mesh_path = city4cfd_output_dir / f"{output_name}_Terrain_Combined.obj"
    if uses_separate_mesh_outputs:
        _write_combined_terrain_obj(
            combined_terrain_mesh_path,
            terrain_mesh_path,
            surface_mesh_paths,
        )
        generated_mesh_scene = rendering.city4cfd_mesh_scene_data(
            building_mesh_path,
            terrain_mesh_path,
            surface_mesh_paths,
        )
    else:
        combined_terrain_mesh_path.unlink(missing_ok=True)
        generated_mesh_scene = rendering.stl_scene_data([])
    if not generated_mesh_scene.get("triangles"):
        generated_mesh_scene = rendering.stl_scene_data([*building_triangles, *terrain_triangles])
    fingerprint = _city_models_input_fingerprint(
        config,
        point_manifest_path,
        diagnostics_path,
        footprint_path,
        ground_point_cloud_path,
        building_point_cloud_path,
        stage1_surface_layers,
        execution,
    )
    atomic_write_text(
        preview_path,
        rendering.render_preview(
            config=config,
            features=footprints,
            diagnostics=diagnostics,
            footprint_diagnostics=footprint_diagnostics,
            surface_scene=generated_mesh_scene,
            stage1_surface_layers=stage1_surface_layers,
        ),
    )
    manifest_status = _manifest_status(execution)
    atomic_write_text(
        report_path,
        reporting.render_report(
            config=config,
            city4cfd_config_path=city4cfd_config_path,
            manifest_path=manifest_path,
            footprint_diagnostics_path=footprint_diagnostics_path,
            run_script_path=run_script_path,
            footprint_path=footprint_path,
            building_stl_path=building_stl_path,
            terrain_stl_path=terrain_stl_path,
            building_mesh_path=building_mesh_path,
            terrain_mesh_path=terrain_mesh_path,
            combined_terrain_mesh_path=combined_terrain_mesh_path,
            surface_mesh_paths=surface_mesh_paths,
            stage1_surface_layers=stage1_surface_layers,
            preview_path=preview_path,
            diagnostics=diagnostics,
            footprint_diagnostics=footprint_diagnostics,
            building_count=len(footprints),
            execution=execution,
            stdout_log_path=stdout_log_path,
            stderr_log_path=stderr_log_path,
            contract_status=manifest_status.value,
        ),
    )
    manifest = publish_city_models_manifest(
        CityModelsPublicationInput(
            status=manifest_status,
            output_directory=output_dir,
            report_path=report_path,
            preview_path=preview_path,
            input_state_fingerprint=fingerprint,
            city4cfd_config_path=city4cfd_config_path,
            footprint_diagnostics_path=footprint_diagnostics_path,
            run_script_path=run_script_path,
            building_preview_path=building_stl_path,
            terrain_preview_path=terrain_stl_path,
            building_mesh_path=building_mesh_path,
            terrain_mesh_path=terrain_mesh_path,
            combined_terrain_mesh_path=combined_terrain_mesh_path,
            surface_mesh_paths=surface_mesh_paths,
            city_mesh_path=city_mesh_path,
            uses_separate_mesh_outputs=uses_separate_mesh_outputs,
            stdout_log_path=stdout_log_path,
            stderr_log_path=stderr_log_path,
            building_count=len(footprints),
            alignment_status=alignment_status,
            footprint_overlap_status=str(footprint_diagnostics["overlap_status"]),
            region=config.region.name,
            crs=config.region.crs,
            point_cloud_manifest_path=point_manifest_path,
            surface_layers=stage1_surface_layers,
            execution=execution,
        )
    )

    return CityModelsStageOutput(
        manifest=manifest,
        city4cfd_config_path=city4cfd_config_path,
        footprint_diagnostics_path=footprint_diagnostics_path,
        run_script_path=run_script_path,
        surfaces_directory=surfaces_dir,
        building_mesh_path=building_mesh_path,
        terrain_mesh_path=terrain_mesh_path,
        combined_terrain_mesh_path=combined_terrain_mesh_path,
        surface_mesh_paths=surface_mesh_paths,
        stdout_log_path=stdout_log_path,
        stderr_log_path=stderr_log_path,
        city4cfd_status=execution.status,
        city4cfd_backend=execution.backend,
        city4cfd_return_code=execution.return_code,
        building_count=len(footprints),
        alignment_status=alignment_status,
    )


def _build_city4cfd_config(
    config: AppConfig,
    city4cfd_inputs: dict[str, Any],
    output_dir: Path,
    stage1_surface_layers: list[dict[str, Any]],
) -> dict[str, Any]:
    center_x, center_y = lonlat_to_epsg25832(config.region.center_lon, config.region.center_lat)
    city_models = config.city_models
    return {
        "name": config.region.name,
        "crs": config.region.crs,
        "point_clouds": {
            "ground": _relative_to_workdir(Path(str(city4cfd_inputs["ground_point_cloud"])), output_dir),
            "buildings": _relative_to_workdir(Path(str(city4cfd_inputs["building_point_cloud"])), output_dir),
        },
        "polygons": [
            {
                "type": "Building",
                "path": _relative_to_workdir(Path(str(city4cfd_inputs["building_footprints"])), output_dir),
                "unique_id": "osm_id",
                "building_base_height_attribute": "building_base_height_m",
            },
            *[
                {
                    "type": "SurfaceLayer",
                    "path": layer["config_path"],
                    "layer_name": layer["layer_name"],
                }
                for layer in stage1_surface_layers
            ],
        ],
        "point_of_interest": [round(center_x, 3), round(center_y, 3)],
        "domain_bnd": city_models.domain_bnd,
        "top_height": city_models.top_height,
        "bnd_type_bpg": city_models.bnd_type_bpg,
        "bpg_blockage_ratio": city_models.bpg_blockage_ratio,
        "flow_direction": list(city_models.flow_direction),
        "buffer_region": city_models.buffer_region,
        "reconstruct_boundaries": city_models.reconstruct_boundaries,
        "terrain_thinning": city_models.terrain_thinning,
        "smooth_terrain": {
            "iterations": city_models.smooth_terrain.iterations,
            "max_pts": city_models.smooth_terrain.max_pts,
        },
        "building_percentile": city_models.building_percentile,
        "edge_max_len": city_models.edge_max_len,
        "reconstruction_regions": [
            {
                "influence_region": city_models.reconstruction_region.influence_region_m,
                "lod": city_models.lod,
                "complexity_factor": city_models.reconstruction_region.complexity_factor,
                "validate": city_models.reconstruction_region.validate,
            }
        ],
        "filters": {"min_area": city_models.filters.min_area, "min_height": city_models.filters.min_height},
        "output_file_name": city_models.output_file_name,
        "output_format": city_models.output_format,
        "output_separately": city_models.output_separately,
        "output_log": city_models.output_log,
        "log_file": city_models.log_file,
        "assumptions": [
            "Footprints and point clouds are generated in the same projected CRS before this configuration is used.",
            "Building footprints carry building_base_height_m: ordinary buildings use zero, while building=roof features use an explicit min_height or the configured fallback.",
            "LoD2.2 roof planes are reconstructed by City4CFD/roofer from the building point cloud and roofprint polygons.",
            "The generated preview surfaces are QA fallbacks; when City4CFD writes mesh files, the preview loads those meshes directly.",
        ],
    }


def _prepare_stage1_surface_layers(
    config: AppConfig,
    surface_layers_dir: Path,
    output_dir: Path,
) -> list[dict[str, Any]]:
    stage1_dir = stage_output_directory(config.output.root_directory, StageId.SHAPEFILES)
    stage1_manifest = require_completed_manifest(
        stage1_dir / "manifest.json",
        expected_stage=StageId.SHAPEFILES.value,
    )
    summary_path = require_manifest_artifact(
        stage1_manifest,
        name="summary",
        kind=ArtifactKind.SUPPORTING,
    ).path
    summary = read_json_object(summary_path)
    feature_counts = summary.get("feature_counts")
    if not isinstance(feature_counts, dict):
        raise ConfigError(f"invalid stage-1 summary: {summary_path}")
    by_category = feature_counts.get("by_category")
    if not isinstance(by_category, dict):
        raise ConfigError(f"invalid stage-1 summary: {summary_path}")

    surface_layers: list[dict[str, Any]] = []
    for category in by_category:
        if category in STAGE1_CATEGORY_EXCLUSIONS:
            continue
        category_path = require_manifest_artifact(
            stage1_manifest,
            name=f"category-{category.replace('_', '-')}",
            kind=ArtifactKind.HANDOFF,
        ).path
        source_features = read_feature_collection(category_path)
        if not source_features:
            continue
        projected_features = [
            _project_surface_layer_feature(feature, config, category_path)
            for feature in source_features
        ]
        features = _clip_surface_layer_features(projected_features, config)
        if not features:
            raise ConfigError(
                f"projected surface layer `{category}` does not overlap the configured region; "
                f"review coordinates in {category_path}"
            )
        layer_path = surface_layers_dir / f"{category}.geojson"
        _write_geojson(layer_path, features, config.region.crs)
        surface_layers.append(
            {
                "category": category,
                "layer_name": category,
                "source_path": str(category_path),
                "layer_path": str(layer_path),
                "config_path": _relative_to_workdir(layer_path, output_dir),
                "feature_count": len(features),
            }
        )
    return surface_layers


def _project_surface_layer_feature(
    feature: dict[str, Any],
    config: AppConfig,
    source_path: Path,
) -> dict[str, Any]:
    """Project a stage-1 EPSG:4326 polygon into the City4CFD metric CRS."""

    if config.region.crs != "EPSG:25832":
        raise ConfigError("city-model surface-layer projection currently supports EPSG:25832")
    return project_surface_layer_feature(
        feature,
        target_crs=config.region.crs,
        source_path=source_path,
    )


def _clip_surface_layer_features(
    features: list[dict[str, Any]],
    config: AppConfig,
) -> list[dict[str, Any]]:
    center_xy = lonlat_to_epsg25832(config.region.center_lon, config.region.center_lat)
    return clip_surface_layer_features(
        features,
        center_xy=center_xy,
        radius_m=config.region.outer_diameter_m / 2.0,
    )


def _find_city4cfd_mesh_path(execution_dir: Path, filename: str) -> Path:
    configured_output_dir = _city4cfd_output_dir(execution_dir)
    candidates = [
        configured_output_dir / filename,
        execution_dir / filename,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _remove_known_city4cfd_outputs(output_dir: Path, filenames: list[str]) -> None:
    output_root = output_dir.resolve()
    parents = (_city4cfd_output_dir(output_dir), output_dir)
    for parent in parents:
        resolved_parent = parent.resolve()
        try:
            resolved_parent.relative_to(output_root)
        except ValueError as exc:
            raise ConfigError(f"refusing City4CFD cleanup outside stage output: {parent}") from exc
        for filename in filenames:
            if Path(filename).name != filename:
                raise ConfigError(f"invalid City4CFD output filename: {filename}")
            candidate = parent / filename
            try:
                candidate.lstat()
            except FileNotFoundError:
                continue
            if candidate.is_dir() and not candidate.is_symlink():
                raise ConfigError(f"expected City4CFD output file is a directory: {candidate}")
            candidate.unlink()


def _expected_city4cfd_mesh_filenames(
    output_name: str,
    output_format: str,
    output_separately: bool,
    stage1_surface_layers: list[dict[str, Any]],
) -> list[str]:
    del output_separately
    return list(dict.fromkeys([
        _aggregate_city4cfd_mesh_filename(output_name, output_format),
        f"{output_name}_Buildings.{output_format}",
        f"{output_name}_Terrain.{output_format}",
        *(f"{output_name}_{layer['layer_name']}.{output_format}" for layer in stage1_surface_layers),
        f"{output_name}_Terrain_Combined.obj",
    ]))


def _uses_separate_city4cfd_mesh_outputs(output_format: str, output_separately: bool) -> bool:
    return output_separately and output_format != "cityjson"


def _aggregate_city4cfd_mesh_filename(output_name: str, output_format: str) -> str:
    extension = "city.json" if output_format == "cityjson" else output_format
    return f"{output_name}.{extension}"


def _city4cfd_output_dir(execution_dir: Path) -> Path:
    return execution_dir / CITY4CFD_OUTPUT_DIR_NAME


def _write_geojson(path: Path, features: list[dict[str, Any]], crs: str) -> None:
    atomic_write_text(
        path,
        json.dumps(
            {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": crs}},
                "features": features,
            },
            indent=2,
            sort_keys=True,
        ),
    )


def _manifest_status(execution: City4CFDExecutionResult) -> StageStatus:
    if execution.status == "external_failed":
        return StageStatus.FAILED_EXTERNAL_EXECUTION
    return StageStatus.COMPLETED


def _city_models_input_fingerprint(
    config: AppConfig,
    point_manifest_path: Path,
    diagnostics_path: Path,
    footprint_path: Path,
    ground_point_cloud_path: Path,
    building_point_cloud_path: Path,
    stage1_surface_layers: list[dict[str, Any]],
    execution: City4CFDExecutionResult,
) -> dict[str, Any]:
    paths = [
        config.path,
        point_manifest_path,
        diagnostics_path,
        footprint_path,
        ground_point_cloud_path,
        building_point_cloud_path,
        *(Path(str(layer["source_path"])) for layer in stage1_surface_layers),
    ]
    return lightweight_state_fingerprint(
        {
            "stage": "city-models",
            "crs": config.region.crs,
            "city_models": asdict(config.city_models),
            "adapter_selection": {
                "status": execution.status,
                "backend": execution.backend,
                "argv": list(execution.argv),
                "return_code": execution.return_code,
            },
        },
        paths,
    )


def _relative_to_workdir(path: Path, workdir: Path) -> str:
    return os.path.relpath(path, start=workdir)


def _write_building_preview_stl(
    path: Path,
    features: list[dict[str, Any]],
    ground_elevation_index: dict[tuple[int, int], float],
    building_roof_index: dict[tuple[int, int], float],
) -> list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]]:
    triangles = building_preview_triangles(
        features,
        ground_elevation_index,
        building_roof_index,
    )
    _write_stl(path, "buildings_lod22_preview", triangles)
    return triangles


def _write_terrain_preview_stl(
    path: Path,
    config: AppConfig,
    features: list[dict[str, Any]],
    ground_elevation_index: dict[tuple[int, int], float],
) -> list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]]:
    triangles = terrain_preview_triangles(
        region_bbox=_region_bbox_projected(config),
        features=features,
        ground_elevation_index=ground_elevation_index,
    )
    _write_stl(path, "terrain_preview", triangles)
    return triangles


def _region_bbox_projected(config: AppConfig) -> tuple[float, float, float, float]:
    if config.region.crs != "EPSG:25832":
        raise ConfigError("city-model preview terrain currently supports EPSG:25832 projected output")
    center_x, center_y = lonlat_to_epsg25832(config.region.center_lon, config.region.center_lat)
    radius = config.region.outer_diameter_m / 2.0
    return center_x - radius, center_y - radius, center_x + radius, center_y + radius


def _write_stl(
    path: Path,
    name: str,
    triangles: list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]],
) -> None:
    lines = [f"solid {name}"]
    for _label, a, b, c in triangles:
        normal = _normal(a, b, c)
        lines.extend(
            [
                f"  facet normal {normal[0]:.6g} {normal[1]:.6g} {normal[2]:.6g}",
                "    outer loop",
                f"      vertex {a[0]:.3f} {a[1]:.3f} {a[2]:.3f}",
                f"      vertex {b[0]:.3f} {b[1]:.3f} {b[2]:.3f}",
                f"      vertex {c[0]:.3f} {c[1]:.3f} {c[2]:.3f}",
                "    endloop",
                "  endfacet",
            ]
        )
    lines.append(f"endsolid {name}")
    atomic_write_text(path, "\n".join(lines) + "\n")


def _normal(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> tuple[float, float, float]:
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length == 0.0:
        return 0.0, 0.0, 0.0
    return nx / length, ny / length, nz / length


def _write_combined_terrain_obj(
    output_path: Path,
    terrain_mesh_path: Path,
    surface_mesh_paths: dict[str, Path],
) -> None:
    source_paths = [
        ("terrain", terrain_mesh_path),
        *((category, path) for category, path in sorted(surface_mesh_paths.items())),
    ]
    existing_sources = [
        (label, path)
        for label, path in source_paths
        if path.exists() and path.suffix.lower() == ".obj"
    ]
    if not existing_sources:
        output_path.unlink(missing_ok=True)
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    vertex_offset = 0
    with atomic_text_writer(output_path) as combined:
        combined.write("# Combined City4CFD terrain and semantic surface-layer geometry\n")
        for label, source_path in existing_sources:
            vertices: list[str] = []
            faces: list[list[int]] = []
            with source_path.open("r", encoding="utf-8", errors="replace") as source:
                for raw_line in source:
                    line = raw_line.strip()
                    if line.startswith("v "):
                        vertices.append(line)
                    elif line.startswith("f "):
                        face: list[int] = []
                        for part in line.split()[1:]:
                            index_text = part.split("/")[0]
                            if not index_text:
                                continue
                            index = int(index_text)
                            if index < 0:
                                index = len(vertices) + index + 1
                            face.append(vertex_offset + index)
                        if len(face) >= 3:
                            faces.append(face)
            combined.write(f"g {label}\n")
            for vertex in vertices:
                combined.write(f"{vertex}\n")
            for face in faces:
                combined.write("f " + " ".join(str(index) for index in face) + "\n")
            vertex_offset += len(vertices)
