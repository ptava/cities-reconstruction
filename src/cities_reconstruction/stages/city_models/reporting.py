"""Markdown reporting for the City4CFD reconstruction stage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cities_reconstruction.adapters.city4cfd import City4CFDExecutionResult
from cities_reconstruction.config import AppConfig


def render_report(
    config: AppConfig,
    city4cfd_config_path: Path,
    manifest_path: Path,
    footprint_diagnostics_path: Path,
    run_script_path: Path,
    footprint_path: Path,
    building_stl_path: Path,
    terrain_stl_path: Path,
    building_mesh_path: Path,
    terrain_mesh_path: Path,
    combined_terrain_mesh_path: Path,
    surface_mesh_paths: dict[str, Path],
    stage1_surface_layers: list[dict[str, Any]],
    preview_path: Path,
    diagnostics: dict[str, Any],
    footprint_diagnostics: dict[str, Any],
    building_count: int,
    execution: City4CFDExecutionResult,
    stdout_log_path: Path,
    stderr_log_path: Path,
    contract_status: str,
) -> str:
    """Render the City4CFD reconstruction handoff report."""

    return f"""# City4CFD Reconstruction Handoff Report

## Region

- Name: {config.region.name}
- CRS: {config.region.crs}
- LoD target: 2.2
- Alignment status: {diagnostics.get("alignment_status", "unknown")}
- Footprint overlap status: {footprint_diagnostics["overlap_status"]}
- Overlapping footprint pairs: {footprint_diagnostics["overlap_pair_count"]}
- Preserved inner rings: {footprint_diagnostics["inner_ring_count"]}
- Buildings prepared: {building_count}

## Outputs

- City4CFD config: `{city4cfd_config_path}`
- Reconstruction manifest: `{manifest_path}`
- Footprint diagnostics: `{footprint_diagnostics_path}`
- Run script: `{run_script_path}`
- Projected building footprints: `{footprint_path}`
- Offline building STL preview: `{building_stl_path}`
- Offline terrain STL preview: `{terrain_stl_path}`
- City4CFD building mesh: `{building_mesh_path}`
- City4CFD terrain mesh: `{terrain_mesh_path}`
- Combined City4CFD terrain OBJ: `{combined_terrain_mesh_path}` ({"present" if combined_terrain_mesh_path.exists() else "not present"})
- City4CFD semantic surface meshes: {len(surface_mesh_paths)} expected, {sum(path.exists() for path in surface_mesh_paths.values())} present
- Graphical preview: `{preview_path}`
- City4CFD stdout log: `{stdout_log_path}`
- City4CFD stderr log: `{stderr_log_path}`

## Stage 1 Surface Layers

The stage-1 surface categories are projected from EPSG:4326 into `{config.region.crs}` and carried into the City4CFD handoff as named SurfaceLayer polygon imports. Empty categories are ignored. With separate output enabled, City4CFD writes each imprinted category as its own `{config.city_models.output_file_name}_<layer_name>.{config.city_models.output_format}` mesh.

{render_surface_layer_report(stage1_surface_layers, surface_mesh_paths)}

## Execution

This stage prepares the City4CFD inputs from the `city_models` TOML settings, checks whether `city4cfd` is available, and runs it directly or through Docker when needed. The generated script remains available as a reproducible fallback for environments where the tool is installed later.

- Contract status: `{contract_status}`
- External execution status: `{execution.status}`
- Backend: `{execution.backend or "none"}`
- Return code: `{execution.return_code if execution.return_code is not None else "not run"}`
- Argument vector: `{list(execution.argv) if execution.argv else "not run"}`
- Stdout truncated: `{execution.stdout_truncated}`
- Stderr truncated: `{execution.stderr_truncated}`

## Assumptions

- The point-cloud stage already produced separate ground and building point clouds in the same projected CRS as the footprints.
- The City4CFD footprint GeoJSON preserves full polygon geometry, including inner rings/holes.
- Overlapping footprint pairs are reported for review because superposed footprints can create duplicated reconstructed surfaces.
- LoD2.2 roof geometry is expected to come from City4CFD/roofer using the building point cloud and projected roofprint polygons.
- The preview shows the actual City4CFD mesh outputs when they are present. The local STL previews remain deterministic QA fallbacks for environments where the generated meshes are missing.
"""


def render_surface_layer_report(
    stage1_surface_layers: list[dict[str, Any]],
    surface_mesh_paths: dict[str, Path],
) -> str:
    """Describe imported surface layers and their generated split meshes."""

    if not stage1_surface_layers:
        return "- No stage-1 surface layers were imported."
    if not surface_mesh_paths:
        return "- Surface layers were imported into the configured aggregate City4CFD output."
    lines = [
        f"- `{layer['category']}` -> `{layer['layer_path']}` as `SurfaceLayer` with "
        f"`layer_name={layer['layer_name']}` ({layer['feature_count']} features); generated mesh: "
        f"`{surface_mesh_paths[layer['category']]}` "
        f"({'present' if surface_mesh_paths[layer['category']].exists() else 'not present'})"
        for layer in stage1_surface_layers
    ]
    return "\n".join(lines)
