"""Strict vertex-only ASCII PLY input and horizontal preparation for City4CFD."""

import math
from pathlib import Path
from typing import TextIO

from cities_reconstruction.errors import ConfigError
from cities_reconstruction.geometry.crs import transform_xy


def _vertex_header(handle: TextIO) -> tuple[int, list[str]]:
    if handle.readline().strip() != "ply" or handle.readline().strip() != "format ascii 1.0":
        raise ConfigError("point clouds must use PLY format ascii 1.0")
    count: int | None = None
    properties: list[str] = []
    for line in handle:
        parts = line.split()
        if not parts or parts[0] in {"comment", "obj_info"}:
            continue
        if parts == ["end_header"]:
            break
        if len(parts) == 3 and parts[:2] == ["element", "vertex"] and count is None:
            count = int(parts[2])
        elif len(parts) == 3 and parts[0] == "property" and count is not None:
            if parts[1] not in {
                "char",
                "uchar",
                "short",
                "ushort",
                "int",
                "uint",
                "float",
                "double",
                "int8",
                "uint8",
                "int16",
                "uint16",
                "int32",
                "uint32",
                "float32",
                "float64",
            }:
                raise ConfigError("unsupported PLY vertex property type")
            properties.append(parts[2])
        else:
            raise ConfigError("PLY inputs require only a vertex element with scalar properties")
    else:
        raise ConfigError("PLY header is missing end_header")
    if (
        count is None
        or count < 0
        or not {"x", "y", "z"}.issubset(properties)
        or len(set(properties)) != len(properties)
    ):
        raise ConfigError("PLY requires a nonnegative vertex count and unique x, y, z properties")
    return count, properties


def read_projected_ply(
    path: Path,
    source_crs: str,
    working_crs: str,
    bbox: tuple[float, float, float, float],
) -> list[tuple[float, float, float]]:
    """Validate every vertex; retain ROI XY samples in the working CRS, preserving Z."""
    points: list[tuple[float, float, float]] = []
    try:
        with path.open(encoding="ascii") as handle:
            count, properties = _vertex_header(handle)
            xi, yi, zi = (properties.index(name) for name in ("x", "y", "z"))
            for index in range(count):
                values = handle.readline().split()
                if len(values) != len(properties):
                    raise ConfigError(f"invalid or missing PLY vertex {index + 1}")
                coordinates = [float(value) for value in values]
                if not all(math.isfinite(value) for value in coordinates):
                    raise ConfigError(f"PLY vertex {index + 1} requires finite values")
                x, y = transform_xy(coordinates[xi], coordinates[yi], source_crs, working_crs)
                if bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]:
                    points.append((x, y, coordinates[zi]))
            if any(line.strip() for line in handle):
                raise ConfigError("unexpected data after declared PLY vertices")
    except (OSError, UnicodeError, ValueError, ConfigError) as exc:
        raise ConfigError(f"cannot prepare point cloud {path}: {exc}") from exc
    return points
