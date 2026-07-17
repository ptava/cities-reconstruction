"""Shared terrain handoff validation and vertical surface sampling."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Callable

from shapely import STRtree
from shapely.geometry import MultiPoint, Point, Polygon
from shapely.ops import nearest_points

from cities_reconstruction.config import AppConfig, ConfigError


Point3 = tuple[float, float, float]
TerrainSampler = Callable[[float, float], float]


def validate_completed_city_models_terrain(
    config: AppConfig,
    path: Path,
    *,
    context: str = "tree",
) -> None:
    """Reject a configured stage-3 terrain produced by a failed handoff."""

    stage_dir = (config.output.root_directory / "03_city_models").resolve()
    try:
        path.resolve().relative_to(stage_dir)
    except ValueError:
        return
    manifest_path = stage_dir / "city4cfd_reconstruction_manifest.json"
    if not manifest_path.exists():
        raise ConfigError(
            f"configured {context} terrain is a stage-3 artifact but its City4CFD "
            f"manifest is missing: {manifest_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid City4CFD manifest: {manifest_path}") from exc
    stage_status = manifest.get("stage_status") if isinstance(manifest, dict) else None
    if stage_status != "completed":
        raise ConfigError(
            f"configured {context} terrain comes from an unsuccessful City4CFD handoff "
            f"(stage_status={stage_status!r}): {manifest_path}"
        )


def load_terrain_sampler(path: Path, *, footprint_label: str = "tree footprint") -> TerrainSampler:
    if not path.exists():
        raise ConfigError(f"terrain geometry file does not exist: {path}")
    triangles = _read_mesh_triangles(path)
    if not triangles:
        raise ConfigError(f"terrain geometry file does not contain any triangles: {path}")

    indexed_triangles: list[tuple[Point3, Point3, Point3]] = []
    triangle_footprints: list[Polygon] = []
    terrain_vertices: set[tuple[float, float]] = set()
    for triangle in triangles:
        footprint = Polygon((point[0], point[1]) for point in triangle)
        if footprint.is_empty or footprint.area <= 1e-12:
            continue
        indexed_triangles.append(triangle)
        triangle_footprints.append(footprint)
        terrain_vertices.update((point[0], point[1]) for point in triangle)
    if not indexed_triangles:
        raise ConfigError(f"terrain geometry file does not contain any non-degenerate XY triangles: {path}")

    spatial_index = STRtree(triangle_footprints)
    terrain_extent = MultiPoint(terrain_vertices).convex_hull

    def sample(x: float, y: float) -> float:
        footprint = Point(x, y)
        hits = [
            height
            for index in spatial_index.query(footprint, predicate="intersects")
            if (height := _triangle_height_at_xy(indexed_triangles[int(index)], x, y)) is not None
        ]
        if hits:
            return max(hits)
        if not terrain_extent.covers(footprint):
            raise ConfigError(
                f"{footprint_label} could not be projected onto terrain geometry "
                f"at ({x:.3f}, {y:.3f}) from {path}"
            )

        nearest_index = int(spatial_index.nearest(footprint))
        nearest_terrain_point = nearest_points(footprint, triangle_footprints[nearest_index])[1]
        height = _triangle_plane_height_at_xy(
            indexed_triangles[nearest_index],
            float(nearest_terrain_point.x),
            float(nearest_terrain_point.y),
        )
        if height is None:
            raise ConfigError(
                f"{footprint_label} could not be projected onto terrain geometry "
                f"at ({x:.3f}, {y:.3f}) from {path}"
            )
        return height

    return sample


def _read_mesh_triangles(path: Path) -> list[tuple[Point3, Point3, Point3]]:
    suffix = path.suffix.lower()
    if suffix == ".obj":
        return _read_obj_mesh_triangles(path)
    if suffix == ".stl":
        return _read_ascii_stl_triangles(path)
    raise ConfigError(f"unsupported terrain geometry format: {path.suffix or path.name}")


def _read_obj_mesh_triangles(path: Path) -> list[tuple[Point3, Point3, Point3]]:
    vertices: list[Point3] = []
    triangles: list[tuple[Point3, Point3, Point3]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("v "):
                parts = line.split()
                if len(parts) >= 4:
                    try:
                        point = (float(parts[1]), float(parts[2]), float(parts[3]))
                    except ValueError as exc:
                        raise _terrain_parse_error(path, line_number, line, "invalid vertex coordinates") from exc
                    if not all(math.isfinite(value) for value in point):
                        raise _terrain_parse_error(path, line_number, line, "non-finite vertex coordinates")
                    vertices.append(point)
                continue
            if not line.startswith("f "):
                continue
            face_indices: list[int] = []
            for part in line.split()[1:]:
                index_text = part.split("/")[0]
                if not index_text:
                    continue
                try:
                    index = int(index_text)
                except ValueError as exc:
                    raise _terrain_parse_error(path, line_number, line, "invalid face index") from exc
                if index < 0:
                    index = len(vertices) + index + 1
                if index < 1 or index > len(vertices):
                    raise _terrain_parse_error(path, line_number, line, "face index is out of range")
                face_indices.append(index - 1)
            if len(face_indices) < 3:
                continue
            anchor = vertices[face_indices[0]]
            for left, right in zip(face_indices[1:], face_indices[2:]):
                triangles.append((anchor, vertices[left], vertices[right]))
    return triangles


def _read_ascii_stl_triangles(path: Path) -> list[tuple[Point3, Point3, Point3]]:
    try:
        text = path.read_text(encoding="ascii")
    except (UnicodeDecodeError, OSError) as exc:
        raise ConfigError(f"terrain geometry must be a readable ASCII STL: {path}") from exc
    if any(ord(character) < 9 or 13 < ord(character) < 32 for character in text):
        raise ConfigError(f"terrain geometry must be a readable ASCII STL: {path}")

    lines = [
        (line_number, raw_line.strip())
        for line_number, raw_line in enumerate(text.splitlines(), start=1)
        if raw_line.strip()
    ]
    if not lines:
        raise ConfigError(f"malformed terrain ASCII STL (no solid records): {path}")

    triangles: list[tuple[Point3, Point3, Point3]] = []
    position = 0
    while position < len(lines):
        solid_number, solid_line = lines[position]
        solid_parts = solid_line.split(maxsplit=1)
        if solid_parts[0] != "solid":
            raise _terrain_stl_parse_error(path, solid_number, solid_line, "unexpected STL token; expected solid")
        solid_name = solid_parts[1] if len(solid_parts) == 2 else ""
        position += 1

        while position < len(lines):
            line_number, line = lines[position]
            end_parts = line.split(maxsplit=1)
            if end_parts[0] == "endsolid":
                end_name = end_parts[1] if len(end_parts) == 2 else ""
                if end_name != solid_name:
                    raise _terrain_stl_parse_error(
                        path,
                        line_number,
                        line,
                        f"endsolid name {end_name!r} does not match solid {solid_name!r}",
                    )
                position += 1
                break
            triangle, position = _parse_terrain_stl_facet(lines, position, path)
            triangles.append(triangle)
        else:
            last_number, last_line = lines[-1]
            raise _terrain_stl_parse_error(
                path,
                last_number,
                last_line,
                f"incomplete solid {solid_name!r}; expected endsolid",
            )
    return triangles


def _parse_terrain_stl_facet(
    lines: list[tuple[int, str]],
    position: int,
    path: Path,
) -> tuple[tuple[Point3, Point3, Point3], int]:
    required_line_count = 7
    if position + required_line_count > len(lines):
        line_number, line = lines[-1]
        raise _terrain_stl_parse_error(path, line_number, line, "incomplete STL facet")

    facet_lines = lines[position : position + required_line_count]
    facet_number, facet_line = facet_lines[0]
    facet_parts = facet_line.split()
    if len(facet_parts) != 5 or facet_parts[:2] != ["facet", "normal"]:
        raise _terrain_stl_parse_error(path, facet_number, facet_line, "unexpected STL token; expected facet normal")
    _parse_terrain_stl_coordinates(facet_parts[2:], path, facet_number, facet_line, "facet normal")
    _require_terrain_stl_line(facet_lines[1], "outer loop", path)

    vertices: list[Point3] = []
    for line_number, line in facet_lines[2:5]:
        parts = line.split()
        if len(parts) != 4 or parts[0] != "vertex":
            raise _terrain_stl_parse_error(path, line_number, line, "expected vertex with three coordinates")
        vertices.append(
            _parse_terrain_stl_coordinates(parts[1:], path, line_number, line, "vertex")
        )
    _require_terrain_stl_line(facet_lines[5], "endloop", path)
    _require_terrain_stl_line(facet_lines[6], "endfacet", path)
    return (vertices[0], vertices[1], vertices[2]), position + required_line_count


def _require_terrain_stl_line(line_record: tuple[int, str], expected: str, path: Path) -> None:
    line_number, line = line_record
    if line != expected:
        raise _terrain_stl_parse_error(path, line_number, line, f"expected {expected}")


def _parse_terrain_stl_coordinates(
    parts: list[str],
    path: Path,
    line_number: int,
    line: str,
    label: str,
) -> Point3:
    try:
        point = (float(parts[0]), float(parts[1]), float(parts[2]))
    except (ValueError, IndexError) as exc:
        raise _terrain_stl_parse_error(path, line_number, line, f"invalid {label} coordinates") from exc
    if not all(math.isfinite(value) for value in point):
        raise _terrain_stl_parse_error(path, line_number, line, f"non-finite {label} coordinates")
    return point


def _terrain_stl_parse_error(path: Path, line_number: int, line: str, detail: str) -> ConfigError:
    return ConfigError(f"malformed terrain ASCII STL ({detail}) at {path}, line {line_number}: {line!r}")


def _terrain_parse_error(path: Path, line_number: int, line: str, detail: str) -> ConfigError:
    return ConfigError(f"malformed terrain geometry at {path}, line {line_number}: {line!r} ({detail})")


def _triangle_height_at_xy(triangle: tuple[Point3, Point3, Point3], x: float, y: float) -> float | None:
    (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = triangle
    if x < min(x1, x2, x3) - 1e-9 or x > max(x1, x2, x3) + 1e-9:
        return None
    if y < min(y1, y2, y3) - 1e-9 or y > max(y1, y2, y3) + 1e-9:
        return None
    denom = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if abs(denom) < 1e-12:
        return None
    u = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / denom
    v = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / denom
    w = 1.0 - u - v
    if u < -1e-9 or v < -1e-9 or w < -1e-9:
        return None
    return u * z1 + v * z2 + w * z3


def _triangle_plane_height_at_xy(triangle: tuple[Point3, Point3, Point3], x: float, y: float) -> float | None:
    (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = triangle
    denom = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if abs(denom) < 1e-12:
        return None
    u = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / denom
    v = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / denom
    return u * z1 + v * z2 + (1.0 - u - v) * z3
