"""Strict ASCII-STL I/O for three-region air-purifier meshes."""

from __future__ import annotations

import math
from pathlib import Path

from cities_reconstruction.artifacts import atomic_text_writer
from cities_reconstruction.config import ConfigError

Point3 = tuple[float, float, float]
Triangle = tuple[Point3, Point3, Point3]
RegionMesh = dict[str, list[Triangle]]
REGION_NAMES = ("inlet", "outlet", "tower")


def read_region_stl(path: Path) -> RegionMesh:
    """Read and validate an ordered three-solid ASCII STL."""

    try:
        text = path.read_text(encoding="ascii")
    except (UnicodeDecodeError, OSError) as exc:
        raise ConfigError(f"air-purifier model must be a readable ASCII STL: {path}") from exc
    if any(ord(character) < 9 or 13 < ord(character) < 32 for character in text):
        raise ConfigError(f"air-purifier model must be a readable ASCII STL: {path}")

    mesh: RegionMesh = {}
    lines = [(number, raw.strip()) for number, raw in enumerate(text.splitlines(), start=1) if raw.strip()]
    position = 0
    while position < len(lines):
        line_number, line = lines[position]
        if line.startswith("#") and not mesh:
            raise ConfigError(f"comments are not allowed before first solid in ASCII STL: {path}:{line_number}")
        parts = line.split()
        if len(parts) != 2 or parts[0] != "solid":
            raise ConfigError(f"unexpected STL token at {path}:{line_number}: {line!r}")
        region = parts[1]
        if region in mesh:
            raise ConfigError(f"duplicate STL solid {region!r}: {path}:{line_number}")
        mesh[region] = []
        position += 1

        while position < len(lines):
            line_number, line = lines[position]
            if line.startswith("endsolid"):
                end_parts = line.split()
                if len(end_parts) != 2:
                    raise ConfigError(f"malformed endsolid at {path}:{line_number}")
                if end_parts[1] != region:
                    raise ConfigError(
                        f"endsolid {end_parts[1]!r} does not match solid {region!r}: {path}:{line_number}"
                    )
                position += 1
                break
            triangle, position = _parse_facet(lines, position, path)
            _validate_triangle(triangle, path)
            mesh[region].append(triangle)
        else:
            raise ConfigError(f"solid {region!r} has no matching endsolid: {path}")

    _validate_mesh(mesh, path)
    return {region: sorted(mesh[region]) for region in REGION_NAMES}


def write_region_stl(path: Path, mesh: RegionMesh) -> None:
    """Write a validated mesh deterministically with recomputed normals."""

    _validate_mesh(mesh, path)
    lines: list[str] = []
    for region in REGION_NAMES:
        lines.append(f"solid {region}")
        for triangle in sorted(mesh[region]):
            normal = _normal(triangle)
            lines.append("  facet normal " + " ".join(_format(value) for value in normal))
            lines.append("    outer loop")
            for point in triangle:
                lines.append("      vertex " + " ".join(_format(value) for value in point))
            lines.extend(("    endloop", "  endfacet"))
        lines.append(f"endsolid {region}")
    with atomic_text_writer(path) as handle:
        handle.write("\n".join(lines) + "\n")


def transform_region_mesh(
    mesh: RegionMesh,
    *,
    scale: tuple[float, float, float],
    rotation_deg: float,
    translation: Point3,
) -> RegionMesh:
    """Scale around the source base centre, rotate about +Z, then translate."""

    _validate_mesh(mesh, Path("<mesh>"))
    values = (*scale, rotation_deg, *translation)
    if not all(math.isfinite(value) for value in values):
        raise ConfigError("mesh transformation values must be finite")
    if any(value <= 0.0 for value in scale):
        raise ConfigError("mesh scale factors must be positive")
    min_x, max_x, min_y, max_y, min_z, _ = mesh_bounds(mesh)
    centre_x = (min_x + max_x) / 2.0
    centre_y = (min_y + max_y) / 2.0
    radians = math.radians(rotation_deg)
    cosine = math.cos(radians)
    sine = math.sin(radians)

    def transform(point: Point3) -> Point3:
        x = (point[0] - centre_x) * scale[0]
        y = (point[1] - centre_y) * scale[1]
        z = (point[2] - min_z) * scale[2]
        return (
            x * cosine - y * sine + translation[0],
            x * sine + y * cosine + translation[1],
            z + translation[2],
        )

    transformed: RegionMesh = {
        region: [
            (transform(triangle[0]), transform(triangle[1]), transform(triangle[2]))
            for triangle in triangles
        ]
        for region, triangles in mesh.items()
    }
    if not all(
        math.isfinite(value)
        for triangles in transformed.values()
        for triangle in triangles
        for point in triangle
        for value in point
    ):
        raise ConfigError("mesh transformation produced non-finite coordinates")
    return transformed


def mesh_bounds(mesh: RegionMesh) -> tuple[float, float, float, float, float, float]:
    points = [point for triangles in mesh.values() for triangle in triangles for point in triangle]
    if not points:
        raise ConfigError("cannot calculate bounds of an empty region mesh")
    return (
        min(point[0] for point in points),
        max(point[0] for point in points),
        min(point[1] for point in points),
        max(point[1] for point in points),
        min(point[2] for point in points),
        max(point[2] for point in points),
    )


def mesh_edge_counts(mesh: RegionMesh) -> dict[tuple[Point3, Point3], int]:
    counts: dict[tuple[Point3, Point3], int] = {}
    for triangles in mesh.values():
        for triangle in triangles:
            rounded = tuple(_rounded_point(point) for point in triangle)
            for start, end in ((rounded[0], rounded[1]), (rounded[1], rounded[2]), (rounded[2], rounded[0])):
                edge = (start, end) if start <= end else (end, start)
                counts[edge] = counts.get(edge, 0) + 1
    return counts


def _rounded_point(point: Point3) -> Point3:
    return (round(point[0], 9), round(point[1], 9), round(point[2], 9))


def _parse_facet(
    lines: list[tuple[int, str]],
    position: int,
    path: Path,
) -> tuple[Triangle, int]:
    expected = ("facet", "outer loop", "vertex", "vertex", "vertex", "endloop", "endfacet")
    if position + len(expected) > len(lines):
        raise ConfigError(f"malformed STL facet in {path}")
    facet_lines = lines[position : position + len(expected)]
    number, facet = facet_lines[0]
    facet_parts = facet.split()
    if len(facet_parts) != 5 or facet_parts[:2] != ["facet", "normal"]:
        raise ConfigError(f"malformed STL facet at {path}:{number}")
    _parse_coordinates(facet_parts[2:], path, number)
    if facet_lines[1][1] != "outer loop" or facet_lines[5][1] != "endloop" or facet_lines[6][1] != "endfacet":
        raise ConfigError(f"malformed STL facet at {path}:{number}")
    vertices: list[Point3] = []
    for vertex_number, vertex_line in facet_lines[2:5]:
        parts = vertex_line.split()
        if len(parts) != 4 or parts[0] != "vertex":
            raise ConfigError(f"malformed STL vertex at {path}:{vertex_number}")
        vertices.append(_parse_coordinates(parts[1:], path, vertex_number))
    return (vertices[0], vertices[1], vertices[2]), position + len(expected)


def _parse_coordinates(parts: list[str], path: Path, line_number: int) -> Point3:
    try:
        point = (float(parts[0]), float(parts[1]), float(parts[2]))
    except (ValueError, IndexError) as exc:
        raise ConfigError(f"malformed STL coordinates at {path}:{line_number}") from exc
    if not all(math.isfinite(value) for value in point):
        raise ConfigError(f"STL coordinates must be finite at {path}:{line_number}")
    return point


def _validate_mesh(mesh: RegionMesh, path: Path) -> None:
    if tuple(mesh) != REGION_NAMES:
        if set(mesh) == set(REGION_NAMES):
            raise ConfigError(f"STL solids must appear in {REGION_NAMES!r} order: {path}")
        raise ConfigError(f"STL must contain exactly the solids inlet, outlet, and tower: {path}")
    for region, triangles in mesh.items():
        if not triangles:
            raise ConfigError(f"empty solid {region!r} is not allowed: {path}")
        for triangle in triangles:
            _validate_triangle(triangle, path)
    if set(mesh_edge_counts(mesh).values()) != {2}:
        raise ConfigError(f"combined STL mesh must be a closed manifold: {path}")


def _validate_triangle(triangle: Triangle, path: Path) -> None:
    if len(triangle) != 3 or any(len(point) != 3 for point in triangle):
        raise ConfigError(f"malformed STL triangle: {path}")
    if not all(math.isfinite(value) for point in triangle for value in point):
        raise ConfigError(f"STL triangle coordinates must be finite: {path}")
    cross = _cross(triangle)
    if math.sqrt(sum(value * value for value in cross)) <= 1e-12:
        raise ConfigError(f"degenerate STL triangle is not allowed: {path}")


def _cross(triangle: Triangle) -> Point3:
    a, b, c = triangle
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    return (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )


def _normal(triangle: Triangle) -> Point3:
    cross = _cross(triangle)
    magnitude = math.sqrt(sum(value * value for value in cross))
    return tuple(value / magnitude for value in cross)  # type: ignore[return-value]


def _format(value: float) -> str:
    if abs(value) < 0.5e-9:
        value = 0.0
    return f"{value:.9f}"
