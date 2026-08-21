"""Projected geometry predicates and raster-to-point classification."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cities_reconstruction.config import ConfigError

from .inputs import (
    RasterTile,
    paired_tile_cells,
    read_ascii_grid_values,
    tile_intersects_bbox,
    tiles_by_name,
    validate_tile_pairing,
)

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

Point2 = tuple[float, float]
Ring = tuple[Point2, ...]


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

    def candidate_indices(self, point: Point2) -> tuple[int, ...]:
        x, y = point
        return self.cells.get((math.floor(x / self.cell_size), math.floor(y / self.cell_size)), ())

    def contains(self, point: Point2) -> bool:
        x, y = point
        for polygon_index in self.candidate_indices(point):
            min_x, min_y, max_x, max_y = self.bounding_boxes[polygon_index]
            if min_x <= x <= max_x and min_y <= y <= max_y:
                if _point_in_projected_polygon(point, self.polygons[polygon_index]):
                    return True
        return False

    def within_buffer(self, point: Point2, buffer_m: float) -> bool:
        if buffer_m <= 0.0:
            return False
        if buffer_m > self.buffer_m:
            raise ValueError(f"polygon index supports buffers up to {self.buffer_m:g} m; got {buffer_m:g} m")
        x, y = point
        for polygon_index in self.candidate_indices(point):
            min_x, min_y, max_x, max_y = self.bounding_boxes[polygon_index]
            if not (min_x - buffer_m <= x <= max_x + buffer_m and min_y - buffer_m <= y <= max_y + buffer_m):
                continue
            if _point_to_projected_polygon_boundary_m(point, self.polygons[polygon_index]) <= buffer_m:
                return True
        return False


def classify_raster_points(
    dtm_directory: Path | None,
    dsm_directory: Path | None,
    bbox: tuple[float, float, float, float],
    building_polygons: list[ProjectedPolygon],
    tree_mask: dict[str, Any] | None,
    tree_tag_points: list[Point2],
) -> tuple[
    list[tuple[float, float, float]],
    list[tuple[float, float, float]],
    list[tuple[float, float, float]],
    list[tuple[float, float, float]],
    list[tuple[float, float, float]],
    dict[str, Any],
]:
    """Classify each valid in-ROI DSM cell as building, tree, or unclassified."""

    assert dtm_directory is not None
    assert dsm_directory is not None
    dtm_tiles = tiles_by_name(dtm_directory)
    dsm_tiles = tiles_by_name(dsm_directory)
    paired_names = sorted(set(dtm_tiles) & set(dsm_tiles))
    validate_tile_pairing(dtm_tiles, dsm_tiles, paired_names, bbox)
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
        if not tile_intersects_bbox(dtm_tile, bbox):
            skipped_tiles += 1
            continue
        used_tiles.append(name)
        dtm_rows = read_ascii_grid_values(dtm_tile)
        dsm_rows = read_ascii_grid_values(dsm_tile)
        local_radius_cells = max(1, math.ceil(TREE_LOCAL_RELIEF_RADIUS_M / dsm_tile.cellsize))
        roof_index = (
            _building_roof_point_index(dtm_tile, dsm_tile, dtm_rows, dsm_rows, bbox, polygon_index)
            if tree_mask is not None
            else {}
        )
        for x, y, ground_z, surface_z, row_index, col_index in paired_tile_cells(
            dtm_tile,
            dsm_tile,
            dtm_rows,
            dsm_rows,
        ):
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
            if tree_mask is not None and height_above_ground >= TREE_HEIGHT_THRESHOLD_M:
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
    return (
        ground_points,
        building_points,
        tree_points,
        unclassified_points,
        alignment_candidate_points,
        {
            "paired_tile_count": len(paired_names),
            "used_tile_count": len(used_tiles),
            "skipped_tile_count": skipped_tiles,
            "used_tiles": used_tiles,
            "tree_filter_counts": tree_filter_counts,
        },
    )


def _point_has_tree_tag_association(point: Point2, tree_tag_points: list[Point2]) -> bool:
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
    for x, y, ground_z, surface_z, _row_index, _col_index in paired_tile_cells(
        dtm_tile,
        dsm_tile,
        dtm_rows,
        dsm_rows,
    ):
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
        summary for summary in cluster_summaries if summary[0] <= surface_z + TREE_ROOF_CLUSTER_Z_TOLERANCE_M
    ]
    close_z_clusters = [
        summary for summary in candidate_roof_clusters if abs(surface_z - summary[0]) < TREE_ROOF_OFFSET_THRESHOLD_M
    ]
    if close_z_clusters:
        return min(close_z_clusters, key=lambda summary: summary[1])[0]
    if candidate_roof_clusters:
        return min(candidate_roof_clusters, key=lambda summary: summary[1])[0]
    return min(cluster_summaries, key=lambda summary: summary[1])[0]


def _roof_index_key(x: float, y: float) -> tuple[int, int]:
    return math.floor(x / TREE_ROOF_SEARCH_RADIUS_M), math.floor(y / TREE_ROOF_SEARCH_RADIUS_M)


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
        _point_to_segment_distance_m(point, start, end) for start, end in zip(ring, [*ring[1:], ring[0]], strict=True)
    )


def _point_to_segment_distance_m(
    point: Point2,
    start: Point2,
    end: Point2,
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
