"""Point-cloud preparation for City4CFD.

City4CFD needs separate ground and building point clouds. This module creates
those PLY files from paired DTM/DSM ASCII grids, preserves DSM points that are
not classified as buildings or trees, and records the footprint alignment
checks that should be reviewed before reconstruction.
"""

from __future__ import annotations

import json
import math
import struct
import zlib
from collections.abc import Iterable
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
from .rendering import render_preview_html
from .reporting import render_report

NODATA_DEFAULT = -9999.0
BUILDING_HEIGHT_THRESHOLD_M = 2.0
TREE_HEIGHT_THRESHOLD_M = 1.5
TREE_LOCAL_RELIEF_THRESHOLD_M = 3.0
TREE_LOCAL_RELIEF_RADIUS_M = 4.0
TREE_ROOF_OFFSET_THRESHOLD_M = 4.0
TREE_ROOF_SEARCH_RADIUS_M = 8.0
TREE_ROOF_CLUSTER_RADIUS_M = 3.0
TREE_ROOF_CLUSTER_Z_TOLERANCE_M = 1.5
TREE_BUILDING_FOOTPRINT_BUFFER_M = 1.5
TREE_TAG_ASSOCIATION_RADIUS_M = 8.0
TREE_CANOPY_MASK_SEARCH_RADIUS_PX = 1
TREE_EXCESS_GREEN_THRESHOLD = 8
TREE_MIN_GREEN_CHANNEL = 60
ALIGNMENT_SEARCH_RADIUS_M = 6
ALIGNMENT_SEARCH_STEP_M = 1
ALIGNMENT_WARN_SHIFT_M = 2.0
ALIGNMENT_FAIL_SHIFT_M = 5.0
SUPPORTED_PROJECTED_CRS = "EPSG:25832"
Point2 = tuple[float, float]
Ring = tuple[Point2, ...]


@dataclass(frozen=True)
class RasterTile:
    path: Path
    ncols: int
    nrows: int
    xllcorner: float
    yllcorner: float
    cellsize: float
    nodata_value: float
    values_offset: int

    @property
    def max_x(self) -> float:
        return self.xllcorner + self.ncols * self.cellsize

    @property
    def max_y(self) -> float:
        return self.yllcorner + self.nrows * self.cellsize


@dataclass(frozen=True)
class ProjectedPolygon:
    exterior: Ring
    holes: tuple[Ring, ...] = ()

    @property
    def rings(self) -> tuple[Ring, ...]:
        return (self.exterior, *self.holes)


@dataclass(frozen=True)
class PolygonSpatialIndex:
    """Coarse polygon lookup that preserves the existing exact geometry tests."""

    polygons: tuple[ProjectedPolygon, ...]
    bounding_boxes: tuple[tuple[float, float, float, float], ...]
    cells: dict[tuple[int, int], tuple[int, ...]]
    cell_size: float
    buffer_m: float

    @classmethod
    def build(
        cls,
        polygons: list[ProjectedPolygon],
        *,
        buffer_m: float = 0.0,
        cell_size: float = 32.0,
    ) -> PolygonSpatialIndex:
        bounding_boxes = tuple(_ring_bbox(polygon.exterior) for polygon in polygons)
        mutable_cells: dict[tuple[int, int], list[int]] = {}
        for polygon_index, (min_x, min_y, max_x, max_y) in enumerate(bounding_boxes):
            if not polygons[polygon_index].exterior:
                continue
            min_cell_x = math.floor((min_x - buffer_m) / cell_size)
            max_cell_x = math.floor((max_x + buffer_m) / cell_size)
            min_cell_y = math.floor((min_y - buffer_m) / cell_size)
            max_cell_y = math.floor((max_y + buffer_m) / cell_size)
            for cell_x in range(min_cell_x, max_cell_x + 1):
                for cell_y in range(min_cell_y, max_cell_y + 1):
                    mutable_cells.setdefault((cell_x, cell_y), []).append(polygon_index)
        return cls(
            polygons=tuple(polygons),
            bounding_boxes=bounding_boxes,
            cells={key: tuple(indices) for key, indices in mutable_cells.items()},
            cell_size=cell_size,
            buffer_m=buffer_m,
        )

    def candidate_indices(self, point: tuple[float, float]) -> tuple[int, ...]:
        x, y = point
        return self.cells.get((math.floor(x / self.cell_size), math.floor(y / self.cell_size)), ())

    def contains(self, point: tuple[float, float]) -> bool:
        x, y = point
        for polygon_index in self.candidate_indices(point):
            min_x, min_y, max_x, max_y = self.bounding_boxes[polygon_index]
            if min_x <= x <= max_x and min_y <= y <= max_y:
                if _point_in_projected_polygon(point, self.polygons[polygon_index]):
                    return True
        return False

    def within_buffer(self, point: tuple[float, float], buffer_m: float) -> bool:
        if buffer_m <= 0.0:
            return False
        if buffer_m > self.buffer_m:
            raise ValueError(f"polygon index supports buffers up to {self.buffer_m:g} m; got {buffer_m:g} m")
        x, y = point
        for polygon_index in self.candidate_indices(point):
            min_x, min_y, max_x, max_y = self.bounding_boxes[polygon_index]
            if not (
                min_x - buffer_m <= x <= max_x + buffer_m
                and min_y - buffer_m <= y <= max_y + buffer_m
            ):
                continue
            if _point_to_projected_polygon_boundary_m(point, self.polygons[polygon_index]) <= buffer_m:
                return True
        return False


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
        footprints = _read_feature_collection(footprint_path)
    except OSError as exc:
        raise ConfigError(f"cannot read selected building footprints: {footprint_path}: {exc}") from exc
    projected_features = [_project_feature(feature, config) for feature in footprints]
    projected_footprints = [
        polygon
        for feature in projected_features
        for polygon in _project_feature_polygon(feature)
    ]
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
    ) = _points_from_rasters(
        dtm_directory=config.inputs.dtm_directory,
        dsm_directory=config.inputs.dsm_directory,
        bbox=bbox,
        building_polygons=projected_footprints,
        tree_mask=tree_mask,
        tree_tag_points=tree_tag_points,
    )
    diagnostics = _build_alignment_diagnostics(
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
            f"point-cloud generation currently supports {SUPPORTED_PROJECTED_CRS}; "
            f"got {config.region.crs}"
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


def _read_feature_collection(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError(f"GeoJSON feature collection missing features list: {path}")
    return [
        feature for feature in features
        if (
            isinstance(feature, dict)
            and feature.get("geometry", {}).get("type") in {"Polygon", "MultiPolygon"}
            and feature.get("properties", {}).get("contributes_to_geometry", True)
            and feature.get("properties", {}).get("include_in_building_lod22_reconstruction", True)
        )
    ]


def _project_feature(feature: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    projected = dict(feature)
    geometry = feature["geometry"]
    projected_geometry = dict(geometry)
    if geometry["type"] == "Polygon":
        projected_geometry["coordinates"] = [[list(point) for point in _project_ring(ring, config)] for ring in geometry["coordinates"]]
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
    holes = tuple(
        ring
        for raw_ring in coordinates[1:]
        if len(ring := _coordinate_ring(raw_ring)) >= 4
    )
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


def _points_from_rasters(
    dtm_directory: Path | None,
    dsm_directory: Path | None,
    bbox: tuple[float, float, float, float],
    building_polygons: list[ProjectedPolygon],
    tree_mask: dict[str, Any] | None,
    tree_tag_points: list[tuple[float, float]],
) -> tuple[
    list[tuple[float, float, float]],
    list[tuple[float, float, float]],
    list[tuple[float, float, float]],
    list[tuple[float, float, float]],
    list[tuple[float, float, float]],
    dict[str, Any],
]:
    assert dtm_directory is not None
    assert dsm_directory is not None
    dtm_tiles = _tiles_by_name(dtm_directory)
    dsm_tiles = _tiles_by_name(dsm_directory)
    paired_names = sorted(set(dtm_tiles) & set(dsm_tiles))
    _validate_tile_pairing(dtm_tiles, dsm_tiles, paired_names, bbox)
    if not paired_names:
        raise ConfigError("no paired DTM/DSM ASCII tiles were found")

    ground_points: list[tuple[float, float, float]] = []
    building_points: list[tuple[float, float, float]] = []
    tree_points: list[tuple[float, float, float]] = []
    unclassified_points: list[tuple[float, float, float]] = []
    alignment_candidate_points: list[tuple[float, float, float]] = []
    used_tiles: list[str] = []
    skipped_tiles = 0
    tree_filter_counts = {
        "evidence_candidate_count": 0,
        "tree_tag_supported_candidate_count": 0,
        "building_footprint_candidate_count": 0,
        "building_footprint_buffer_candidate_count": 0,
        "building_footprint_without_roof_estimate_count": 0,
        "roof_estimate_candidate_count": 0,
        "roof_offset_pass_count": 0,
        "local_relief_fallback_candidate_count": 0,
        "local_relief_pass_count": 0,
    }
    min_x, min_y, max_x, max_y = bbox
    polygon_index = PolygonSpatialIndex.build(
        building_polygons,
        buffer_m=TREE_BUILDING_FOOTPRINT_BUFFER_M if tree_mask is not None else 0.0,
    )

    for name in paired_names:
        dtm_tile = dtm_tiles[name]
        dsm_tile = dsm_tiles[name]
        if not _tile_intersects_bbox(dtm_tile, bbox):
            skipped_tiles += 1
            continue
        used_tiles.append(name)
        dtm_rows = _read_ascii_grid_values(dtm_tile)
        dsm_rows = _read_ascii_grid_values(dsm_tile)
        local_radius_cells = max(1, math.ceil(TREE_LOCAL_RELIEF_RADIUS_M / dsm_tile.cellsize))
        roof_index = (
            _building_roof_point_index(dtm_tile, dsm_tile, dtm_rows, dsm_rows, bbox, polygon_index)
            if tree_mask is not None
            else {}
        )
        for x, y, ground_z, surface_z, row_index, col_index in _paired_tile_cells(dtm_tile, dsm_tile, dtm_rows, dsm_rows):
            if not (min_x <= x <= max_x and min_y <= y <= max_y):
                continue
            ground_points.append((x, y, ground_z))
            height_above_ground = surface_z - ground_z
            if height_above_ground >= BUILDING_HEIGHT_THRESHOLD_M:
                alignment_candidate_points.append((x, y, surface_z))
            inside_building_footprint = polygon_index.contains((x, y))
            in_building_roof_validation_zone = inside_building_footprint
            if tree_mask is not None and not in_building_roof_validation_zone:
                in_building_roof_validation_zone = polygon_index.within_buffer(
                    (x, y),
                    TREE_BUILDING_FOOTPRINT_BUFFER_M,
                )
            is_tree_candidate = False
            if (
                tree_mask is not None
                and height_above_ground >= TREE_HEIGHT_THRESHOLD_M
            ):
                has_tree_tag_evidence = _point_has_tree_tag_association((x, y), tree_tag_points)
                has_vegetation_evidence = _point_matches_tree_canopy_mask(x, y, tree_mask)
                if has_vegetation_evidence or has_tree_tag_evidence:
                    tree_filter_counts["evidence_candidate_count"] += 1
                    if has_tree_tag_evidence:
                        tree_filter_counts["tree_tag_supported_candidate_count"] += 1
                    if in_building_roof_validation_zone:
                        tree_filter_counts["building_footprint_candidate_count"] += 1
                        if not inside_building_footprint:
                            tree_filter_counts["building_footprint_buffer_candidate_count"] += 1
                        roof_z = _estimate_nearby_roof_z(x, y, surface_z, roof_index)
                        if roof_z is None:
                            tree_filter_counts["building_footprint_without_roof_estimate_count"] += 1
                        else:
                            tree_filter_counts["roof_estimate_candidate_count"] += 1
                            is_tree_candidate = abs(surface_z - roof_z) >= TREE_ROOF_OFFSET_THRESHOLD_M
                            if is_tree_candidate:
                                tree_filter_counts["roof_offset_pass_count"] += 1
                    else:
                        tree_filter_counts["local_relief_fallback_candidate_count"] += 1
                        local_relief_z = _local_surface_relief(
                            dsm_rows,
                            row_index,
                            col_index,
                            local_radius_cells,
                            dsm_tile.nodata_value,
                        )
                        is_tree_candidate = local_relief_z >= TREE_LOCAL_RELIEF_THRESHOLD_M
                        if is_tree_candidate:
                            tree_filter_counts["local_relief_pass_count"] += 1
            if is_tree_candidate:
                tree_points.append((x, y, surface_z))
            elif height_above_ground >= BUILDING_HEIGHT_THRESHOLD_M and inside_building_footprint:
                building_points.append((x, y, surface_z))
            else:
                unclassified_points.append((x, y, surface_z))

    if not ground_points:
        raise ConfigError("no DTM ground points intersect the configured outer region")
    return ground_points, building_points, tree_points, unclassified_points, alignment_candidate_points, {
        "paired_tile_count": len(paired_names),
        "used_tile_count": len(used_tiles),
        "skipped_tile_count": skipped_tiles,
        "used_tiles": used_tiles,
        "tree_filter_counts": tree_filter_counts,
    }


def _load_tree_canopy_mask(config: AppConfig) -> dict[str, Any] | None:
    path = config.inputs.tree_canopy_overlay_path
    if path is None:
        return None
    image = _read_png_rgba(path)
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


def _point_has_tree_tag_association(point: tuple[float, float], tree_tag_points: list[tuple[float, float]]) -> bool:
    radius_sq = TREE_TAG_ASSOCIATION_RADIUS_M**2
    x, y = point
    return any((tree_x - x) ** 2 + (tree_y - y) ** 2 <= radius_sq for tree_x, tree_y in tree_tag_points)


def _point_matches_tree_canopy_mask(x: float, y: float, tree_mask: dict[str, Any]) -> bool:
    min_x, min_y, max_x, max_y = tree_mask["bbox"]
    width = int(tree_mask["width"])
    height = int(tree_mask["height"])
    if not (min_x <= x <= max_x and min_y <= y <= max_y):
        return False
    column = min(width - 1, max(0, int((x - min_x) / (max_x - min_x) * width)))
    row = min(height - 1, max(0, int((max_y - y) / (max_y - min_y) * height)))
    radius_px = TREE_CANOPY_MASK_SEARCH_RADIUS_PX
    for nearby_row in range(max(0, row - radius_px), min(height - 1, row + radius_px) + 1):
        for nearby_column in range(max(0, column - radius_px), min(width - 1, column + radius_px) + 1):
            red, green, blue, alpha = tree_mask["pixels"][nearby_row * width + nearby_column]
            excess_green = 2 * green - red - blue
            if alpha >= 16 and green >= TREE_MIN_GREEN_CHANNEL and excess_green >= TREE_EXCESS_GREEN_THRESHOLD:
                return True
    return False


def _building_roof_point_index(
    dtm_tile: RasterTile,
    dsm_tile: RasterTile,
    dtm_rows: list[list[float]],
    dsm_rows: list[list[float]],
    bbox: tuple[float, float, float, float],
    polygon_index: PolygonSpatialIndex,
) -> dict[tuple[int, int], list[tuple[float, float, float]]]:
    index: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    min_x, min_y, max_x, max_y = bbox
    for x, y, ground_z, surface_z, _row_index, _col_index in _paired_tile_cells(dtm_tile, dsm_tile, dtm_rows, dsm_rows):
        if not (min_x <= x <= max_x and min_y <= y <= max_y):
            continue
        if surface_z - ground_z < BUILDING_HEIGHT_THRESHOLD_M:
            continue
        if not polygon_index.contains((x, y)):
            continue
        key = _roof_index_key(x, y)
        index.setdefault(key, []).append((x, y, surface_z))
    return index


def _estimate_nearby_roof_z(
    x: float,
    y: float,
    surface_z: float,
    roof_index: dict[tuple[int, int], list[tuple[float, float, float]]],
) -> float | None:
    if not roof_index:
        return None
    radius_sq = TREE_ROOF_SEARCH_RADIUS_M**2
    key_x, key_y = _roof_index_key(x, y)
    nearby_points: list[tuple[float, float, float, float]] = []
    for delta_x in (-1, 0, 1):
        for delta_y in (-1, 0, 1):
            for point_x, point_y, roof_z in roof_index.get((key_x + delta_x, key_y + delta_y), []):
                distance_sq = (point_x - x) ** 2 + (point_y - y) ** 2
                if 0.0 < distance_sq <= radius_sq:
                    nearby_points.append((point_x, point_y, roof_z, distance_sq))
    if not nearby_points:
        return None
    return _select_roof_cluster_z(nearby_points, surface_z)


def _select_roof_cluster_z(
    points: list[tuple[float, float, float, float]],
    surface_z: float,
) -> float:
    """Estimate roof Z from a spatial/Z-contiguous roof cluster.

    Prefer a cluster close to the candidate Z so a coherent roof patch is not
    compared against a nearby but different roof level.
    """

    unvisited = set(range(len(points)))
    clusters: list[list[int]] = []
    cluster_radius_sq = TREE_ROOF_CLUSTER_RADIUS_M**2
    while unvisited:
        start = min(unvisited, key=lambda index: points[index][3])
        unvisited.remove(start)
        cluster = [start]
        queue = [start]
        while queue:
            current = queue.pop()
            current_x, current_y, current_z, _distance_sq = points[current]
            for other in list(unvisited):
                other_x, other_y, other_z, _other_distance_sq = points[other]
                if (other_x - current_x) ** 2 + (other_y - current_y) ** 2 > cluster_radius_sq:
                    continue
                if abs(other_z - current_z) > TREE_ROOF_CLUSTER_Z_TOLERANCE_M:
                    continue
                unvisited.remove(other)
                queue.append(other)
                cluster.append(other)
        clusters.append(cluster)

    cluster_summaries: list[tuple[float, float]] = []
    for cluster in clusters:
        values = sorted(points[index][2] for index in cluster)
        roof_z = values[(len(values) - 1) // 2]
        nearest_distance_sq = min(points[index][3] for index in cluster)
        cluster_summaries.append((roof_z, nearest_distance_sq))

    candidate_roof_clusters = [
        summary for summary in cluster_summaries
        if summary[0] <= surface_z + TREE_ROOF_CLUSTER_Z_TOLERANCE_M
    ]
    close_z_clusters = [
        summary for summary in candidate_roof_clusters
        if abs(surface_z - summary[0]) < TREE_ROOF_OFFSET_THRESHOLD_M
    ]
    if close_z_clusters:
        return min(close_z_clusters, key=lambda summary: summary[1])[0]
    if candidate_roof_clusters:
        return min(candidate_roof_clusters, key=lambda summary: summary[1])[0]
    return min(cluster_summaries, key=lambda summary: summary[1])[0]


def _roof_index_key(x: float, y: float) -> tuple[int, int]:
    return math.floor(x / TREE_ROOF_SEARCH_RADIUS_M), math.floor(y / TREE_ROOF_SEARCH_RADIUS_M)


def _read_png_rgba(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        raise ConfigError(f"tree canopy overlay must be a PNG image: {path}")
    offset = len(signature)
    width = 0
    height = 0
    color_type = -1
    bit_depth = -1
    compressed = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _compression, _filter, interlace = struct.unpack(">IIBBBBB", chunk)
            if bit_depth != 8 or color_type not in {2, 6} or interlace != 0:
                raise ConfigError("tree canopy overlay PNG must be 8-bit, non-interlaced RGB or RGBA")
        elif chunk_type == b"IDAT":
            compressed.extend(chunk)
        elif chunk_type == b"IEND":
            break
    if width <= 0 or height <= 0 or not compressed:
        raise ConfigError(f"invalid PNG tree canopy overlay: {path}")
    channels = 4 if color_type == 6 else 3
    stride = width * channels
    raw = zlib.decompress(bytes(compressed))
    rows: list[bytes] = []
    previous = bytes(stride)
    cursor = 0
    for _row in range(height):
        filter_type = raw[cursor]
        cursor += 1
        scanline = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        _unfilter_png_scanline(scanline, previous, channels, filter_type)
        rows.append(bytes(scanline))
        previous = rows[-1]
    pixels: list[tuple[int, int, int, int]] = []
    for row in rows:
        for index in range(0, len(row), channels):
            red = row[index]
            green = row[index + 1]
            blue = row[index + 2]
            alpha = row[index + 3] if channels == 4 else 255
            pixels.append((red, green, blue, alpha))
    return {"width": width, "height": height, "pixels": pixels}


def _unfilter_png_scanline(scanline: bytearray, previous: bytes, channels: int, filter_type: int) -> None:
    for index, value in enumerate(scanline):
        left = scanline[index - channels] if index >= channels else 0
        up = previous[index]
        up_left = previous[index - channels] if index >= channels else 0
        if filter_type == 0:
            reconstructed = value
        elif filter_type == 1:
            reconstructed = value + left
        elif filter_type == 2:
            reconstructed = value + up
        elif filter_type == 3:
            reconstructed = value + ((left + up) // 2)
        elif filter_type == 4:
            reconstructed = value + _paeth_predictor(left, up, up_left)
        else:
            raise ConfigError(f"unsupported PNG filter type: {filter_type}")
        scanline[index] = reconstructed & 0xFF


def _paeth_predictor(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    distance_left = abs(estimate - left)
    distance_up = abs(estimate - up)
    distance_up_left = abs(estimate - up_left)
    if distance_left <= distance_up and distance_left <= distance_up_left:
        return left
    if distance_up <= distance_up_left:
        return up
    return up_left


def _tiles_by_name(directory: Path) -> dict[str, RasterTile]:
    tiles: dict[str, RasterTile] = {}
    normalized_paths: dict[str, Path] = {}
    paths = sorted(path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() == ".asc")
    for path in paths:
        normalized_name = path.name.casefold()
        previous_path = normalized_paths.get(normalized_name)
        if previous_path is not None:
            raise ConfigError(
                "duplicate ASCII grid basename in "
                f"{directory}: {previous_path} and {path}"
            )
        normalized_paths[normalized_name] = path
        tiles[path.name] = _read_ascii_grid_header(path)
    return tiles


def _validate_tile_pairing(
    dtm_tiles: dict[str, RasterTile],
    dsm_tiles: dict[str, RasterTile],
    paired_names: list[str],
    bbox: tuple[float, float, float, float],
) -> None:
    for name in paired_names:
        dtm_tile = dtm_tiles[name]
        dsm_tile = dsm_tiles[name]
        dtm_geometry = (
            dtm_tile.ncols,
            dtm_tile.nrows,
            dtm_tile.cellsize,
            dtm_tile.xllcorner,
            dtm_tile.yllcorner,
        )
        dsm_geometry = (
            dsm_tile.ncols,
            dsm_tile.nrows,
            dsm_tile.cellsize,
            dsm_tile.xllcorner,
            dsm_tile.yllcorner,
        )
        if dtm_geometry != dsm_geometry:
            raise ConfigError(
                f"DTM/DSM tile grid mismatch for {name}: {dtm_tile.path} and {dsm_tile.path}"
            )

    unmatched_tiles = (
        *(("DTM", dtm_tiles[name]) for name in sorted(set(dtm_tiles) - set(dsm_tiles))),
        *(("DSM", dsm_tiles[name]) for name in sorted(set(dsm_tiles) - set(dtm_tiles))),
    )
    for source, tile in unmatched_tiles:
        if _tile_intersects_bbox(tile, bbox):
            raise ConfigError(
                f"unmatched {source} tile intersects the configured region: {tile.path}"
            )


def _read_ascii_grid_header(path: Path) -> RasterTile:
    header: dict[str, float] = {}
    values_offset = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for _ in range(6):
            line = handle.readline()
            values_offset = handle.tell()
            if not line:
                break
            parts = line.strip().split()
            if len(parts) >= 2:
                header[parts[0].lower()] = float(parts[1])
    required = ("ncols", "nrows", "cellsize")
    missing = [key for key in required if key not in header]
    if missing:
        raise ConfigError(f"ASCII grid {path} missing header keys: {', '.join(missing)}")
    cellsize = header["cellsize"]
    if "xllcorner" in header:
        xllcorner = header["xllcorner"]
    elif "xllcenter" in header:
        xllcorner = header["xllcenter"] - (cellsize / 2.0)
    else:
        raise ConfigError(f"ASCII grid {path} missing xllcorner or xllcenter header key")
    if "yllcorner" in header:
        yllcorner = header["yllcorner"]
    elif "yllcenter" in header:
        yllcorner = header["yllcenter"] - (cellsize / 2.0)
    else:
        raise ConfigError(f"ASCII grid {path} missing yllcorner or yllcenter header key")
    return RasterTile(
        path=path,
        ncols=int(header["ncols"]),
        nrows=int(header["nrows"]),
        xllcorner=xllcorner,
        yllcorner=yllcorner,
        cellsize=cellsize,
        nodata_value=header.get("nodata_value", NODATA_DEFAULT),
        values_offset=values_offset,
    )


def _tile_intersects_bbox(tile: RasterTile, bbox: tuple[float, float, float, float]) -> bool:
    min_x, min_y, max_x, max_y = bbox
    return not (tile.max_x < min_x or tile.xllcorner > max_x or tile.max_y < min_y or tile.yllcorner > max_y)


def _paired_tile_cells(
    dtm_tile: RasterTile,
    dsm_tile: RasterTile,
    dtm_rows: list[list[float]],
    dsm_rows: list[list[float]],
) -> Iterable[tuple[float, float, float, float, int, int]]:
    for row_index, dtm_values in enumerate(dtm_rows):
        dsm_values = dsm_rows[row_index]
        if len(dtm_values) != dtm_tile.ncols or len(dsm_values) != dsm_tile.ncols:
            raise ConfigError(f"unexpected row width while reading {dtm_tile.path.name}")
        y = dtm_tile.yllcorner + (dtm_tile.nrows - row_index - 0.5) * dtm_tile.cellsize
        for col_index, ground_z in enumerate(dtm_values):
            surface_z = dsm_values[col_index]
            if ground_z == dtm_tile.nodata_value or surface_z == dsm_tile.nodata_value:
                continue
            x = dtm_tile.xllcorner + (col_index + 0.5) * dtm_tile.cellsize
            yield x, y, ground_z, surface_z, row_index, col_index


def _read_ascii_grid_values(tile: RasterTile) -> list[list[float]]:
    rows: list[list[float]] = []
    with tile.path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(tile.values_offset)
        for _row_index in range(tile.nrows):
            rows.append(_parse_grid_row(handle.readline()))
    return rows


def _local_surface_relief(
    rows: list[list[float]],
    row_index: int,
    col_index: int,
    radius_cells: int,
    nodata_value: float,
) -> float:
    surface_z = rows[row_index][col_index]
    min_neighbor_z = math.inf
    min_row = max(0, row_index - radius_cells)
    max_row = min(len(rows) - 1, row_index + radius_cells)
    for neighbor_row_index in range(min_row, max_row + 1):
        row = rows[neighbor_row_index]
        min_col = max(0, col_index - radius_cells)
        max_col = min(len(row) - 1, col_index + radius_cells)
        for neighbor_col_index in range(min_col, max_col + 1):
            if neighbor_row_index == row_index and neighbor_col_index == col_index:
                continue
            neighbor_z = row[neighbor_col_index]
            if neighbor_z == nodata_value:
                continue
            min_neighbor_z = min(min_neighbor_z, neighbor_z)
    if min_neighbor_z == math.inf:
        return 0.0
    return max(0.0, surface_z - min_neighbor_z)


def _parse_grid_row(line: str) -> list[float]:
    return [float(value) for value in line.split()]


def _build_alignment_diagnostics(
    config: AppConfig,
    footprint_path: Path,
    building_polygons: list[ProjectedPolygon],
    ground_points: list[tuple[float, float, float]],
    building_points: list[tuple[float, float, float]],
    alignment_candidate_points: list[tuple[float, float, float]],
    tree_points: list[tuple[float, float, float]],
    unclassified_points: list[tuple[float, float, float]],
    raster_summary: dict[str, Any],
    tree_mask: dict[str, Any] | None,
    tree_tag_points: list[tuple[float, float]],
) -> dict[str, Any]:
    best_offset, score = _estimate_horizontal_offset(alignment_candidate_points, building_polygons)
    shift_m = round(math.hypot(best_offset[0], best_offset[1]), 3)
    if not building_polygons:
        status = "failed"
        message = "no building footprint polygons were available"
    elif not alignment_candidate_points:
        status = "warning"
        message = "no elevated DSM points were available for alignment review; check the rasters and ROI"
    elif score <= 0:
        status = "warning"
        message = "no elevated DSM points overlapped building footprints within the alignment search radius"
    elif shift_m > ALIGNMENT_FAIL_SHIFT_M:
        status = "failed"
        message = "estimated horizontal footprint/point-cloud shift exceeds the failure tolerance"
    elif shift_m > ALIGNMENT_WARN_SHIFT_M:
        status = "warning"
        message = "estimated horizontal footprint/point-cloud shift exceeds the review tolerance"
    else:
        status = "passed"
        message = "footprint and point-cloud alignment is within the configured tolerance"

    return {
        "alignment_status": status,
        "message": message,
        "crs": {
            "target": config.region.crs,
            "footprint_source": "EPSG:4326 GeoJSON coordinates projected to EPSG:25832",
            "dtm_dsm_source": config.region.crs,
            "same_metric_output_crs": config.region.crs.upper() == SUPPORTED_PROJECTED_CRS,
        },
        "footprint_path": str(footprint_path),
        "footprint_polygon_count": len(building_polygons),
        "ground_point_count": len(ground_points),
        "building_point_count": len(building_points),
        "alignment_candidate_point_count": len(alignment_candidate_points),
        "alignment_evidence": "in-ROI DSM cells at least 2 m above DTM, collected before footprint and tree classification",
        "tree_point_count": len(tree_points),
        "unclassified_point_count": len(unclassified_points),
        "dsm_classification_complete": len(ground_points) == (
            len(building_points) + len(tree_points) + len(unclassified_points)
        ),
        "building_height_threshold_m": BUILDING_HEIGHT_THRESHOLD_M,
        "tree_height_threshold_m": TREE_HEIGHT_THRESHOLD_M,
        "tree_local_relief_threshold_m": TREE_LOCAL_RELIEF_THRESHOLD_M,
        "tree_local_relief_radius_m": TREE_LOCAL_RELIEF_RADIUS_M,
        "tree_roof_offset_threshold_m": TREE_ROOF_OFFSET_THRESHOLD_M,
        "tree_roof_search_radius_m": TREE_ROOF_SEARCH_RADIUS_M,
        "tree_roof_cluster_radius_m": TREE_ROOF_CLUSTER_RADIUS_M,
        "tree_roof_cluster_z_tolerance_m": TREE_ROOF_CLUSTER_Z_TOLERANCE_M,
        "tree_building_footprint_buffer_m": TREE_BUILDING_FOOTPRINT_BUFFER_M,
        "estimated_horizontal_shift_m": shift_m,
        "best_offset_m": {"x": best_offset[0], "y": best_offset[1]},
        "best_offset_inside_point_count": score,
        "alignment_tolerances_m": {
            "warning": ALIGNMENT_WARN_SHIFT_M,
            "failure": ALIGNMENT_FAIL_SHIFT_M,
        },
        "raster_summary": raster_summary,
        "tree_filter": {
            "enabled": tree_mask is not None,
            "overlay_path": tree_mask["path"] if tree_mask is not None else None,
            "tree_tag_point_count": len(tree_tag_points),
            "tag_association_radius_m": TREE_TAG_ASSOCIATION_RADIUS_M,
            "roof_offset_threshold_m": TREE_ROOF_OFFSET_THRESHOLD_M,
            "roof_search_radius_m": TREE_ROOF_SEARCH_RADIUS_M,
            "roof_cluster_radius_m": TREE_ROOF_CLUSTER_RADIUS_M,
            "roof_cluster_z_tolerance_m": TREE_ROOF_CLUSTER_Z_TOLERANCE_M,
            "building_footprint_buffer_m": TREE_BUILDING_FOOTPRINT_BUFFER_M,
            "local_relief_threshold_m": TREE_LOCAL_RELIEF_THRESHOLD_M,
            "local_relief_radius_m": TREE_LOCAL_RELIEF_RADIUS_M,
            "canopy_mask_search_radius_px": TREE_CANOPY_MASK_SEARCH_RADIUS_PX,
            "excess_green_threshold": TREE_EXCESS_GREEN_THRESHOLD,
            "min_green_channel": TREE_MIN_GREEN_CHANNEL,
            "counts": raster_summary.get("tree_filter_counts", {}),
            "policy": (
                "DSM cells first need candidate evidence from vegetation-colored overlay pixels or nearby stage-1 "
                "natural=tree tags. If the candidate is inside or within the configured buffer around a building footprint, it enters the tree cloud only "
                "when nearby roof DSM points can be estimated. Roof estimation clusters nearby samples by spatial/Z continuity, "
                "prefers a cluster close to the candidate surface Z when one exists, otherwise falls back to the nearest lower "
                "roof cluster, then requires the candidate surface Z to differ from that cluster Z by at least the configured "
                "roof offset. The local-relief fallback is used only for candidates outside the buffered building footprint zone."
            ),
        },
        "assumptions": [
        "City4CFD requires separate ground and building point clouds.",
        "Footprint coordinates are interpreted as EPSG:4326 lon/lat and projected to EPSG:25832.",
        "DSM points are assigned to the building cloud only when they are at least 2 m above DTM and inside a building footprint.",
        "Optional tree DSM points require vegetation-colored overlay evidence or nearby stage-1 natural=tree tags plus a Z test. Inside or near building footprints, the Z test must be roof-relative; local DSM relief is used only outside the buffered building footprint zone.",
        "Every valid paired DSM point is classified exactly once as building, tree, or unclassified, so ground point count equals building plus tree plus unclassified point count.",
        "The horizontal shift estimate is a deterministic grid search that maximizes raw elevated DSM candidates inside shifted footprints. Candidates are collected before footprint and tree classification, so trees and other elevated objects can be present in the diagnostic evidence.",
        "The preview preserves the same meter-scale height differences as the exported PLY files and does not exaggerate vertical scale.",
    ],
    }


def _estimate_horizontal_offset(
    building_points: list[tuple[float, float, float]],
    building_polygons: list[ProjectedPolygon],
) -> tuple[tuple[int, int], int]:
    if not building_points or not building_polygons:
        return (0, 0), 0
    sample_points = building_points[:: max(1, len(building_points) // 2000)]
    polygon_index = PolygonSpatialIndex.build(building_polygons)
    best_offset = (0, 0)
    best_score = -1
    for dx in range(-ALIGNMENT_SEARCH_RADIUS_M, ALIGNMENT_SEARCH_RADIUS_M + 1, ALIGNMENT_SEARCH_STEP_M):
        for dy in range(-ALIGNMENT_SEARCH_RADIUS_M, ALIGNMENT_SEARCH_RADIUS_M + 1, ALIGNMENT_SEARCH_STEP_M):
            shifted = [(x - dx, y - dy, z) for x, y, z in sample_points]
            score = sum(1 for x, y, _z in shifted if polygon_index.contains((x, y)))
            if score > best_score or (score == best_score and math.hypot(dx, dy) < math.hypot(*best_offset)):
                best_score = score
                best_offset = (dx, dy)
    return best_offset, best_score


def _point_cloud_input_fingerprint(
    config: AppConfig,
    footprint_path: Path,
    tree_features_path: Path | None,
) -> dict[str, Any]:
    paths = [config.path, footprint_path]
    for directory in (config.inputs.dtm_directory, config.inputs.dsm_directory):
        if directory is not None:
            paths.extend(
                path
                for path in directory.rglob("*")
                if path.is_file() and path.suffix.lower() == ".asc"
            )
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


def _point_in_any_polygon(point: Point2, polygons: list[ProjectedPolygon]) -> bool:
    """Return the exact predicate used as an oracle for the spatial index."""

    return any(_point_in_projected_polygon(point, polygon) for polygon in polygons)


def _point_in_projected_polygon(point: Point2, polygon: ProjectedPolygon) -> bool:
    if _point_on_ring_boundary(point, polygon.exterior):
        return True
    if not _point_in_ring(point, polygon.exterior):
        return False
    for hole in polygon.holes:
        if _point_on_ring_boundary(point, hole):
            return True
        if _point_in_ring(point, hole):
            return False
    return True


def _ring_bbox(ring: Ring) -> tuple[float, float, float, float]:
    if not ring:
        return math.inf, math.inf, -math.inf, -math.inf
    x_values = [point[0] for point in ring]
    y_values = [point[1] for point in ring]
    return min(x_values), min(y_values), max(x_values), max(y_values)


def _point_within_any_polygon_buffer(
    point: Point2,
    polygons: list[ProjectedPolygon],
    buffer_m: float,
) -> bool:
    """Return the exact buffered predicate used to verify indexed lookups."""

    if buffer_m <= 0.0:
        return False
    return any(_point_to_projected_polygon_boundary_m(point, polygon) <= buffer_m for polygon in polygons)


def _point_to_projected_polygon_boundary_m(point: Point2, polygon: ProjectedPolygon) -> float:
    return min(_point_to_ring_distance_m(point, ring) for ring in polygon.rings)


def _point_on_ring_boundary(point: Point2, ring: Ring) -> bool:
    return _point_to_ring_distance_m(point, ring) <= 1e-9


def _point_to_ring_distance_m(point: Point2, ring: Ring) -> float:
    if len(ring) < 2:
        return math.inf
    return min(
        _point_to_segment_distance_m(point, start, end)
        for start, end in zip(ring, [*ring[1:], ring[0]], strict=True)
    )


def _point_to_segment_distance_m(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    point_x, point_y = point
    start_x, start_y = start
    end_x, end_y = end
    segment_dx = end_x - start_x
    segment_dy = end_y - start_y
    segment_length_sq = segment_dx * segment_dx + segment_dy * segment_dy
    if segment_length_sq == 0.0:
        return math.hypot(point_x - start_x, point_y - start_y)
    t = ((point_x - start_x) * segment_dx + (point_y - start_y) * segment_dy) / segment_length_sq
    t = max(0.0, min(1.0, t))
    nearest_x = start_x + t * segment_dx
    nearest_y = start_y + t * segment_dy
    return math.hypot(point_x - nearest_x, point_y - nearest_y)


def _point_in_ring(point: Point2, ring: Ring) -> bool:
    x, y = point
    inside = False
    if len(ring) < 3:
        return inside
    previous_x, previous_y = ring[-1]
    for current_x, current_y in ring:
        intersects = (current_y > y) != (previous_y > y)
        if intersects:
            x_at_y = (previous_x - current_x) * (y - current_y) / (previous_y - current_y) + current_x
            if x < x_at_y:
                inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


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
        - (3 * eccentricity_sq / 8 + 3 * eccentricity_sq**2 / 32 + 45 * eccentricity_sq**3 / 1024) * math.sin(2 * lat_rad)
        + (15 * eccentricity_sq**2 / 256 + 45 * eccentricity_sq**3 / 1024) * math.sin(4 * lat_rad)
        - (35 * eccentricity_sq**3 / 3072) * math.sin(6 * lat_rad)
    )
    easting = scale * n * (
        a
        + (1 - t + c) * a**3 / 6
        + (5 - 18 * t + t**2 + 72 * c - 58 * second_eccentricity_sq) * a**5 / 120
    ) + 500000.0
    northing = scale * (
        m + n * math.tan(lat_rad) * (
            a**2 / 2
            + (5 - t + 9 * c + 4 * c**2) * a**4 / 24
            + (61 - 58 * t + t**2 + 600 * c - 330 * second_eccentricity_sq) * a**6 / 720
        )
    )
    return easting, northing
