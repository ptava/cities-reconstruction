"""City4CFD reconstruction handoff for the third pipeline module."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from html import escape
import os
import json
import math
from statistics import median
from pathlib import Path
from typing import Any

from shapely.geometry import MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import triangulate
from shapely.validation import make_valid

from cities_reconstruction.artifacts import (
    atomic_text_writer,
    atomic_write_json,
    atomic_write_text,
    lightweight_state_fingerprint,
    manifest_provenance,
    stage_output_lock,
)
from cities_reconstruction.adapters.city4cfd import (
    City4CFDExecutionRequest,
    City4CFDExecutionResult,
    City4CFDExecutor,
    SubprocessCity4CFDExecutor,
    render_handoff_script,
)
from cities_reconstruction.config import AppConfig, ConfigError
from cities_reconstruction.stage_result import StageResult


DEFAULT_BUILDING_HEIGHT_M = 9.0
DEFAULT_ROOF_RAISE_M = 1.5
CITY4CFD_OUTPUT_DIR_NAME = "city4cfd_output"
MAX_CITY4CFD_BUILDING_PREVIEW_TRIANGLES = 20000
MAX_CITY4CFD_TERRAIN_PREVIEW_TRIANGLES = 8000
MAX_CITY4CFD_SURFACE_LAYER_PREVIEW_TRIANGLES = 4000
STAGE1_CATEGORY_EXCLUSIONS = frozenset({"buildings", "trees"})
SURFACE_LAYER_PREVIEW_COLORS = (
    (0.91, 0.47, 0.13),
    (0.52, 0.31, 0.76),
    (0.04, 0.58, 0.53),
    (0.86, 0.27, 0.45),
    (0.46, 0.57, 0.13),
    (0.02, 0.52, 0.78),
)


@dataclass(frozen=True)
class CityModelsStageOutput:
    output_directory: Path
    city4cfd_config_path: Path
    manifest_path: Path
    footprint_diagnostics_path: Path
    run_script_path: Path
    surfaces_directory: Path
    building_mesh_path: Path
    terrain_mesh_path: Path
    combined_terrain_mesh_path: Path
    surface_mesh_paths: dict[str, Path]
    preview_path: Path
    report_path: Path
    stdout_log_path: Path
    stderr_log_path: Path
    stage_status: str
    city4cfd_status: str
    city4cfd_backend: str | None
    city4cfd_return_code: int | None
    building_count: int
    alignment_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "output_directory": str(self.output_directory),
            "city4cfd_config_path": str(self.city4cfd_config_path),
            "manifest_path": str(self.manifest_path),
            "footprint_diagnostics_path": str(self.footprint_diagnostics_path),
            "run_script_path": str(self.run_script_path),
            "surfaces_directory": str(self.surfaces_directory),
            "building_mesh_path": str(self.building_mesh_path),
            "terrain_mesh_path": str(self.terrain_mesh_path),
            "combined_terrain_mesh_path": str(self.combined_terrain_mesh_path),
            "surface_mesh_paths": {
                category: str(path) for category, path in self.surface_mesh_paths.items()
            },
            "preview_path": str(self.preview_path),
            "report_path": str(self.report_path),
            "stdout_log_path": str(self.stdout_log_path),
            "stderr_log_path": str(self.stderr_log_path),
            "stage_status": self.stage_status,
            "city4cfd_status": self.city4cfd_status,
            "city4cfd_backend": self.city4cfd_backend,
            "city4cfd_return_code": self.city4cfd_return_code,
            "building_count": self.building_count,
            "alignment_status": self.alignment_status,
        }


def plan(config: AppConfig) -> StageResult:
    output = config.output.root_directory / "03_city_models"
    return StageResult(
        stage="city-models",
        summary="Prepare configurable City4CFD LoD2.2 reconstruction inputs, command scripts, and tagged surface handoff files.",
        planned_actions=(
            "Read module-2 ground/building point-cloud manifest and projected building footprints.",
            "Stop with a clear error when footprint/point-cloud alignment diagnostics failed.",
            "Write a City4CFD configuration from the stage-03 TOML settings with separate ground/building point clouds.",
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

    output_dir = config.output.root_directory / "03_city_models"
    with stage_output_lock(output_dir, "city-models"):
        (output_dir / "city4cfd_reconstruction_manifest.json").unlink(missing_ok=True)
        (output_dir / "city4cfd_stdout.log").unlink(missing_ok=True)
        (output_dir / "city4cfd_stderr.log").unlink(missing_ok=True)
        return _run_locked(config, executor or SubprocessCity4CFDExecutor())


def _run_locked(config: AppConfig, executor: City4CFDExecutor) -> CityModelsStageOutput:

    point_manifest_path = config.output.root_directory / "02_point_cloud" / "city4cfd_point_cloud_manifest.json"
    if not point_manifest_path.exists():
        raise ConfigError("missing point-cloud manifest. Run `point-cloud` before `city-models`.")
    point_manifest = _read_json(point_manifest_path)
    diagnostics_path = Path(str(point_manifest["alignment_diagnostics"]))
    diagnostics = _read_json(diagnostics_path)
    alignment_status = str(diagnostics.get("alignment_status", "unknown"))
    if alignment_status == "failed":
        raise ConfigError(
            "point-cloud/footprint alignment failed; review "
            f"{diagnostics_path} before running City4CFD reconstruction"
        )

    city4cfd_inputs = point_manifest.get("city4cfd_inputs")
    if not isinstance(city4cfd_inputs, dict):
        raise ConfigError(f"invalid point-cloud manifest: {point_manifest_path}")
    footprint_path = Path(str(city4cfd_inputs["building_footprints"]))
    ground_point_cloud_path = Path(str(city4cfd_inputs["ground_point_cloud"]))
    building_point_cloud_path = Path(str(city4cfd_inputs["building_point_cloud"]))
    footprints = _read_feature_collection(footprint_path)
    ground_elevation_index = _point_cloud_cell_stats(ground_point_cloud_path, prefer="min")
    building_roof_index = _point_cloud_cell_stats(building_point_cloud_path, prefer="max")
    output_dir = config.output.root_directory / "03_city_models"
    surfaces_dir = output_dir / "surfaces"
    surface_layers_dir = output_dir / "surface_layers"
    output_dir.mkdir(parents=True, exist_ok=True)
    surfaces_dir.mkdir(parents=True, exist_ok=True)
    surface_layers_dir.mkdir(parents=True, exist_ok=True)

    city4cfd_config_path = output_dir / "city4cfd_config.json"
    manifest_path = output_dir / "city4cfd_reconstruction_manifest.json"
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

    footprint_diagnostics = _build_footprint_diagnostics(footprints)
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
    building_mesh_path = _find_city4cfd_mesh_path(output_dir, f"{output_name}_Buildings.{output_format}")
    terrain_mesh_path = _find_city4cfd_mesh_path(output_dir, f"{output_name}_Terrain.{output_format}")
    surface_mesh_paths = {
        layer["category"]: _find_city4cfd_mesh_path(
            output_dir,
            f"{output_name}_{layer['layer_name']}.{output_format}",
        )
        for layer in stage1_surface_layers
    }
    combined_terrain_mesh_path = city4cfd_output_dir / f"{output_name}_Terrain_Combined.obj"
    _write_combined_terrain_obj(
        combined_terrain_mesh_path,
        terrain_mesh_path,
        surface_mesh_paths,
    )
    generated_mesh_scene = _city4cfd_mesh_scene_data(
        building_mesh_path,
        terrain_mesh_path,
        surface_mesh_paths,
    )
    if not generated_mesh_scene.get("triangles"):
        generated_mesh_scene = _stl_scene_data([*building_triangles, *terrain_triangles])
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
    manifest = _build_manifest(
        config=config,
        point_manifest_path=point_manifest_path,
        diagnostics_path=diagnostics_path,
        city4cfd_config_path=city4cfd_config_path,
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
        diagnostics=diagnostics,
        footprint_diagnostics=footprint_diagnostics,
        building_count=len(footprints),
        execution=execution,
        stdout_log_path=stdout_log_path,
        stderr_log_path=stderr_log_path,
        fingerprint=fingerprint,
    )
    atomic_write_text(
        preview_path,
        _render_preview(
            config=config,
            features=footprints,
            diagnostics=diagnostics,
            footprint_diagnostics=footprint_diagnostics,
            surface_scene=generated_mesh_scene,
            stage1_surface_layers=stage1_surface_layers,
        ),
    )
    atomic_write_text(
        report_path,
        _render_report(
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
        ),
    )
    atomic_write_json(manifest_path, manifest)

    return CityModelsStageOutput(
        output_directory=output_dir,
        city4cfd_config_path=city4cfd_config_path,
        manifest_path=manifest_path,
        footprint_diagnostics_path=footprint_diagnostics_path,
        run_script_path=run_script_path,
        surfaces_directory=surfaces_dir,
        building_mesh_path=building_mesh_path,
        terrain_mesh_path=terrain_mesh_path,
        combined_terrain_mesh_path=combined_terrain_mesh_path,
        surface_mesh_paths=surface_mesh_paths,
        preview_path=preview_path,
        report_path=report_path,
        stdout_log_path=stdout_log_path,
        stderr_log_path=stderr_log_path,
        stage_status=_stage_status(execution),
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
    stage1_dir = config.output.root_directory / "01_shapefiles"
    summary_path = stage1_dir / "summary.json"
    if not summary_path.exists():
        raise ConfigError("missing stage-1 summary. Run `shapefiles` before `city-models`.")
    summary = _read_json(summary_path)
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
        category_path = stage1_dir / f"{category}.geojson"
        if not category_path.exists():
            raise ConfigError(f"missing stage-1 category output: {category_path}")
        source_features = _read_feature_collection(category_path)
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
    stage1_surface_layers: list[dict[str, Any]],
) -> list[str]:
    return [
        f"{output_name}_Buildings.{output_format}",
        f"{output_name}_Terrain.{output_format}",
        *(f"{output_name}_{layer['layer_name']}.{output_format}" for layer in stage1_surface_layers),
        f"{output_name}_Terrain_Combined.obj",
    ]


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


def _build_manifest(
    config: AppConfig,
    point_manifest_path: Path,
    diagnostics_path: Path,
    city4cfd_config_path: Path,
    footprint_diagnostics_path: Path,
    run_script_path: Path,
    footprint_path: Path,
    building_stl_path: Path,
    terrain_stl_path: Path,
    building_mesh_path: Path,
    terrain_mesh_path: Path,
    combined_terrain_mesh_path: Path,
    surface_mesh_paths: dict[str, Path],
    stage1_surface_layers: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    footprint_diagnostics: dict[str, Any],
    building_count: int,
    execution: City4CFDExecutionResult,
    stdout_log_path: Path,
    stderr_log_path: Path,
    fingerprint: dict[str, Any],
) -> dict[str, Any]:
    return {
        "stage": "city-models",
        **manifest_provenance(fingerprint),
        "region": config.region.name,
        "crs": config.region.crs,
        "city4cfd_config": str(city4cfd_config_path),
        "run_script": str(run_script_path),
        "point_cloud_manifest": str(point_manifest_path),
        "alignment_diagnostics": str(diagnostics_path),
        "footprint_diagnostics": str(footprint_diagnostics_path),
        "alignment_status": diagnostics.get("alignment_status", "unknown"),
        "footprint_overlap_status": footprint_diagnostics["overlap_status"],
        "building_footprints": str(footprint_path),
        "building_count": building_count,
        "offline_preview_surfaces": {
            "buildings": str(building_stl_path),
            "terrain": str(terrain_stl_path),
        },
        "surface_layers": [
            {
                "category": layer["category"],
                "layer_name": layer["layer_name"],
                "source_path": layer["source_path"],
                "layer_path": layer["layer_path"],
                "config_path": layer["config_path"],
                "feature_count": layer["feature_count"],
                "mesh_path": str(surface_mesh_paths[layer["category"]]),
                "mesh_exists": surface_mesh_paths[layer["category"]].exists(),
            }
            for layer in stage1_surface_layers
        ],
        "city4cfd_generated_surfaces": {
            "buildings": str(building_mesh_path),
            "terrain": str(terrain_mesh_path),
            "combined_terrain": str(combined_terrain_mesh_path),
            "surface_layers": {
                category: str(path) for category, path in surface_mesh_paths.items()
            },
        },
        "required_external_tool": "City4CFD with OpenFOAM-compatible dependencies",
        "stage_status": _stage_status(execution),
        "city4cfd_execution": {
            "status": execution.status,
            "backend": execution.backend,
            "argv": list(execution.argv),
            "return_code": execution.return_code,
            "stdout_log": str(stdout_log_path),
            "stderr_log": str(stderr_log_path),
            "stdout_truncated": execution.stdout_truncated,
            "stderr_truncated": execution.stderr_truncated,
        },
    }


def _stage_status(execution: City4CFDExecutionResult) -> str:
    return "failed_external_execution" if execution.status == "external_failed" else "completed"


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


def _read_feature_collection(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError(f"GeoJSON feature collection missing features list: {path}")
    return [
        feature for feature in features
        if isinstance(feature, dict) and feature.get("geometry", {}).get("type") in {"Polygon", "MultiPolygon"}
    ]


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def _build_footprint_diagnostics(features: list[dict[str, Any]]) -> dict[str, Any]:
    polygon_items: list[tuple[int, dict[str, Any], Polygon]] = []
    hole_count = 0
    invalid_count = 0
    for index, feature in enumerate(features):
        polygons = _feature_polygons(feature)
        if not polygons:
            invalid_count += 1
            continue
        for polygon in polygons:
            hole_count += len(polygon.interiors)
            polygon_items.append((index, feature, polygon))

    overlaps: list[dict[str, Any]] = []
    for first_index, first_feature, first_polygon in polygon_items:
        first_bounds = first_polygon.bounds
        for second_index, second_feature, second_polygon in polygon_items:
            if second_index <= first_index:
                continue
            if not _bounds_intersect(first_bounds, second_polygon.bounds):
                continue
            intersection = first_polygon.intersection(second_polygon)
            if intersection.is_empty:
                continue
            area = float(intersection.area)
            if area <= 0.05:
                continue
            smaller_area = max(min(first_polygon.area, second_polygon.area), 1e-9)
            overlaps.append(
                {
                    "first_feature_index": first_index,
                    "second_feature_index": second_index,
                    "first_source_tag": first_feature.get("properties", {}).get("source_tag"),
                    "second_source_tag": second_feature.get("properties", {}).get("source_tag"),
                    "intersection_area_m2": round(area, 3),
                    "overlap_ratio_of_smaller": round(area / smaller_area, 4),
                }
            )
    overlaps.sort(key=lambda item: item["intersection_area_m2"], reverse=True)
    status = "warning" if overlaps else "passed"
    return {
        "overlap_status": status,
        "feature_count": len(features),
        "polygon_count": len(polygon_items),
        "invalid_or_empty_geometry_count": invalid_count,
        "inner_ring_count": hole_count,
        "overlap_pair_count": len(overlaps),
        "largest_overlap_area_m2": overlaps[0]["intersection_area_m2"] if overlaps else 0.0,
        "overlaps": overlaps[:100],
        "assumptions": [
            "City4CFD receives the projected GeoJSON footprint file with full polygon coordinates, including inner rings.",
            "Overlaps are reported as QA diagnostics because overlapping footprints can create duplicated or superposed reconstructed building surfaces.",
            "The offline STL preview preserves polygon holes, but it is a QA fallback rather than City4CFD output.",
        ],
    }


def _bounds_intersect(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (first[2] < second[0] or first[0] > second[2] or first[3] < second[1] or first[1] > second[3])


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
                for start, end in zip(range(len(base_points)), range(1, len(base_points) + 1)):
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
                    for start, end in zip(range(len(top_points)), range(1, len(top_points) + 1)):
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


def _point_cloud_cell_stats(path: Path, prefer: str) -> dict[tuple[int, int], float]:
    if prefer not in {"min", "max"}:
        raise ValueError("prefer must be either 'min' or 'max'")
    stats: dict[tuple[int, int], float] = {}
    in_data = False
    cell_size = 2.0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if in_data:
                parts = line.split()
                if len(parts) < 3:
                    continue
                x = float(parts[0])
                y = float(parts[1])
                z = float(parts[2])
                key = (math.floor(x / cell_size), math.floor(y / cell_size))
                current = stats.get(key)
                if current is None:
                    stats[key] = z
                elif prefer == "min":
                    stats[key] = min(current, z)
                else:
                    stats[key] = max(current, z)
            elif line.strip() == "end_header":
                in_data = True
    return stats


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


def _ring_xy(ring: list[list[float]]) -> list[tuple[float, float]]:
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


def _render_preview(
    config: AppConfig,
    features: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    footprint_diagnostics: dict[str, Any],
    surface_scene: dict[str, Any],
    stage1_surface_layers: list[dict[str, Any]],
) -> str:
    stl_scene = surface_scene
    stl_scene_json = json.dumps(stl_scene, separators=(",", ":"))
    surface_legend = _render_surface_layer_legend(stage1_surface_layers)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{escape(config.region.name)} City4CFD surfaces</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; color: #1f2933; background: #f8fafc; }}
    .canvas-stack {{ position: relative; width: min(1080px, 100%); height: min(68vh, 720px); border: 1px solid #c8d1dc; background: #ffffff; margin-bottom: 1.2rem; }}
    .canvas-stack canvas {{ position: absolute; inset: 0; display: block; width: 100%; height: 100%; }}
    #meshOverlay {{ pointer-events: none; background: transparent; }}
    .note {{ max-width: 1080px; color: #52606d; line-height: 1.35; }}
    .legend {{ display: flex; gap: 1rem; flex-wrap: wrap; margin: 0.8rem 0; color: #334155; }}
    .swatch {{ display: inline-block; width: 0.9rem; height: 0.9rem; margin-right: 0.35rem; vertical-align: -0.12rem; }}
    .zoom-controls {{ display: flex; gap: 0.45rem; flex-wrap: wrap; margin: 0.7rem 0; }}
    .zoom-controls button {{ border: 1px solid #94a3b8; background: #ffffff; color: #1f2933; padding: 0.35rem 0.65rem; border-radius: 0.35rem; cursor: pointer; }}
    .zoom-controls button:hover {{ background: #e2e8f0; }}
  </style>
</head>
<body>
  <h1>{escape(config.region.name)} City4CFD generated surfaces</h1>
  <div class="zoom-controls" aria-label="Generated surface preview zoom controls">
    <button type="button" data-view-index="0" data-zoom-in>Zoom in</button>
    <button type="button" data-view-index="0" data-zoom-out>Zoom out</button>
    <button type="button" data-view-index="0" data-zoom-reset>Reset zoom</button>
  </div>
  <div class="canvas-stack">
    <canvas id="stlScene" width="1400" height="900" aria-label="3D generated City4CFD surface preview"></canvas>
    <canvas id="meshOverlay" width="1400" height="900" aria-hidden="true"></canvas>
  </div>
  <div class="legend">
    <span><span class="swatch" style="background:#2563eb"></span>generated building mesh</span>
    <span><span class="swatch" style="background:#16a34a"></span>generated terrain mesh</span>
    {surface_legend}
  </div>
  <p class="note">Drag to rotate the generated surface preview. Use the mouse wheel or zoom buttons to zoom in and out. This plot renders the generated City4CFD OBJ meshes when present, with a bounded preview sample focused on the 3D objects so browser rendering remains responsive. If City4CFD has not produced meshes yet, the view falls back to the deterministic QA STL previews built from the same projected footprint and point-cloud evidence. Alignment status from module 2: {escape(str(diagnostics.get("alignment_status", "unknown")))}. Footprint overlap status: {escape(str(footprint_diagnostics["overlap_status"]))} ({footprint_diagnostics["overlap_pair_count"]} pairs).</p>
  <script>
    const stlScene = {stl_scene_json};
    const views = [
      {{ canvas: document.getElementById("stlScene"), overlayCanvas: document.getElementById("meshOverlay"), scene: stlScene, mode: "stl", yaw: -0.65, pitch: 0.78, zoom: 1.0, dragging: false, last: null }},
    ];

    function resize(view) {{
      const canvas = view.canvas;
      const ratio = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(640, Math.round(rect.width * ratio));
      canvas.height = Math.max(420, Math.round(rect.height * ratio));
      view.overlayCanvas.width = canvas.width;
      view.overlayCanvas.height = canvas.height;
      draw(view);
    }}

    function rotate(view, point) {{
      const [x, y, z] = point;
      const cy = Math.cos(view.yaw), sy = Math.sin(view.yaw);
      const cp = Math.cos(view.pitch), sp = Math.sin(view.pitch);
      const rx = x * cy - y * sy;
      const ry = x * sy + y * cy;
      const rz = z;
      return [rx, ry * cp + rz * sp, ry * sp - rz * cp];
    }}

    function project(view, point) {{
      const canvas = view.canvas;
      const [x, y, z] = rotate(view, point);
      const scale = Math.min(canvas.width, canvas.height) * 0.42 / view.scene.extent * view.zoom;
      return [canvas.width / 2 + x * scale, canvas.height * 0.62 - y * scale, z];
    }}

    function drawLine(view, a, b, color, width) {{
      const ctx = view.canvas.getContext("2d");
      const pa = project(view, a), pb = project(view, b);
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.beginPath();
      ctx.moveTo(pa[0], pa[1]);
      ctx.lineTo(pb[0], pb[1]);
      ctx.stroke();
    }}

    function drawFace(view, points, color, stroke) {{
      const ctx = view.canvas.getContext("2d");
      if (points.length < 3) return;
      const projected = points.map((point) => project(view, point));
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(projected[0][0], projected[0][1]);
      for (let i = 1; i < projected.length; i++) ctx.lineTo(projected[i][0], projected[i][1]);
      ctx.closePath();
      ctx.fill();
      if (stroke) {{
        ctx.strokeStyle = stroke;
        ctx.lineWidth = 0.7;
        ctx.stroke();
      }}
    }}

    function mat4Identity() {{
      const out = new Float32Array(16);
      out[0] = 1;
      out[5] = 1;
      out[10] = 1;
      out[15] = 1;
      return out;
    }}

    function mat4Multiply(a, b) {{
      const out = new Float32Array(16);
      const a00 = a[0], a01 = a[1], a02 = a[2], a03 = a[3];
      const a10 = a[4], a11 = a[5], a12 = a[6], a13 = a[7];
      const a20 = a[8], a21 = a[9], a22 = a[10], a23 = a[11];
      const a30 = a[12], a31 = a[13], a32 = a[14], a33 = a[15];
      const b00 = b[0], b01 = b[1], b02 = b[2], b03 = b[3];
      const b10 = b[4], b11 = b[5], b12 = b[6], b13 = b[7];
      const b20 = b[8], b21 = b[9], b22 = b[10], b23 = b[11];
      const b30 = b[12], b31 = b[13], b32 = b[14], b33 = b[15];

      out[0] = b00 * a00 + b01 * a10 + b02 * a20 + b03 * a30;
      out[1] = b00 * a01 + b01 * a11 + b02 * a21 + b03 * a31;
      out[2] = b00 * a02 + b01 * a12 + b02 * a22 + b03 * a32;
      out[3] = b00 * a03 + b01 * a13 + b02 * a23 + b03 * a33;
      out[4] = b10 * a00 + b11 * a10 + b12 * a20 + b13 * a30;
      out[5] = b10 * a01 + b11 * a11 + b12 * a21 + b13 * a31;
      out[6] = b10 * a02 + b11 * a12 + b12 * a22 + b13 * a32;
      out[7] = b10 * a03 + b11 * a13 + b12 * a23 + b13 * a33;
      out[8] = b20 * a00 + b21 * a10 + b22 * a20 + b23 * a30;
      out[9] = b20 * a01 + b21 * a11 + b22 * a21 + b23 * a31;
      out[10] = b20 * a02 + b21 * a12 + b22 * a22 + b23 * a32;
      out[11] = b20 * a03 + b21 * a13 + b22 * a23 + b23 * a33;
      out[12] = b30 * a00 + b31 * a10 + b32 * a20 + b33 * a30;
      out[13] = b30 * a01 + b31 * a11 + b32 * a21 + b33 * a31;
      out[14] = b30 * a02 + b31 * a12 + b32 * a22 + b33 * a32;
      out[15] = b30 * a03 + b31 * a13 + b32 * a23 + b33 * a33;
      return out;
    }}

    function mat4Translation(x, y, z) {{
      const out = mat4Identity();
      out[12] = x;
      out[13] = y;
      out[14] = z;
      return out;
    }}

    function mat4RotationX(angle) {{
      const out = mat4Identity();
      const c = Math.cos(angle);
      const s = Math.sin(angle);
      out[5] = c;
      out[6] = s;
      out[9] = -s;
      out[10] = c;
      return out;
    }}

    function mat4RotationY(angle) {{
      const out = mat4Identity();
      const c = Math.cos(angle);
      const s = Math.sin(angle);
      out[0] = c;
      out[2] = -s;
      out[8] = s;
      out[10] = c;
      return out;
    }}

    function mat4Perspective(fovy, aspect, near, far) {{
      const f = 1.0 / Math.tan(fovy / 2.0);
      const out = new Float32Array(16);
      out[0] = f / aspect;
      out[5] = f;
      out[10] = (far + near) / (near - far);
      out[11] = -1;
      out[14] = (2 * far * near) / (near - far);
      return out;
    }}

    function createShader(gl, type, source) {{
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {{
        throw new Error(gl.getShaderInfoLog(shader) || "failed to compile shader");
      }}
      return shader;
    }}

    function createProgram(gl, vertexSource, fragmentSource) {{
      const program = gl.createProgram();
      gl.attachShader(program, createShader(gl, gl.VERTEX_SHADER, vertexSource));
      gl.attachShader(program, createShader(gl, gl.FRAGMENT_SHADER, fragmentSource));
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {{
        throw new Error(gl.getProgramInfoLog(program) || "failed to link program");
      }}
      return program;
    }}

    function buildMeshBuffers(scene) {{
      const positions = [];
      const normals = [];
      const colors = [];
      for (const triangle of scene.triangles) {{
        const [a, b, c] = triangle.points;
        const ux = b[0] - a[0], uy = b[1] - a[1], uz = b[2] - a[2];
        const vx = c[0] - a[0], vy = c[1] - a[1], vz = c[2] - a[2];
        let nx = uy * vz - uz * vy;
        let ny = uz * vx - ux * vz;
        let nz = ux * vy - uy * vx;
        const length = Math.hypot(nx, ny, nz) || 1.0;
        nx /= length;
        ny /= length;
        nz /= length;
        const color = triangle.color || (triangle.kind === "terrain" ? [0.12, 0.60, 0.34] : [0.15, 0.42, 0.83]);
        for (const point of triangle.points) {{
          positions.push(point[0], point[1], point[2]);
          normals.push(nx, ny, nz);
          colors.push(color[0], color[1], color[2]);
        }}
      }}
      return {{
        positions: new Float32Array(positions),
        normals: new Float32Array(normals),
        colors: new Float32Array(colors),
        vertexCount: positions.length / 3,
      }};
    }}

    function initMeshView(view) {{
      if (view.gl || view.webglUnavailable) return;
      const gl = view.canvas.getContext("webgl2", {{ antialias: true, alpha: false }});
      if (!gl) {{
        view.webglUnavailable = true;
        return;
      }}
      const vertexSource = `#version 300 es
        in vec3 a_position;
        in vec3 a_normal;
        in vec3 a_color;
        uniform mat4 u_matrix;
        uniform mat4 u_normalMatrix;
        out vec3 v_normal;
        out vec3 v_color;
        void main() {{
          gl_Position = u_matrix * vec4(a_position, 1.0);
          v_normal = mat3(u_normalMatrix) * a_normal;
          v_color = a_color;
        }}
      `;
      const fragmentSource = `#version 300 es
        precision highp float;
        in vec3 v_normal;
        in vec3 v_color;
        out vec4 outColor;
        void main() {{
          vec3 n = normalize(v_normal);
          vec3 lightDir = normalize(vec3(0.45, 0.75, 0.50));
          float diffuse = max(dot(n, lightDir), 0.0);
          float light = 0.45 + diffuse * 0.55;
          outColor = vec4(v_color * light, 1.0);
        }}
      `;
      const program = createProgram(gl, vertexSource, fragmentSource);
      const mesh = buildMeshBuffers(view.scene);
      const vao = gl.createVertexArray();
      gl.bindVertexArray(vao);
      const positionBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, mesh.positions, gl.STATIC_DRAW);
      const positionLocation = gl.getAttribLocation(program, "a_position");
      gl.enableVertexAttribArray(positionLocation);
      gl.vertexAttribPointer(positionLocation, 3, gl.FLOAT, false, 0, 0);
      const normalBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, normalBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, mesh.normals, gl.STATIC_DRAW);
      const normalLocation = gl.getAttribLocation(program, "a_normal");
      gl.enableVertexAttribArray(normalLocation);
      gl.vertexAttribPointer(normalLocation, 3, gl.FLOAT, false, 0, 0);
      const colorBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, colorBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, mesh.colors, gl.STATIC_DRAW);
      const colorLocation = gl.getAttribLocation(program, "a_color");
      gl.enableVertexAttribArray(colorLocation);
      gl.vertexAttribPointer(colorLocation, 3, gl.FLOAT, false, 0, 0);
      gl.bindVertexArray(null);
      view.gl = gl;
      view.glProgram = program;
      view.glVao = vao;
      view.meshVertexCount = mesh.vertexCount;
      view.meshUniforms = {{
        matrix: gl.getUniformLocation(program, "u_matrix"),
        normalMatrix: gl.getUniformLocation(program, "u_normalMatrix"),
      }};
      gl.enable(gl.DEPTH_TEST);
      gl.disable(gl.CULL_FACE);
      gl.clearColor(1.0, 1.0, 1.0, 1.0);
    }}

    function drawMesh(view) {{
      initMeshView(view);
      if (!view.gl) {{
        const canvas = view.canvas;
        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        const faces = [...view.scene.triangles].sort((a, b) => {{
          const ap = a.points, bp = b.points;
          const ac = [(ap[0][0] + ap[1][0] + ap[2][0]) / 3, (ap[0][1] + ap[1][1] + ap[2][1]) / 3, (ap[0][2] + ap[1][2] + ap[2][2]) / 3];
          const bc = [(bp[0][0] + bp[1][0] + bp[2][0]) / 3, (bp[0][1] + bp[1][1] + bp[2][1]) / 3, (bp[0][2] + bp[1][2] + bp[2][2]) / 3];
          return rotate(view, ac)[2] - rotate(view, bc)[2];
        }});
        for (const triangle of faces) {{
          const rgb = triangle.color || (triangle.kind === "terrain" ? [0.12, 0.60, 0.34] : [0.15, 0.42, 0.83]);
          const fill = `rgba(${{Math.round(rgb[0] * 255)}}, ${{Math.round(rgb[1] * 255)}}, ${{Math.round(rgb[2] * 255)}}, 0.24)`;
          drawFace(view, triangle.points, fill, null);
        }}
        drawMeshOverlay(view);
        return;
      }}
      const gl = view.gl;
      const canvas = view.canvas;
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.useProgram(view.glProgram);
      gl.bindVertexArray(view.glVao);
      const aspect = canvas.width / canvas.height;
      const distance = Math.max(view.scene.extent * 2.8, 1.0) / view.zoom;
      const projection = mat4Perspective(45 * Math.PI / 180, aspect, 0.1, distance + view.scene.extent * 20.0);
      const rotation = mat4Multiply(mat4RotationX(view.pitch), mat4RotationY(view.yaw));
      const modelView = mat4Multiply(mat4Translation(0.0, 0.0, -distance), rotation);
      const matrix = mat4Multiply(projection, modelView);
      gl.uniformMatrix4fv(view.meshUniforms.matrix, false, matrix);
      gl.uniformMatrix4fv(view.meshUniforms.normalMatrix, false, rotation);
      gl.drawArrays(gl.TRIANGLES, 0, view.meshVertexCount);
      gl.bindVertexArray(null);
      drawMeshOverlay(view);
    }}

    function drawMeshOverlay(view) {{
      const overlay = view.overlayCanvas.getContext("2d");
      overlay.clearRect(0, 0, view.overlayCanvas.width, view.overlayCanvas.height);
      overlay.fillStyle = "#334155";
      overlay.font = `${{Math.max(13, view.overlayCanvas.width / 95)}}px Arial`;
      overlay.fillText(`Source: ${{view.scene.label}}`, 18, 28);
      if (view.scene.totalBuildingTriangles !== undefined) {{
        overlay.fillText(`Buildings: ${{view.scene.shownBuildingTriangles}} / ${{view.scene.totalBuildingTriangles}} triangles shown`, 18, 52);
        overlay.fillText(`Terrain: ${{view.scene.shownTerrainTriangles}} / ${{view.scene.totalTerrainTriangles}} triangles shown`, 18, 76);
        if (view.scene.totalSurfaceLayerTriangles !== undefined) {{
          overlay.fillText(`Surface layers: ${{view.scene.shownSurfaceLayerTriangles}} / ${{view.scene.totalSurfaceLayerTriangles}} triangles shown`, 18, 100);
        }}
      }}
    }}

    function draw(view) {{
      if (view.mode === "stl") {{
        drawMesh(view);
        return;
      }}
      const canvas = view.canvas;
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      const buildings = [...view.scene.buildings].sort((a, b) => rotate(view, a.center)[2] - rotate(view, b.center)[2]);
      for (const building of buildings) {{
        const top = building.top;
        const bottom = building.bottom;
        drawFace(view, top, building.hasRoofShape ? "rgba(15, 118, 110, 0.18)" : "rgba(217, 119, 6, 0.16)", null);
        if (building.peak) {{
          drawFace(view, [top[0], top[1], building.peak], "rgba(15, 118, 110, 0.10)", null);
        }}
      }}
      ctx.fillStyle = "#334155";
      ctx.font = `${{Math.max(13, canvas.width / 95)}}px Arial`;
      ctx.fillText(`3D buildings: ${{view.scene.buildings.length}}`, 18, 28);
      ctx.fillText(`LoD2.2 roof-shape evidence: ${{view.scene.roofShapeCount}}`, 18, 52);
    }}

    for (const view of views) {{
      view.canvas.addEventListener("pointerdown", (event) => {{ view.dragging = true; view.last = [event.clientX, event.clientY]; view.canvas.setPointerCapture(event.pointerId); }});
      view.canvas.addEventListener("pointermove", (event) => {{
        if (!view.dragging || !view.last) return;
        view.yaw += (event.clientX - view.last[0]) * 0.008;
        view.pitch = Math.max(0.15, Math.min(1.45, view.pitch + (event.clientY - view.last[1]) * 0.006));
        view.last = [event.clientX, event.clientY];
        draw(view);
      }});
      view.canvas.addEventListener("pointerup", () => {{ view.dragging = false; view.last = null; }});
      view.canvas.addEventListener("wheel", (event) => {{
        event.preventDefault();
        const factor = event.deltaY > 0 ? 0.9 : 1.1;
        view.zoom = Math.max(0.45, Math.min(2.75, view.zoom * factor));
        draw(view);
      }}, {{ passive: false }});
    }}
    for (const button of document.querySelectorAll("[data-view-index]")) {{
      button.addEventListener("click", () => {{
        const view = views[Number(button.dataset.viewIndex)];
        if (button.hasAttribute("data-zoom-in")) view.zoom = Math.max(0.45, Math.min(2.75, view.zoom * 1.2));
        if (button.hasAttribute("data-zoom-out")) view.zoom = Math.max(0.45, Math.min(2.75, view.zoom / 1.2));
        if (button.hasAttribute("data-zoom-reset")) view.zoom = 1.0;
        draw(view);
      }});
    }}
    window.addEventListener("resize", () => views.forEach(resize));
    views.forEach(resize);
  </script>
</body>
</html>
"""


def _render_report(
    config: AppConfig,
    city4cfd_config_path: Path,
    manifest_path: Path,
    footprint_diagnostics_path: Path,
    run_script_path: Path,
    footprint_path: Path,
    building_stl_path: Path,
    terrain_stl_path: Path,
    building_mesh_path: Path,
    terrain_mesh_path: Path,
    combined_terrain_mesh_path: Path,
    surface_mesh_paths: dict[str, Path],
    stage1_surface_layers: list[dict[str, Any]],
    preview_path: Path,
    diagnostics: dict[str, Any],
    footprint_diagnostics: dict[str, Any],
    building_count: int,
    execution: City4CFDExecutionResult,
    stdout_log_path: Path,
    stderr_log_path: Path,
) -> str:
    return f"""# City4CFD Reconstruction Handoff Report

## Region

- Name: {config.region.name}
- CRS: {config.region.crs}
- LoD target: 2.2
- Alignment status: {diagnostics.get("alignment_status", "unknown")}
- Footprint overlap status: {footprint_diagnostics["overlap_status"]}
- Overlapping footprint pairs: {footprint_diagnostics["overlap_pair_count"]}
- Preserved inner rings: {footprint_diagnostics["inner_ring_count"]}
- Buildings prepared: {building_count}

## Outputs

- City4CFD config: `{city4cfd_config_path}`
- Reconstruction manifest: `{manifest_path}`
- Footprint diagnostics: `{footprint_diagnostics_path}`
- Run script: `{run_script_path}`
- Projected building footprints: `{footprint_path}`
- Offline building STL preview: `{building_stl_path}`
- Offline terrain STL preview: `{terrain_stl_path}`
- City4CFD building mesh: `{building_mesh_path}`
- City4CFD terrain mesh: `{terrain_mesh_path}`
- Combined City4CFD terrain OBJ: `{combined_terrain_mesh_path}` ({"present" if combined_terrain_mesh_path.exists() else "not present"})
- City4CFD semantic surface meshes: {len(surface_mesh_paths)} expected, {sum(path.exists() for path in surface_mesh_paths.values())} present
- Graphical preview: `{preview_path}`
- City4CFD stdout log: `{stdout_log_path}`
- City4CFD stderr log: `{stderr_log_path}`

## Stage 1 Surface Layers

The stage-1 surface categories are projected from EPSG:4326 into `{config.region.crs}` and carried into the City4CFD handoff as named SurfaceLayer polygon imports. Empty categories are ignored. With separate output enabled, City4CFD writes each imprinted category as its own `{config.city_models.output_file_name}_<layer_name>.{config.city_models.output_format}` mesh.

{_render_surface_layer_report(stage1_surface_layers, surface_mesh_paths)}

## Execution

This stage prepares the City4CFD inputs from the stage-03 TOML settings, checks whether `city4cfd` is available, and runs it directly or through Docker when needed. The generated script remains available as a reproducible fallback for environments where the tool is installed later.

- Stage status: `{_stage_status(execution)}`
- External execution status: `{execution.status}`
- Backend: `{execution.backend or "none"}`
- Return code: `{execution.return_code if execution.return_code is not None else "not run"}`
- Argument vector: `{list(execution.argv) if execution.argv else "not run"}`
- Stdout truncated: `{execution.stdout_truncated}`
- Stderr truncated: `{execution.stderr_truncated}`

## Assumptions

- Module 2 already produced separate ground and building point clouds in the same projected CRS as the footprints.
- The City4CFD footprint GeoJSON preserves full polygon geometry, including inner rings/holes.
- Overlapping footprint pairs are reported for review because superposed footprints can create duplicated reconstructed surfaces.
- LoD2.2 roof geometry is expected to come from City4CFD/roofer using the building point cloud and projected roofprint polygons.
- The preview shows the actual City4CFD mesh outputs when they are present. The local STL previews remain deterministic QA fallbacks for environments where the generated meshes are missing.
"""


def _render_surface_layer_report(
    stage1_surface_layers: list[dict[str, Any]],
    surface_mesh_paths: dict[str, Path],
) -> str:
    if not stage1_surface_layers:
        return "- No stage-1 surface layers were imported."
    lines = [
        f"- `{layer['category']}` -> `{layer['layer_path']}` as `SurfaceLayer` with `layer_name={layer['layer_name']}` ({layer['feature_count']} features); generated mesh: `{surface_mesh_paths[layer['category']]}` ({'present' if surface_mesh_paths[layer['category']].exists() else 'not present'})"
        for layer in stage1_surface_layers
    ]
    return "\n".join(lines)


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


def _stl_scene_data(
    triangles: list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]],
) -> dict[str, Any]:
    points = [point for _label, a, b, c in triangles for point in (a, b, c)]
    if not points:
        return {"extent": 1.0, "triangles": [], "source": "qa-stl-preview", "label": "QA preview triangles"}
    center_x, center_y = _triangle_focus_point(triangles)
    extent = max(
        max(point[0] for point in points) - min(point[0] for point in points),
        max(point[1] for point in points) - min(point[1] for point in points),
        1.0,
    ) / 2.0
    return {
        "extent": extent,
        "source": "qa-stl-preview",
        "label": "QA preview triangles",
        "triangles": [
            {
                "kind": "terrain" if label == "terrain" else "building",
                "points": [
                    [round(a[0] - center_x, 3), round(a[1] - center_y, 3), round(a[2], 3)],
                    [round(b[0] - center_x, 3), round(b[1] - center_y, 3), round(b[2], 3)],
                    [round(c[0] - center_x, 3), round(c[1] - center_y, 3), round(c[2], 3)],
                ],
            }
            for label, a, b, c in triangles
        ],
    }


def _surface_layer_color_map(
    surface_layers: dict[str, Any],
) -> dict[str, tuple[float, float, float]]:
    return {
        category: SURFACE_LAYER_PREVIEW_COLORS[index % len(SURFACE_LAYER_PREVIEW_COLORS)]
        for index, category in enumerate(surface_layers)
    }


def _render_surface_layer_legend(stage1_surface_layers: list[dict[str, Any]]) -> str:
    colors = _surface_layer_color_map(
        {layer["category"]: None for layer in stage1_surface_layers}
    )
    entries: list[str] = []
    for layer in stage1_surface_layers:
        category = str(layer["category"])
        red, green, blue = colors[category]
        color = f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"
        entries.append(
            f'<span><span class="swatch" style="background:{color}"></span>'
            f'{escape(category)} surface layer</span>'
        )
    return "\n    ".join(entries)


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


def _city4cfd_mesh_scene_data(
    building_mesh_path: Path,
    terrain_mesh_path: Path,
    surface_mesh_paths: dict[str, Path] | None = None,
) -> dict[str, Any]:
    building_triangles = _read_obj_triangles(building_mesh_path, "building")
    terrain_triangles = _read_obj_triangles(terrain_mesh_path, "terrain")
    surface_mesh_paths = surface_mesh_paths or {}
    surface_layer_triangles = {
        category: _read_obj_triangles(path, f"surface_layer:{category}")
        for category, path in surface_mesh_paths.items()
    }
    all_surface_layer_triangles = [
        triangle
        for triangles in surface_layer_triangles.values()
        for triangle in triangles
    ]
    all_triangles = [*building_triangles, *terrain_triangles, *all_surface_layer_triangles]
    if not all_triangles:
        fallback = _stl_scene_data([])
        fallback["source"] = "qa-stl-preview"
        fallback["label"] = "QA preview triangles"
        return fallback
    sampled_building_triangles = _evenly_sample_triangles(
        building_triangles,
        MAX_CITY4CFD_BUILDING_PREVIEW_TRIANGLES,
    )
    sampled_terrain_triangles = _evenly_sample_triangles(
        terrain_triangles,
        MAX_CITY4CFD_TERRAIN_PREVIEW_TRIANGLES,
    )
    sampled_surface_layer_triangles = [
        triangle
        for layer_triangles in surface_layer_triangles.values()
        for triangle in _evenly_sample_triangles(
            layer_triangles,
            MAX_CITY4CFD_SURFACE_LAYER_PREVIEW_TRIANGLES,
        )
    ]
    triangles = [
        *sampled_terrain_triangles,
        *sampled_surface_layer_triangles,
        *sampled_building_triangles,
    ]
    points = [point for _kind, a, b, c in all_triangles for point in (a, b, c)]
    center_x, center_y = _triangle_focus_point(building_triangles or all_triangles)
    base_z = min(point[2] for point in points)
    extent = max(
        max(point[0] for point in points) - min(point[0] for point in points),
        max(point[1] for point in points) - min(point[1] for point in points),
        max(point[2] for point in points) - base_z,
        1.0,
    ) / 2.0
    surface_colors = _surface_layer_color_map(surface_mesh_paths)
    return {
        "extent": extent,
        "source": "city4cfd" if building_triangles or terrain_triangles else "qa-stl-preview",
        "label": "City4CFD OBJ triangles" if building_triangles or terrain_triangles else "QA preview triangles",
        "totalBuildingTriangles": len(building_triangles),
        "totalTerrainTriangles": len(terrain_triangles),
        "shownBuildingTriangles": len(sampled_building_triangles),
        "shownTerrainTriangles": len(sampled_terrain_triangles),
        "totalSurfaceLayerTriangles": len(all_surface_layer_triangles),
        "shownSurfaceLayerTriangles": len(sampled_surface_layer_triangles),
        "triangles": [
            {
                "kind": kind,
                **(
                    {"color": list(surface_colors[kind.split(":", 1)[1]])}
                    if kind.startswith("surface_layer:")
                    else {}
                ),
                "points": [
                    [round(a[0] - center_x, 3), round(a[1] - center_y, 3), round(a[2] - base_z, 3)],
                    [round(b[0] - center_x, 3), round(b[1] - center_y, 3), round(b[2] - base_z, 3)],
                    [round(c[0] - center_x, 3), round(c[1] - center_y, 3), round(c[2] - base_z, 3)],
                ],
            }
            for kind, a, b, c in triangles
        ],
    }


def _evenly_sample_triangles(
    triangles: list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]],
    limit: int,
) -> list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]]:
    if len(triangles) <= limit:
        return triangles
    step = len(triangles) / limit
    return [triangles[min(int(index * step), len(triangles) - 1)] for index in range(limit)]


def _triangle_focus_point(
    triangles: list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]],
) -> tuple[float, float]:
    building_points = [point for kind, a, b, c in triangles if kind == "building" for point in (a, b, c)]
    points = building_points or [point for _kind, a, b, c in triangles for point in (a, b, c)]
    if not points:
        return 0.0, 0.0
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _read_obj_triangles(
    path: Path,
    kind: str,
) -> list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]]:
    if not path.exists():
        return []
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("v "):
                parts = line.split()
                if len(parts) >= 4:
                    vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                continue
            if not line.startswith("f "):
                continue
            parts = line.split()[1:]
            indices: list[int] = []
            for part in parts:
                index_text = part.split("/")[0]
                if not index_text:
                    continue
                index = int(index_text)
                if index < 0:
                    index = len(vertices) + index + 1
                indices.append(index - 1)
            if len(indices) < 3:
                continue
            anchor = vertices[indices[0]]
            for left, right in zip(indices[1:], indices[2:]):
                triangles.append((kind, anchor, vertices[left], vertices[right]))
    return triangles
