"""City4CFD reconstruction handoff for the third pipeline module."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any

from shapely.geometry import MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import triangulate
from shapely.validation import make_valid

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
from .inputs import point_cloud_cell_stats, read_feature_collection, read_json_object
from .publication import CityModelsPublicationInput, publish_city_models_manifest

DEFAULT_BUILDING_HEIGHT_M = 9.0
DEFAULT_ROOF_RAISE_M = 1.5
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
    _validate_successful_city4cfd_geometry(execution, required_core_mesh_paths)
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
    center_x, center_y = _lonlat_to_epsg25832(config.region.center_lon, config.region.center_lat)
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
    projected = dict(feature)
    geometry = dict(feature["geometry"])

    def project_ring(ring: list[list[float]]) -> list[list[float]]:
        projected_ring: list[list[float]] = []
        for coordinate in ring:
            lon = float(coordinate[0])
            lat = float(coordinate[1])
            if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                raise ConfigError(
                    f"stage-1 surface coordinates must be EPSG:4326 lon/lat before projection: {source_path}"
                )
            x, y = _lonlat_to_epsg25832(lon, lat)
            projected_ring.append([x, y])
        return projected_ring

    if geometry["type"] == "Polygon":
        geometry["coordinates"] = [project_ring(ring) for ring in geometry["coordinates"]]
    else:
        geometry["coordinates"] = [
            [project_ring(ring) for ring in polygon]
            for polygon in geometry["coordinates"]
        ]
    properties = dict(feature.get("properties", {}))
    properties["source_crs"] = "EPSG:4326"
    properties["projected_crs"] = config.region.crs
    projected["geometry"] = geometry
    projected["properties"] = properties
    return projected


def _clip_surface_layer_features(
    features: list[dict[str, Any]],
    config: AppConfig,
) -> list[dict[str, Any]]:
    center_x, center_y = _lonlat_to_epsg25832(config.region.center_lon, config.region.center_lat)
    region = Point(center_x, center_y).buffer(config.region.outer_diameter_m / 2.0, quad_segs=48)
    clipped_features: list[dict[str, Any]] = []
    for feature in features:
        clipped_geometry = make_valid(shape(feature["geometry"])).intersection(region)
        polygons: list[Polygon] = []
        if isinstance(clipped_geometry, Polygon):
            polygons = [clipped_geometry]
        elif isinstance(clipped_geometry, MultiPolygon):
            polygons = list(clipped_geometry.geoms)
        else:
            polygons = [
                geometry
                for geometry in getattr(clipped_geometry, "geoms", ())
                if isinstance(geometry, Polygon)
            ]
        polygons = [polygon for polygon in polygons if not polygon.is_empty and polygon.area > 0.0]
        if not polygons:
            continue
        clipped = dict(feature)
        clipped["geometry"] = mapping(polygons[0] if len(polygons) == 1 else MultiPolygon(polygons))
        properties = dict(feature.get("properties", {}))
        properties["clipped_to_outer_region"] = True
        clipped["properties"] = properties
        clipped_features.append(clipped)
    return clipped_features


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


def _validate_successful_city4cfd_geometry(
    execution: City4CFDExecutionResult,
    required_paths: tuple[Path | None, ...],
) -> None:
    if not execution.succeeded:
        return
    missing = [
        path
        for path in required_paths
        if path is None or not path.is_file() or path.stat().st_size == 0
    ]
    if missing:
        rendered_paths = ", ".join("<unresolved>" if path is None else str(path) for path in missing)
        raise ConfigError(
            f"City4CFD {execution.status} reported success but required generated geometry "
            f"is missing or empty: {rendered_paths}"
        )


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


def _feature_polygons(feature: dict[str, Any]) -> list[Polygon]:
    geometry = make_valid(shape(feature["geometry"]))
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    return []


def _polygon_rings(polygon: Polygon) -> list[list[tuple[float, float]]]:
    rings = [_ring_xy(list(polygon.exterior.coords))]
    rings.extend(_ring_xy(list(interior.coords)) for interior in polygon.interiors)
    return rings


def _polygon_top_triangles(polygon: Polygon, z: float) -> list[tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]]:
    triangles = []
    for triangle in triangulate(polygon):
        clipped = triangle.intersection(polygon)
        for piece in _geometry_polygons(clipped):
            if piece.area <= 0.001 or not polygon.covers(piece.representative_point()):
                continue
            coords = list(piece.exterior.coords)
            if len(coords) < 4:
                continue
            anchor = (float(coords[0][0]), float(coords[0][1]), z)
            for index in range(1, len(coords) - 2):
                triangles.append(
                    (
                        anchor,
                        (float(coords[index][0]), float(coords[index][1]), z),
                        (float(coords[index + 1][0]), float(coords[index + 1][1]), z),
                    )
                )
    return triangles


def _geometry_polygons(geometry: Any) -> list[Polygon]:
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    return []


def _write_building_preview_stl(
    path: Path,
    features: list[dict[str, Any]],
    ground_elevation_index: dict[tuple[int, int], float],
    building_roof_index: dict[tuple[int, int], float],
) -> list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]]:
    triangles: list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]] = []
    for index, feature in enumerate(features, start=1):
        for polygon in _feature_polygons(feature):
            if polygon.is_empty:
                continue
            base_z, top_z = _feature_preview_elevation(
                polygon,
                feature,
                ground_elevation_index,
                building_roof_index,
            )
            roof_raise = DEFAULT_ROOF_RAISE_M if _has_lod22_roof_shape(feature) else 0.0
            centroid = (float(polygon.centroid.x), float(polygon.centroid.y))
            roof_peak = (centroid[0], centroid[1], top_z + roof_raise)
            for ring in _polygon_rings(polygon):
                base_points = [(x, y, base_z) for x, y in ring[:-1]]
                top_points = [(x, y, top_z) for x, y in ring[:-1]]
                for start, end in zip(
                    range(len(base_points)),
                    range(1, len(base_points) + 1),
                    strict=True,
                ):
                    next_index = end % len(base_points)
                    triangles.extend(
                        _quad_triangles(
                            f"building_{index}_wall",
                            base_points[start],
                            base_points[next_index],
                            top_points[next_index],
                            top_points[start],
                        )
                    )
            for triangle in _polygon_top_triangles(polygon, top_z):
                triangles.append((f"building_{index}_roof", *triangle))
            if roof_raise > 0.0:
                for ring in _polygon_rings(polygon):
                    top_points = [(x, y, top_z) for x, y in ring[:-1]]
                    for start, end in zip(
                        range(len(top_points)),
                        range(1, len(top_points) + 1),
                        strict=True,
                    ):
                        triangles.append((f"building_{index}_lod22_roof", top_points[start], top_points[end % len(top_points)], roof_peak))
    _write_stl(path, "buildings_lod22_preview", triangles)
    return triangles


def _write_terrain_preview_stl(
    path: Path,
    config: AppConfig,
    features: list[dict[str, Any]],
    ground_elevation_index: dict[tuple[int, int], float],
) -> list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]]:
    min_x, min_y, max_x, max_y = _terrain_surface_bbox_projected(config, features)
    terrain_z = _terrain_preview_elevation(ground_elevation_index)
    triangles = [
        ("terrain", (min_x, min_y, terrain_z), (max_x, min_y, terrain_z), (max_x, max_y, terrain_z)),
        ("terrain", (min_x, min_y, terrain_z), (max_x, max_y, terrain_z), (min_x, max_y, terrain_z)),
    ]
    _write_stl(path, "terrain_preview", triangles)
    return triangles


def _terrain_surface_bbox_projected(config: AppConfig, features: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    region_min_x, region_min_y, region_max_x, region_max_y = _region_bbox_projected(config)
    if not features:
        return region_min_x, region_min_y, region_max_x, region_max_y
    footprint_min_x, footprint_min_y, footprint_max_x, footprint_max_y = _features_bbox(features)
    return (
        min(region_min_x, footprint_min_x),
        min(region_min_y, footprint_min_y),
        max(region_max_x, footprint_max_x),
        max(region_max_y, footprint_max_y),
    )


def _feature_preview_elevation(
    polygon: Polygon,
    feature: dict[str, Any],
    ground_elevation_index: dict[tuple[int, int], float],
    building_roof_index: dict[tuple[int, int], float],
) -> tuple[float, float]:
    min_x, min_y, max_x, max_y = polygon.bounds
    cell_size = 2.0
    min_cell_x = math.floor(min_x / cell_size) - 1
    max_cell_x = math.floor(max_x / cell_size) + 1
    min_cell_y = math.floor(min_y / cell_size) - 1
    max_cell_y = math.floor(max_y / cell_size) + 1
    ground_samples: list[float] = []
    roof_samples: list[float] = []
    for cell_x in range(min_cell_x, max_cell_x + 1):
        for cell_y in range(min_cell_y, max_cell_y + 1):
            center_x = (cell_x + 0.5) * cell_size
            center_y = (cell_y + 0.5) * cell_size
            if not polygon.covers(Point(center_x, center_y)):
                continue
            ground_z = ground_elevation_index.get((cell_x, cell_y))
            if ground_z is not None:
                ground_samples.append(ground_z)
            roof_z = building_roof_index.get((cell_x, cell_y))
            if roof_z is not None:
                roof_samples.append(roof_z)
    base_z = median(ground_samples) if ground_samples else 0.0
    if roof_samples:
        top_z = max(roof_samples)
        if top_z <= base_z:
            top_z = base_z + _feature_height(feature)
    else:
        top_z = base_z + _feature_height(feature)
    return base_z, top_z


def _terrain_preview_elevation(ground_elevation_index: dict[tuple[int, int], float]) -> float:
    if not ground_elevation_index:
        return 0.0
    return float(median(ground_elevation_index.values()))


def _region_bbox_projected(config: AppConfig) -> tuple[float, float, float, float]:
    if config.region.crs != "EPSG:25832":
        raise ConfigError("city-model preview terrain currently supports EPSG:25832 projected output")
    center_x, center_y = _lonlat_to_epsg25832(config.region.center_lon, config.region.center_lat)
    radius = config.region.outer_diameter_m / 2.0
    return center_x - radius, center_y - radius, center_x + radius, center_y + radius


def _lonlat_to_epsg25832(lon: float, lat: float) -> tuple[float, float]:
    semi_major = 6378137.0
    flattening = 1 / 298.257223563
    eccentricity_sq = flattening * (2 - flattening)
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    lon0 = math.radians(9.0)
    k0 = 0.9996
    false_easting = 500000.0

    n = semi_major / math.sqrt(1 - eccentricity_sq * math.sin(lat_rad) ** 2)
    t = math.tan(lat_rad) ** 2
    c = (eccentricity_sq / (1 - eccentricity_sq)) * math.cos(lat_rad) ** 2
    a = (lon_rad - lon0) * math.cos(lat_rad)
    m = semi_major * (
        (1 - eccentricity_sq / 4 - 3 * eccentricity_sq**2 / 64 - 5 * eccentricity_sq**3 / 256) * lat_rad
        - (3 * eccentricity_sq / 8 + 3 * eccentricity_sq**2 / 32 + 45 * eccentricity_sq**3 / 1024)
        * math.sin(2 * lat_rad)
        + (15 * eccentricity_sq**2 / 256 + 45 * eccentricity_sq**3 / 1024) * math.sin(4 * lat_rad)
        - (35 * eccentricity_sq**3 / 3072) * math.sin(6 * lat_rad)
    )
    easting = false_easting + k0 * n * (
        a + (1 - t + c) * a**3 / 6 + (5 - 18 * t + t**2 + 72 * c - 58 * eccentricity_sq) * a**5 / 120
    )
    northing = k0 * (
        m
        + n
        * math.tan(lat_rad)
        * (
            a**2 / 2
            + (5 - t + 9 * c + 4 * c**2) * a**4 / 24
            + (61 - 58 * t + t**2 + 600 * c - 330 * eccentricity_sq) * a**6 / 720
        )
    )
    return easting, northing


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


def _outer_rings(geometry: dict[str, Any]) -> list[list[tuple[float, float]]]:
    if geometry["type"] == "Polygon":
        return [_ring_xy(geometry["coordinates"][0])]
    return [_ring_xy(polygon[0]) for polygon in geometry["coordinates"] if polygon]


def _ring_xy(ring: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
    return [(float(point[0]), float(point[1])) for point in ring]


def _feature_height(feature: dict[str, Any]) -> float:
    properties = feature.get("properties", {})
    for key in ("height_m", "estimated_height_m"):
        value = properties.get(key)
        if isinstance(value, int | float) and value > 0:
            return float(value)
    tags = properties.get("tags", {})
    if isinstance(tags, dict):
        height = _float_tag(tags.get("height") or tags.get("building:height"))
        if height is not None:
            return height
        levels = _float_tag(tags.get("building:levels"))
        if levels is not None:
            return levels * 3.0
    return DEFAULT_BUILDING_HEIGHT_M


def _has_lod22_roof_shape(feature: dict[str, Any]) -> bool:
    properties = feature.get("properties", {})
    roof_shape = properties.get("roof_shape")
    if isinstance(roof_shape, str) and roof_shape:
        return True
    tags = properties.get("tags", {})
    return isinstance(tags, dict) and isinstance(tags.get("roof:shape"), str)


def _float_tag(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = value.lower().replace("m", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _quad_triangles(
    label: str,
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
    d: tuple[float, float, float],
) -> list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]]:
    return [(label, a, b, c), (label, a, c, d)]


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


def _features_bbox(features: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    points = [
        point
        for feature in features
        for ring in _outer_rings(feature["geometry"])
        for point in ring
    ]
    if not points:
        return 0.0, 0.0, 1.0, 1.0
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_y = max(point[1] for point in points)
    if min_x == max_x:
        max_x += 1.0
    if min_y == max_y:
        max_y += 1.0
    padding = max(max_x - min_x, max_y - min_y) * 0.05
    return min_x - padding, min_y - padding, max_x + padding, max_y + padding


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
