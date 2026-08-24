"""File parsers used by the City4CFD reconstruction stage."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


def read_feature_collection(path: Path) -> list[dict[str, Any]]:
    """Read polygon features from a GeoJSON FeatureCollection handoff."""

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError(f"GeoJSON feature collection missing features list: {path}")
    return [
        feature
        for feature in features
        if isinstance(feature, dict) and feature.get("geometry", {}).get("type") in {"Polygon", "MultiPolygon"}
    ]


def read_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON document whose root must be an object."""

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def point_cloud_cell_stats(path: Path, prefer: str) -> dict[tuple[int, int], float]:
    """Aggregate ASCII PLY elevations into two-metre horizontal cells."""

    if prefer not in {"min", "max"}:
        raise ValueError("prefer must be either 'min' or 'max'")
    stats: dict[tuple[int, int], float] = {}
    in_data = False
    cell_size = 2.0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if in_data:
                parts = line.split()
                if len(parts) < 3:
                    continue
                x = float(parts[0])
                y = float(parts[1])
                z = float(parts[2])
                key = (math.floor(x / cell_size), math.floor(y / cell_size))
                current = stats.get(key)
                if current is None:
                    stats[key] = z
                elif prefer == "min":
                    stats[key] = min(current, z)
                else:
                    stats[key] = max(current, z)
            elif line.strip() == "end_header":
                in_data = True
    return stats
