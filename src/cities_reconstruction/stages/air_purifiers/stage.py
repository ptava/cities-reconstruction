"""Place catalogued air-purifier towers in the City4CFD local frame."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cities_reconstruction.artifacts import (
    atomic_write_json,
    atomic_write_text,
    stage_output_lock,
)
from cities_reconstruction.config import AppConfig, ConfigError
from cities_reconstruction.geometry.stl_regions import (
    REGION_NAMES,
    RegionMesh,
    transform_region_mesh,
    write_region_stl,
)
from cities_reconstruction.geometry.terrain import (
    TerrainSampler,
    load_terrain_sampler,
    validate_completed_city_models_terrain,
)
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
from cities_reconstruction.stage_layout import STAGE_LAYOUT_BY_ID, StageId, stage_output_directory
from cities_reconstruction.stage_result import StageResult
from cities_reconstruction.stages.air_purifiers import publication as air_purifiers_publication
from cities_reconstruction.stages.air_purifiers import reporting as air_purifiers_reporting
from cities_reconstruction.stages.air_purifiers.diagnostics import counts as _counts
from cities_reconstruction.stages.air_purifiers.geometry import (
    lonlat_to_epsg25832 as _lonlat_to_epsg25832,
)
from cities_reconstruction.stages.air_purifiers.geometry import (
    resolve_instances as _resolve_instances,
)
from cities_reconstruction.stages.air_purifiers.inputs import (
    load_features as _load_features,
)
from cities_reconstruction.stages.air_purifiers.inputs import (
    load_model_library as _load_model_library,
)
from cities_reconstruction.stages.air_purifiers.rendering import render_preview as _render_preview

STAGE_ID = StageId.AIR_PURIFIERS
SHAPEFILES_NUMBER_NAME = STAGE_LAYOUT_BY_ID[StageId.SHAPEFILES].number_name


@dataclass(frozen=True)
class AirPurifiersStageOutput:
    manifest: StageManifest
    placement_geojson_path: Path
    catalog_path: Path
    surfaces_directory: Path
    combined_stl_path: Path
    instance_stl_paths: dict[str, Path]
    purifier_count: int
    model_counts: dict[str, int]

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


def plan(config: AppConfig) -> StageResult:
    output = stage_output_directory(config.output.root_directory, STAGE_ID)
    catalog = config.air_purifiers.model_library_path
    terrain = config.air_purifiers.terrain_geometry_path
    return StageResult(
        stage=STAGE_ID.value,
        summary="Place normalized air-purifier models and publish CFD-ready three-region STL surfaces.",
        planned_actions=(
            f"Read normalized purifier points from {SHAPEFILES_NUMBER_NAME}/air_purifiers.geojson.",
            f"Resolve model library: {catalog if catalog is not None else 'unresolved (required at execution)' }.",
            f"Resolve terrain geometry: {terrain if terrain is not None else 'unresolved (use z=0)' }.",
            "Scale, rotate, terrain-project, and write aggregate/per-unit inlet, outlet, and tower surfaces.",
            "Publish placements, offline preview, report, and completion manifest.",
        ),
        expected_outputs=(output,),
    )


def run(
    config: AppConfig,
    *,
    model_library_path: Path | str | None = None,
    terrain_geometry_path: Path | str | None = None,
) -> AirPurifiersStageOutput:
    output_dir = stage_output_directory(config.output.root_directory, STAGE_ID)
    instances_dir = output_dir / "surfaces" / "instances"
    with stage_output_lock(output_dir, STAGE_ID.value):
        prior_instance_paths = air_purifiers_publication.prior_instance_allowlist(
            output_dir / "manifest.json",
            instances_dir,
        )
        invalidate_stage_manifests(
            output_dir,
            legacy_names=("air_purifier_models_manifest.json",),
        )
        if config.region.crs != "EPSG:25832":
            raise ConfigError("air-purifier generation currently supports EPSG:25832 output coordinates")
        return _run_locked(
            config,
            model_library_path=_effective_path(config, model_library_path, config.air_purifiers.model_library_path),
            terrain_geometry_path=_effective_path(config, terrain_geometry_path, config.air_purifiers.terrain_geometry_path),
            prior_instance_paths=prior_instance_paths,
        )


def _run_locked(
    config: AppConfig,
    *,
    model_library_path: Path | None,
    terrain_geometry_path: Path | None,
    prior_instance_paths: set[Path],
) -> AirPurifiersStageOutput:
    if model_library_path is None:
        raise ConfigError("air-purifier model library is unresolved; configure model_library_path or provide an override")
    stage1_manifest = require_completed_manifest(
        stage_output_directory(config.output.root_directory, StageId.SHAPEFILES) / "manifest.json",
        expected_stage=StageId.SHAPEFILES.value,
    )
    source_geojson = require_manifest_artifact(
        stage1_manifest,
        name="air-purifiers",
        kind=ArtifactKind.HANDOFF,
    ).path

    output_dir = stage_output_directory(config.output.root_directory, STAGE_ID)
    surfaces_dir = output_dir / "surfaces"
    instances_dir = surfaces_dir / "instances"
    placement_path = output_dir / "air_purifier_placements.geojson"
    manifest_path = output_dir / "manifest.json"
    preview_path = output_dir / "air_purifier_models_preview.html"
    report_path = output_dir / "air_purifier_models_report.md"
    combined_path = surfaces_dir / "air_purifiers_combined.stl"
    models = _load_model_library(model_library_path)
    features = _load_features(source_geojson)
    origin_x, origin_y = _lonlat_to_epsg25832(config.region.center_lon, config.region.center_lat)
    terrain_sampler: TerrainSampler | None = None
    if terrain_geometry_path is not None:
        validate_completed_city_models_terrain(config, terrain_geometry_path, context="air-purifier")
        terrain_sampler = load_terrain_sampler(
            terrain_geometry_path,
            footprint_label="air-purifier footprint",
        )
    instances = _resolve_instances(
        features,
        models,
        origin_x=origin_x,
        origin_y=origin_y,
        terrain_path=terrain_geometry_path,
        terrain_sampler=terrain_sampler,
    )

    aggregate: RegionMesh = {region: [] for region in REGION_NAMES}
    instance_paths: dict[str, Path] = {}
    instance_meshes: dict[str, RegionMesh] = {}
    for instance in instances:
        transformed = transform_region_mesh(
            models[instance.model_name].mesh,
            scale=(instance.scale_x, instance.scale_y, instance.scale_z),
            rotation_deg=instance.rotation_deg,
            translation=(instance.local_x, instance.local_y, instance.base_z),
        )
        instance_path = instances_dir / f"{instance.purifier_id}.stl"
        write_region_stl(instance_path, transformed)
        instance_paths[instance.purifier_id] = instance_path
        instance_meshes[instance.purifier_id] = transformed
        for region in REGION_NAMES:
            aggregate[region].extend(transformed[region])
    write_region_stl(combined_path, aggregate)

    current_paths = {path.resolve() for path in instance_paths.values()}
    for stale_path in prior_instance_paths - current_paths:
        stale_path.unlink(missing_ok=True)

    model_counts = _counts(instance.model_name for instance in instances)
    input_counts = _counts(instance.input_id for instance in instances)
    parameter_source_counts = {
        field: _counts(getattr(instance, field) for instance in instances)
        for field in ("height_source", "width_source", "depth_source", "rotation_source")
    }
    atomic_write_json(placement_path, air_purifiers_publication.placement_payload(instances))
    atomic_write_text(preview_path, _render_preview(instances, instance_meshes, origin_x, origin_y))
    atomic_write_text(
        report_path,
        air_purifiers_reporting.render_report(
            source_geojson, model_library_path, terrain_geometry_path, origin_x, origin_y,
            instances, model_counts, input_counts, parameter_source_counts,
            placement_path, combined_path, instance_paths, preview_path, manifest_path,
        ),
    )
    manifest = air_purifiers_publication.publish_air_purifiers_manifest(
        air_purifiers_publication.AirPurifiersPublicationInput(
            config=config,
            output_directory=output_dir,
            source_geojson=source_geojson,
            model_library_path=model_library_path,
            terrain_geometry_path=terrain_geometry_path,
            models=models,
            instances=instances,
            model_counts=model_counts,
            input_counts=input_counts,
            parameter_source_counts=parameter_source_counts,
            placement_path=placement_path,
            report_path=report_path,
            preview_path=preview_path,
            combined_path=combined_path,
            instance_paths=instance_paths,
            origin_x=origin_x,
            origin_y=origin_y,
        )
    )
    return AirPurifiersStageOutput(
        manifest=manifest, placement_geojson_path=placement_path,
        catalog_path=model_library_path,
        surfaces_directory=surfaces_dir, combined_stl_path=combined_path,
        instance_stl_paths=instance_paths, purifier_count=len(instances), model_counts=model_counts,
    )


def _effective_path(config: AppConfig, override: Path | str | None, configured: Path | None) -> Path | None:
    if override is None:
        return configured
    path = Path(override)
    return path if path.is_absolute() else (config.path.parent / path).resolve()
