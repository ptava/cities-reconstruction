"""Payload assembly and manifest-last publication for air-purifier placement."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cities_reconstruction.artifacts import lightweight_state_fingerprint
from cities_reconstruction.config import AppConfig, ConfigError
from cities_reconstruction.geometry.stl_regions import REGION_NAMES
from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    JsonValue,
    StageManifest,
    StageStatus,
    load_stage_manifest,
    publish_stage_manifest,
)
from cities_reconstruction.stage_layout import StageId
from cities_reconstruction.stages.air_purifiers.geometry import PURIFIER_ID_PATTERN, TERRAIN_CLEARANCE_M
from cities_reconstruction.stages.air_purifiers.models import AirPurifierInstance, AirPurifierModel

STAGE_ID = StageId.AIR_PURIFIERS


@dataclass(frozen=True)
class AirPurifiersPublicationInput:
    config: AppConfig
    output_directory: Path
    source_geojson: Path
    model_library_path: Path
    terrain_geometry_path: Path | None
    models: dict[str, AirPurifierModel]
    instances: list[AirPurifierInstance]
    model_counts: dict[str, int]
    input_counts: dict[str, int]
    parameter_source_counts: dict[str, dict[str, int]]
    placement_path: Path
    report_path: Path
    preview_path: Path
    combined_path: Path
    instance_paths: dict[str, Path]
    origin_x: float
    origin_y: float


def publish_air_purifiers_manifest(publication: AirPurifiersPublicationInput) -> StageManifest:
    artifacts = (
        ArtifactReference("combined-surface", publication.combined_path, ArtifactKind.HANDOFF),
        *(
            ArtifactReference(f"instance-{purifier_id}", path, ArtifactKind.HANDOFF)
            for purifier_id, path in sorted(publication.instance_paths.items())
        ),
        ArtifactReference("placements", publication.placement_path, ArtifactKind.SUPPORTING),
        ArtifactReference("report", publication.report_path, ArtifactKind.REPORT),
        ArtifactReference("preview", publication.preview_path, ArtifactKind.PREVIEW),
    )
    return publish_stage_manifest(
        stage=STAGE_ID.value,
        status=StageStatus.COMPLETED,
        output_directory=publication.output_directory,
        report_path=publication.report_path,
        preview_path=publication.preview_path,
        input_state_fingerprint=_air_purifiers_input_fingerprint(
            publication.config,
            publication.source_geojson,
            publication.model_library_path,
            publication.terrain_geometry_path,
            publication.models,
        ),
        artifacts=artifacts,
        metrics={
            "purifier_count": len(publication.instances),
            "model_counts": _json_counts(publication.model_counts),
            "input_counts": _json_counts(publication.input_counts),
            "parameter_source_counts": {
                field: _json_counts(counts)
                for field, counts in publication.parameter_source_counts.items()
            },
        },
        details={
            "source_geojson": str(publication.source_geojson),
            "model_library": str(publication.model_library_path),
            "model_files": {
                name: str(model.source_path) for name, model in sorted(publication.models.items())
            },
            "resolved_overrides": {
                "model_library_path": str(publication.model_library_path),
                "terrain_geometry_path": (
                    str(publication.terrain_geometry_path)
                    if publication.terrain_geometry_path
                    else None
                ),
            },
            "local_origin": {
                "crs": "EPSG:25832",
                "easting": publication.origin_x,
                "northing": publication.origin_y,
            },
            "terrain": {
                "path": (
                    str(publication.terrain_geometry_path)
                    if publication.terrain_geometry_path
                    else None
                ),
                "status": "projected" if publication.terrain_geometry_path else "z=0 fallback",
                "base_clearance_m": TERRAIN_CLEARANCE_M if publication.terrain_geometry_path else 0.0,
                "footprint_validation": "all four rotated bounding-box corners",
            },
            "openfoam_handoff": {
                "aggregate_surface": str(publication.combined_path),
                "regions": list(REGION_NAMES),
            },
        },
    )


def placement_payload(instances: list[AirPurifierInstance]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "name": "air_purifier_placements",
        "crs": {"type": "name", "properties": {"name": "EPSG:25832"}},
        "features": [
            {
                "type": "Feature", "geometry": {"type": "Point", "coordinates": [item.projected_x, item.projected_y]},
                "properties": {
                    "purifier_id": item.purifier_id, "model": item.model_name,
                    "source_coordinates": [item.source_lon, item.source_lat],
                    "source_coordinate_crs": "EPSG:4326",
                    "local_x": item.local_x, "local_y": item.local_y, "base_z": item.base_z,
                    "height_m": item.target_height_m, "width_m": item.target_width_m, "depth_m": item.target_depth_m,
                    "native_height_m": item.native_height_m, "native_width_m": item.native_width_m, "native_depth_m": item.native_depth_m,
                    "scale_x": item.scale_x, "scale_y": item.scale_y, "scale_z": item.scale_z,
                    "rotation_deg": item.rotation_deg, "height_source": item.height_source,
                    "width_source": item.width_source, "depth_source": item.depth_source,
                    "rotation_source": item.rotation_source, "terrain_source": item.terrain_source,
                    "urban_planning_input_id": item.input_id,
                    "source": item.source, "source_crs": item.source_crs,
                    "source_feature_index": item.source_feature_index,
                    "roi_zone": item.roi_zone, "source_properties": item.source_properties,
                },
            }
            for item in instances
        ],
    }


def prior_instance_allowlist(manifest_path: Path, instances_dir: Path) -> set[Path]:
    try:
        manifest = load_stage_manifest(manifest_path, expected_stage=STAGE_ID.value)
    except ConfigError:
        return set()
    parent = instances_dir.resolve()
    allowed: set[Path] = set()
    for artifact in manifest.artifacts:
        if artifact.kind is not ArtifactKind.HANDOFF:
            continue
        path = artifact.path.resolve()
        purifier_id = path.stem
        if not PURIFIER_ID_PATTERN.fullmatch(purifier_id):
            continue
        if path.parent == parent and path.name == f"{purifier_id}.stl":
            allowed.add(path)
    return allowed


def _air_purifiers_input_fingerprint(
    config: AppConfig,
    source_geojson: Path,
    model_library_path: Path,
    terrain_geometry_path: Path | None,
    models: dict[str, AirPurifierModel],
) -> dict[str, JsonValue]:
    paths = [config.path, source_geojson, model_library_path]
    paths.extend(model.source_path for model in models.values())
    if terrain_geometry_path is not None:
        paths.append(terrain_geometry_path)
    return lightweight_state_fingerprint(
        {
            "stage": "air-purifiers",
            "crs": config.region.crs,
            "center": [config.region.center_lon, config.region.center_lat],
            "model_library_path": str(model_library_path),
            "terrain_geometry_path": str(terrain_geometry_path) if terrain_geometry_path else None,
        },
        paths,
    )


def _json_counts(counts: dict[str, int]) -> dict[str, JsonValue]:
    return {name: count for name, count in counts.items()}
