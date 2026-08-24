"""Pure alignment diagnostics for the point-cloud stage."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from cities_reconstruction.config import AppConfig

from .geometry import (
    BUILDING_HEIGHT_THRESHOLD_M,
    TREE_BUILDING_FOOTPRINT_BUFFER_M,
    TREE_CANOPY_MASK_SEARCH_RADIUS_PX,
    TREE_EXCESS_GREEN_THRESHOLD,
    TREE_HEIGHT_THRESHOLD_M,
    TREE_LOCAL_RELIEF_RADIUS_M,
    TREE_LOCAL_RELIEF_THRESHOLD_M,
    TREE_MIN_GREEN_CHANNEL,
    TREE_ROOF_CLUSTER_RADIUS_M,
    TREE_ROOF_CLUSTER_Z_TOLERANCE_M,
    TREE_ROOF_OFFSET_THRESHOLD_M,
    TREE_ROOF_SEARCH_RADIUS_M,
    TREE_TAG_ASSOCIATION_RADIUS_M,
    PolygonSpatialIndex,
    ProjectedPolygon,
)

ALIGNMENT_SEARCH_RADIUS_M = 6
ALIGNMENT_SEARCH_STEP_M = 1
ALIGNMENT_WARN_SHIFT_M = 2.0
ALIGNMENT_FAIL_SHIFT_M = 5.0


def build_alignment_diagnostics(
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
    same_metric_output_crs: bool,
) -> dict[str, Any]:
    """Build the complete review payload for footprint/raster alignment."""

    best_offset, score = estimate_horizontal_offset(alignment_candidate_points, building_polygons)
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
            "same_metric_output_crs": same_metric_output_crs,
        },
        "footprint_path": str(footprint_path),
        "footprint_polygon_count": len(building_polygons),
        "ground_point_count": len(ground_points),
        "building_point_count": len(building_points),
        "alignment_candidate_point_count": len(alignment_candidate_points),
        "alignment_evidence": "in-ROI DSM cells at least 2 m above DTM, collected before footprint and tree classification",
        "tree_point_count": len(tree_points),
        "unclassified_point_count": len(unclassified_points),
        "dsm_classification_complete": len(ground_points)
        == (len(building_points) + len(tree_points) + len(unclassified_points)),
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


def estimate_horizontal_offset(
    building_points: list[tuple[float, float, float]],
    building_polygons: list[ProjectedPolygon],
) -> tuple[tuple[int, int], int]:
    """Find the deterministic integer-meter shift with maximum footprint overlap."""

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
