"""Markdown reporting for the point-cloud preparation stage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cities_reconstruction.config import AppConfig


def render_report(
    *,
    config: AppConfig,
    footprint_path: Path,
    projected_footprints_path: Path,
    ground_path: Path,
    building_path: Path,
    tree_path: Path | None,
    unclassified_path: Path,
    diagnostics_path: Path,
    manifest_path: Path,
    preview_path: Path,
    diagnostics: dict[str, Any],
) -> str:
    """Render the human-readable report from completed point-cloud data."""

    assumptions = "\n".join(f"- {item}" for item in diagnostics["assumptions"])
    tree_output = f"- Tree point cloud: `{tree_path}`\n" if tree_path is not None else ""
    tree_filter = diagnostics["tree_filter"]
    tree_filter_status = "enabled" if tree_filter["enabled"] else "disabled"
    return f"""# Point Cloud Preparation Report

## Region

- Name: {config.region.name}
- CRS: {config.region.crs}
- Alignment status: {diagnostics["alignment_status"]}
- Message: {diagnostics["message"]}

## Result

- Ground points: {diagnostics["ground_point_count"]}
- Building points: {diagnostics["building_point_count"]}
- Tree points: {diagnostics["tree_point_count"]}
- Unclassified DSM points: {diagnostics["unclassified_point_count"]}
- DSM classification complete: {diagnostics["dsm_classification_complete"]}
- Building footprints: {diagnostics["footprint_polygon_count"]}
- Estimated horizontal shift: {diagnostics["estimated_horizontal_shift_m"]} m
- Tree filter: {tree_filter_status}
- Tree tag points used: {tree_filter["tree_tag_point_count"]}

## Outputs

- Ground point cloud: `{ground_path}`
- Building point cloud: `{building_path}`
{tree_output}- Unclassified DSM point cloud: `{unclassified_path}`
- Building footprints used: `{footprint_path}`
- Projected building footprints for City4CFD: `{projected_footprints_path}`
- Alignment diagnostics: `{diagnostics_path}`
- City4CFD point-cloud manifest: `{manifest_path}`
- Graphical alignment preview: `{preview_path}`

## Assumptions

{assumptions}
"""
