"""Raw GeoJSON, PNG, and ASCII-grid inputs for the point-cloud stage."""

from __future__ import annotations

import json
import struct
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cities_reconstruction.config import ConfigError

NODATA_DEFAULT = -9999.0


@dataclass(frozen=True)
class RasterTile:
    """Metadata needed to address values in an ESRI ASCII-grid tile."""

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


def read_feature_collection(path: Path) -> list[dict[str, Any]]:
    """Read building polygons that are enabled for geometry reconstruction."""

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError(f"GeoJSON feature collection missing features list: {path}")
    return [
        feature
        for feature in features
        if (
            isinstance(feature, dict)
            and feature.get("geometry", {}).get("type") in {"Polygon", "MultiPolygon"}
            and feature.get("properties", {}).get("contributes_to_geometry", True)
            and feature.get("properties", {}).get("include_in_building_lod22_reconstruction", True)
        )
    ]


def read_png_rgba(path: Path) -> dict[str, Any]:
    """Decode an 8-bit, non-interlaced RGB or RGBA PNG."""

    data = path.read_bytes()
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        raise ConfigError(f"tree canopy overlay must be a PNG image: {path}")
    offset = len(signature)
    width = 0
    height = 0
    color_type = -1
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


def tiles_by_name(directory: Path) -> dict[str, RasterTile]:
    """Discover ASCII grids by basename and reject ambiguous duplicates."""

    tiles: dict[str, RasterTile] = {}
    normalized_paths: dict[str, Path] = {}
    paths = sorted(path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() == ".asc")
    for path in paths:
        normalized_name = path.name.casefold()
        previous_path = normalized_paths.get(normalized_name)
        if previous_path is not None:
            raise ConfigError(f"duplicate ASCII grid basename in {directory}: {previous_path} and {path}")
        normalized_paths[normalized_name] = path
        tiles[path.name] = read_ascii_grid_header(path)
    return tiles


def validate_tile_pairing(
    dtm_tiles: dict[str, RasterTile],
    dsm_tiles: dict[str, RasterTile],
    paired_names: list[str],
    bbox: tuple[float, float, float, float],
) -> None:
    """Require paired tiles to share a grid and reject unmatched ROI tiles."""

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
            raise ConfigError(f"DTM/DSM tile grid mismatch for {name}: {dtm_tile.path} and {dsm_tile.path}")

    unmatched_tiles = (
        *(("DTM", dtm_tiles[name]) for name in sorted(set(dtm_tiles) - set(dsm_tiles))),
        *(("DSM", dsm_tiles[name]) for name in sorted(set(dsm_tiles) - set(dtm_tiles))),
    )
    for source, tile in unmatched_tiles:
        if tile_intersects_bbox(tile, bbox):
            raise ConfigError(f"unmatched {source} tile intersects the configured region: {tile.path}")


def read_ascii_grid_header(path: Path) -> RasterTile:
    """Read and normalize the six-line ESRI ASCII-grid header."""

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


def tile_intersects_bbox(
    tile: RasterTile,
    bbox: tuple[float, float, float, float],
) -> bool:
    """Return whether a tile overlaps the projected region bounds."""

    min_x, min_y, max_x, max_y = bbox
    return not (tile.max_x < min_x or tile.xllcorner > max_x or tile.max_y < min_y or tile.yllcorner > max_y)


def paired_tile_cells(
    dtm_tile: RasterTile,
    dsm_tile: RasterTile,
    dtm_rows: list[list[float]],
    dsm_rows: list[list[float]],
) -> Iterable[tuple[float, float, float, float, int, int]]:
    """Yield paired ground/surface values at cell-center coordinates."""

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


def read_ascii_grid_values(tile: RasterTile) -> list[list[float]]:
    """Read numeric cell rows using metadata from ``read_ascii_grid_header``."""

    rows: list[list[float]] = []
    with tile.path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(tile.values_offset)
        for _row_index in range(tile.nrows):
            rows.append(_parse_grid_row(handle.readline()))
    return rows


def _unfilter_png_scanline(
    scanline: bytearray,
    previous: bytes,
    channels: int,
    filter_type: int,
) -> None:
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


def _parse_grid_row(line: str) -> list[float]:
    return [float(value) for value in line.split()]
