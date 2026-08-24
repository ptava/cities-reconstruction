"""Point-cloud preparation for City4CFD.

City4CFD needs separate ground and building point clouds. This module creates
those PLY files from paired DTM/DSM ASCII grids, preserves DSM points that are
not classified as buildings or trees, and records the footprint alignment
checks that should be reviewed before reconstruction.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    publish_stage_manifest,
    require_completed_manifest,
    require_manifest_artifact,
)
from cities_reconstruction.stage_layout import StageId, stage_output_directory
from cities_reconstruction.stage_result import StageResult

from .diagnostics import build_alignment_diagnostics
from .geometry import (
    BUILDING_HEIGHT_THRESHOLD_M,
    TREE_BUILDING_FOOTPRINT_BUFFER_M,
    TREE_ROOF_OFFSET_THRESHOLD_M,
    TREE_ROOF_SEARCH_RADIUS_M,
    Point2,
    ProjectedPolygon,
    Ring,
    classify_raster_points,
)
from .inputs import (
    read_feature_collection,
    read_png_rgba,
)
from .rendering import render_preview_html
from .reporting import render_report

SUPPORTED_PROJECTED_CRS = "EPSG:25832"


@dataclass(frozen=True)
class PointCloudStageOutput:
    manifest: StageManifest
    projected_footprints_path: Path
    ground_points_path: Path
    building_points_path: Path
    tree_points_path: Path | None
    unclassified_points_path: Path
    diagnostics_path: Path
    ground_point_count: int
    building_point_count: int
    tree_point_count: int
    unclassified_point_count: int
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


STAGE_ID = StageId.POINT_CLOUD


def plan(config: AppConfig) -> StageResult:
    output = stage_output_directory(config.output.root_directory, STAGE_ID)
    if config.inputs.point_cloud_path is not None:
        source_action = (
            f"Reject single point-cloud input {config.inputs.point_cloud_path} until the config supports "
            "explicit ground/building point-cloud paths."
        )
    else:
        source_action = (
            "Prepare separate City4CFD point clouds from DTM/DSM directories: "
            f"DTM={config.inputs.dtm_directory}, DSM={config.inputs.dsm_directory}."
        )

    return StageResult(
        stage=STAGE_ID.value,
        summary="Prepare City4CFD point clouds, unclassified DSM points, and footprint alignment diagnostics.",
        planned_actions=(
            source_action,
            "Read default building footprints from "
            f"{stage_output_directory(config.output.root_directory, StageId.SHAPEFILES) / 'buildings.geojson'}; "
            "an execution-time CLI override must be explicit.",
            "Project footprints to the configured metric CRS and split DSM cells into building points.",
            "Optionally combine `inputs.tree_canopy_overlay_path` with stage-1 tree tags to identify DSM tree points.",
            "Write `ground_points.ply`, `building_points.ply`, optional `tree_points.ply`, "
            "`unclassified_points.ply`, an alignment report, and a graphical QA preview.",
        ),
        expected_outputs=(output,),
    )


def run(
    config: AppConfig,
    *,
    building_footprints_path: Path | None = None,
) -> PointCloudStageOutput:
    """Generate separate ground and building PLY files for City4CFD."""

    output_dir = stage_output_directory(config.output.root_directory, STAGE_ID)
    with stage_output_lock(output_dir, STAGE_ID.value):
        invalidate_stage_manifests(
            output_dir,
            legacy_names=("city4cfd_point_cloud_manifest.json",),
        )
        _validate_inputs(config)
        return _run_locked(config, building_footprints_path=building_footprints_path)


def _run_locked(
    config: AppConfig,
    *,
    building_footprints_path: Path | None,
) -> PointCloudStageOutput:
    output_dir = stage_output_directory(config.output.root_directory, STAGE_ID)
    manifest_path = output_dir / "manifest.json"

    footprint_path = _select_building_footprints_path(config, building_footprints_path)
    try:
        footprints = read_feature_collection(footprint_path)
    except OSError as exc:
        raise ConfigError(f"cannot read selected building footprints: {footprint_path}: {exc}") from exc
    projected_features = [_project_feature(feature, config) for feature in footprints]
    projected_footprints = [polygon for feature in projected_features for polygon in _project_feature_polygon(feature)]
    bbox = _region_bbox_projected(config)

    tree_mask = _load_tree_canopy_mask(config)
    tree_features_path = _select_optional_tree_features_path(config) if tree_mask is not None else None
    tree_tag_points = _load_projected_tree_tag_points(tree_features_path)
    (
        ground_points,
        building_points,
        tree_points,
        unclassified_points,
        alignment_candidate_points,
        raster_summary,
    ) = classify_raster_points(
        dtm_directory=config.inputs.dtm_directory,
        dsm_directory=config.inputs.dsm_directory,
        bbox=bbox,
        building_polygons=projected_footprints,
        tree_mask=tree_mask,
        tree_tag_points=tree_tag_points,
    )
    diagnostics = build_alignment_diagnostics(
        config=config,
        footprint_path=footprint_path,
        building_polygons=projected_footprints,
        ground_points=ground_points,
        building_points=building_points,
        alignment_candidate_points=alignment_candidate_points,
        tree_points=tree_points,
        unclassified_points=unclassified_points,
        raster_summary=raster_summary,
        tree_mask=tree_mask,
        tree_tag_points=tree_tag_points,
        same_metric_output_crs=config.region.crs.upper() == SUPPORTED_PROJECTED_CRS,
    )

    ground_path = output_dir / "ground_points.ply"
    building_path = output_dir / "building_points.ply"
    tree_path = output_dir / "tree_points.ply" if tree_mask is not None else None
    unclassified_path = output_dir / "unclassified_points.ply"
    projected_footprints_path = output_dir / "building_footprints_epsg25832.geojson"
    diagnostics_path = output_dir / "alignment_diagnostics.json"
    preview_path = output_dir / "point_cloud_alignment_preview.html"
    report_path = output_dir / "point_cloud_report.md"

    _write_ply(ground_path, ground_points)
    _write_ply(building_path, building_points)
    _write_ply(unclassified_path, unclassified_points)
    if tree_path is not None:
        _write_ply(tree_path, tree_points)
    else:
        (output_dir / "tree_points.ply").unlink(missing_ok=True)
    _write_geojson(projected_footprints_path, projected_features, config.region.crs)
    atomic_write_json(diagnostics_path, diagnostics)
    fingerprint = _point_cloud_input_fingerprint(config, footprint_path, tree_features_path)
    atomic_write_text(
        preview_path,
        render_preview_html(
            config,
            projected_footprints,
            ground_points,
            building_points,
            tree_points,
            unclassified_points,
            diagnostics,
            projected_bbox=bbox,
            tree_building_footprint_buffer_m=TREE_BUILDING_FOOTPRINT_BUFFER_M,
            tree_roof_offset_threshold_m=TREE_ROOF_OFFSET_THRESHOLD_M,
            tree_roof_search_radius_m=TREE_ROOF_SEARCH_RADIUS_M,
        ),
    )
    atomic_write_text(
        report_path,
        render_report(
            config=config,
            footprint_path=footprint_path,
            projected_footprints_path=projected_footprints_path,
            ground_path=ground_path,
            building_path=building_path,
            tree_path=tree_path,
            unclassified_path=unclassified_path,
            diagnostics_path=diagnostics_path,
            manifest_path=manifest_path,
            preview_path=preview_path,
            diagnostics=diagnostics,
        ),
    )
    artifacts = [
        ArtifactReference(
            "projected-building-footprints",
            projected_footprints_path,
            ArtifactKind.HANDOFF,
        ),
        ArtifactReference("ground-points", ground_path, ArtifactKind.HANDOFF),
        ArtifactReference("building-points", building_path, ArtifactKind.HANDOFF),
    ]
    if tree_path is not None:
        artifacts.append(
            ArtifactReference(
                "tree-points",
                tree_path,
                ArtifactKind.HANDOFF,
                required=False,
            )
        )
    artifacts.extend(
        (
            ArtifactReference(
                "unclassified-points",
                unclassified_path,
                ArtifactKind.DIAGNOSTIC,
            ),
            ArtifactReference(
                "alignment-diagnostics",
                diagnostics_path,
                ArtifactKind.DIAGNOSTIC,
            ),
            ArtifactReference("report", report_path, ArtifactKind.REPORT),
            ArtifactReference("preview", preview_path, ArtifactKind.PREVIEW),
        )
    )
    manifest = publish_stage_manifest(
        stage=STAGE_ID.value,
        status=StageStatus.COMPLETED,
        output_directory=output_dir,
        report_path=report_path,
        preview_path=preview_path,
        input_state_fingerprint=fingerprint,
        artifacts=tuple(artifacts),
        metrics={
            "ground_point_count": len(ground_points),
            "building_point_count": len(building_points),
            "tree_point_count": len(tree_points),
            "unclassified_point_count": len(unclassified_points),
            "alignment_status": str(diagnostics["alignment_status"]),
        },
        details={
            "source_building_footprints": str(footprint_path),
            "crs": config.region.crs,
            "tree_filter": diagnostics["tree_filter"],
        },
    )

    return PointCloudStageOutput(
        manifest=manifest,
        projected_footprints_path=projected_footprints_path,
        ground_points_path=ground_path,
        building_points_path=building_path,
        tree_points_path=tree_path,
        unclassified_points_path=unclassified_path,
        diagnostics_path=diagnostics_path,
        ground_point_count=len(ground_points),
        building_point_count=len(building_points),
        tree_point_count=len(tree_points),
        unclassified_point_count=len(unclassified_points),
        alignment_status=str(diagnostics["alignment_status"]),
    )


def _validate_inputs(config: AppConfig) -> None:
    if config.region.crs.upper() != SUPPORTED_PROJECTED_CRS:
        raise ConfigError(
            f"point-cloud generation currently supports {SUPPORTED_PROJECTED_CRS}; got {config.region.crs}"
        )
    if config.inputs.point_cloud_path is not None:
        raise ConfigError(
            "City4CFD requires separate ground and building point clouds. "
            "The current config has only inputs.point_cloud_path, so use DTM/DSM inputs for this stage."
        )
    if config.inputs.dtm_directory is None or config.inputs.dsm_directory is None:
        raise ConfigError("point-cloud generation requires inputs.dtm_directory and inputs.dsm_directory")
    if not config.inputs.dtm_directory.exists():
        raise ConfigError(f"DTM directory does not exist: {config.inputs.dtm_directory}")
    if not config.inputs.dsm_directory.exists():
        raise ConfigError(f"DSM directory does not exist: {config.inputs.dsm_directory}")
    if config.inputs.tree_canopy_overlay_path is not None and not config.inputs.tree_canopy_overlay_path.exists():
        raise ConfigError(f"tree canopy overlay image does not exist: {config.inputs.tree_canopy_overlay_path}")


def _select_building_footprints_path(
    config: AppConfig,
    explicit_path: Path | None = None,
) -> Path:
    if explicit_path is not None:
        if not explicit_path.is_file():
            raise ConfigError(f"explicit building-footprint GeoJSON does not exist: {explicit_path}")
        return explicit_path
    stage1_manifest = require_completed_manifest(
        stage_output_directory(config.output.root_directory, StageId.SHAPEFILES) / "manifest.json",
        expected_stage=StageId.SHAPEFILES.value,
    )
    return require_manifest_artifact(
        stage1_manifest,
        name="category-buildings",
        kind=ArtifactKind.HANDOFF,
    ).path


def _select_optional_tree_features_path(config: AppConfig) -> Path | None:
    manifest_path = stage_output_directory(config.output.root_directory, StageId.SHAPEFILES) / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        stage1_manifest = require_completed_manifest(
            manifest_path,
            expected_stage=StageId.SHAPEFILES.value,
        )
        return require_manifest_artifact(
            stage1_manifest,
            name="category-trees",
            kind=ArtifactKind.HANDOFF,
        ).path
    except ConfigError:
        return None


def _project_feature(feature: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    projected = dict(feature)
    geometry = feature["geometry"]
    projected_geometry = dict(geometry)
    if geometry["type"] == "Polygon":
        projected_geometry["coordinates"] = [
            [list(point) for point in _project_ring(ring, config)] for ring in geometry["coordinates"]
        ]
    elif geometry["type"] == "MultiPolygon":
        projected_geometry["coordinates"] = [
            [[list(point) for point in _project_ring(ring, config)] for ring in polygon]
            for polygon in geometry["coordinates"]
        ]
    projected["geometry"] = projected_geometry
    properties = dict(feature.get("properties", {}))
    properties["source_crs"] = "EPSG:4326"
    properties["projected_crs"] = config.region.crs
    projected["properties"] = properties
    return projected


def _project_feature_polygon(feature: dict[str, Any]) -> list[ProjectedPolygon]:
    geometry = feature["geometry"]
    if geometry["type"] == "Polygon":
        polygon = _projected_polygon_from_coordinates(geometry["coordinates"])
        return [polygon] if polygon is not None else []
    if geometry["type"] == "MultiPolygon":
        return [
            projected
            for polygon in geometry["coordinates"]
            if (projected := _projected_polygon_from_coordinates(polygon)) is not None
        ]
    return []


def _projected_polygon_from_coordinates(coordinates: Any) -> ProjectedPolygon | None:
    if not isinstance(coordinates, list) or not coordinates:
        return None
    exterior = _coordinate_ring(coordinates[0])
    if len(exterior) < 4:
        return None
    holes = tuple(ring for raw_ring in coordinates[1:] if len(ring := _coordinate_ring(raw_ring)) >= 4)
    return ProjectedPolygon(exterior=exterior, holes=holes)


def _coordinate_ring(coordinates: Any) -> Ring:
    if not isinstance(coordinates, list):
        return ()
    points: list[Point2] = []
    for coordinate in coordinates:
        if not isinstance(coordinate, list) or len(coordinate) < 2:
            return ()
        points.append((float(coordinate[0]), float(coordinate[1])))
    return tuple(points)


def _project_ring(ring: list[list[float]], config: AppConfig) -> list[tuple[float, float]]:
    return [_lonlat_to_epsg25832(float(point[0]), float(point[1])) for point in ring]


def _region_bbox_projected(config: AppConfig) -> tuple[float, float, float, float]:
    center_x, center_y = _lonlat_to_epsg25832(config.region.center_lon, config.region.center_lat)
    radius = config.region.outer_diameter_m / 2.0
    return center_x - radius, center_y - radius, center_x + radius, center_y + radius


def _load_tree_canopy_mask(config: AppConfig) -> dict[str, Any] | None:
    path = config.inputs.tree_canopy_overlay_path
    if path is None:
        return None
    image = read_png_rgba(path)
    min_x, min_y, max_x, max_y = _region_bbox_projected(config)
    return {
        "path": str(path),
        "width": image["width"],
        "height": image["height"],
        "pixels": image["pixels"],
        "bbox": (min_x, min_y, max_x, max_y),
    }


def _load_projected_tree_tag_points(path: Path | None) -> list[tuple[float, float]]:
    if path is None:
        return []
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    features = data.get("features", [])
    if not isinstance(features, list):
        raise ValueError(f"GeoJSON feature collection missing features list: {path}")
    points: list[tuple[float, float]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties", {})
        tags = properties.get("tags", {}) if isinstance(properties, dict) else {}
        geometry = feature.get("geometry", {})
        if (
            isinstance(tags, dict)
            and tags.get("natural") == "tree"
            and isinstance(geometry, dict)
            and geometry.get("type") == "Point"
        ):
            coordinates = geometry.get("coordinates", [])
            if isinstance(coordinates, list) and len(coordinates) >= 2:
                points.append(_lonlat_to_epsg25832(float(coordinates[0]), float(coordinates[1])))
    return points


def _point_cloud_input_fingerprint(
    config: AppConfig,
    footprint_path: Path,
    tree_features_path: Path | None,
) -> dict[str, Any]:
    paths = [config.path, footprint_path]
    for directory in (config.inputs.dtm_directory, config.inputs.dsm_directory):
        if directory is not None:
            paths.extend(path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() == ".asc")
    if config.inputs.tree_canopy_overlay_path is not None:
        paths.append(config.inputs.tree_canopy_overlay_path)
        if tree_features_path is not None:
            paths.append(tree_features_path)
    return lightweight_state_fingerprint(
        {
            "stage": "point-cloud",
            "crs": config.region.crs,
            "center": [config.region.center_lon, config.region.center_lat],
            "outer_diameter_m": config.region.outer_diameter_m,
            "building_height_threshold_m": BUILDING_HEIGHT_THRESHOLD_M,
            "tree_filter_enabled": config.inputs.tree_canopy_overlay_path is not None,
        },
        paths,
    )


def _write_ply(path: Path, points: list[tuple[float, float, float]]) -> None:
    lines = [
        "ply",
        "format ascii 1.0",
        f"element vertex {len(points)}",
        "property double x",
        "property double y",
        "property double z",
        "end_header",
    ]
    with atomic_text_writer(path) as handle:
        handle.write("\n".join(lines) + "\n")
        handle.writelines(f"{x:.3f} {y:.3f} {z:.3f}\n" for x, y, z in points)


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


def _lonlat_to_epsg25832(lon: float, lat: float) -> tuple[float, float]:
    """Project WGS84 lon/lat to ETRS89 / UTM zone 32N.

    The project CRS default is EPSG:25832. For the Florence-scale QA outputs,
    ETRS89 and WGS84 differences are below the precision needed here.
    """

    semi_major = 6378137.0
    flattening = 1.0 / 298.257223563
    eccentricity_sq = flattening * (2.0 - flattening)
    second_eccentricity_sq = eccentricity_sq / (1.0 - eccentricity_sq)
    scale = 0.9996
    central_meridian = math.radians(9.0)
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)

    n = semi_major / math.sqrt(1.0 - eccentricity_sq * math.sin(lat_rad) ** 2)
    t = math.tan(lat_rad) ** 2
    c = second_eccentricity_sq * math.cos(lat_rad) ** 2
    a = math.cos(lat_rad) * (lon_rad - central_meridian)
    m = semi_major * (
        (1 - eccentricity_sq / 4 - 3 * eccentricity_sq**2 / 64 - 5 * eccentricity_sq**3 / 256) * lat_rad
        - (3 * eccentricity_sq / 8 + 3 * eccentricity_sq**2 / 32 + 45 * eccentricity_sq**3 / 1024)
        * math.sin(2 * lat_rad)
        + (15 * eccentricity_sq**2 / 256 + 45 * eccentricity_sq**3 / 1024) * math.sin(4 * lat_rad)
        - (35 * eccentricity_sq**3 / 3072) * math.sin(6 * lat_rad)
    )
    easting = (
        scale
        * n
        * (a + (1 - t + c) * a**3 / 6 + (5 - 18 * t + t**2 + 72 * c - 58 * second_eccentricity_sq) * a**5 / 120)
        + 500000.0
    )
    northing = scale * (
        m
        + n
        * math.tan(lat_rad)
        * (
            a**2 / 2
            + (5 - t + 9 * c + 4 * c**2) * a**4 / 24
            + (61 - 58 * t + t**2 + 600 * c - 330 * second_eccentricity_sq) * a**6 / 720
        )
    )
    return easting, northing
