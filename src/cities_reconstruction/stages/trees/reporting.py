"""Markdown reporting for parametric tree reconstruction."""

from __future__ import annotations

from pathlib import Path

from cities_reconstruction.config import AppConfig
from cities_reconstruction.stages.trees.diagnostics import category_counts, information_summary
from cities_reconstruction.stages.trees.models import TreeInstance


def render_report(
    config: AppConfig,
    tree_features_path: Path,
    placement_path: Path,
    library_path: Path,
    manifest_path: Path,
    trunks_stl_path: Path,
    crowns_stl_path: Path,
    combined_stl_path: Path,
    species_crown_paths: dict[str, Path],
    preview_path: Path,
    instances: list[TreeInstance],
    species_counts: dict[str, int],
    surface_origin_x: float,
    surface_origin_y: float,
    terrain_geometry_path: Path | None,
) -> str:
    counts = "\n".join(f"- {species}: {count}" for species, count in sorted(species_counts.items())) or "- none"
    category_lines = "\n".join(
        f"- {category}: {count}" for category, count in sorted(category_counts(instances).items())
    ) or "- none"
    species_crown_lines = "\n".join(
        f"- {species}: `{path}`" for species, path in sorted(species_crown_paths.items())
    ) or "- none"
    information = information_summary(instances)
    tree_rows = "\n".join(
        "| {tree_id} | {osm_id} | {species} | {category} | {model_source} | {height_source} | {crown_source} | {trunk_source} | {defaulted} |".format(
            tree_id=instance.tree_id,
            osm_id=instance.osm_id if instance.osm_id is not None else "",
            species=instance.species,
            category=instance.model_category,
            model_source=instance.model_source,
            height_source=instance.height_source,
            crown_source=instance.crown_radius_source,
            trunk_source=instance.trunk_radius_source,
            defaulted=", ".join(instance.defaulted_fields) if instance.defaulted_fields else "none",
        )
        for instance in instances[:200]
    )
    if not tree_rows:
        tree_rows = "| none | | | | | | | | |"
    return f"""# Tree Model Generation Report

Region: {config.region.name}
CRS: {config.region.crs}

## Summary

- Source tree features: {tree_features_path}
- Generated tree instances: {len(instances)}
- Species counts:
{counts}
- Category counts:
{category_lines}
- Trees reconstructed from species tags: {information["trees_with_species_tag_model"]} / {information["tree_count"]}
- Trees reconstructed from direct urban-planning models: {information["trees_with_direct_planning_model"]} / {information["tree_count"]}
- Trees reconstructed with configured fallback species model ({config.trees.default}): {information["trees_with_fallback_species_model"]} / {information["tree_count"]}
- Trees with any usable source information or allometry: {information["trees_with_any_model_input_tags_or_allometry"]} / {information["tree_count"]}
- Trees with all primary values directly from tags: {information["trees_with_all_primary_values_from_tags"]} / {information["tree_count"]}
- Defaulted values: species model {information["default_value_counts"]["species_model"]}, height {information["default_value_counts"]["height_m"]}, crown radius {information["default_value_counts"]["crown_radius_m"]}, trunk radius {information["default_value_counts"]["trunk_radius_m"]}

## Per-Tree Model Inputs

| Tree | OSM ID | Species | Category model | Model source | Height source | Crown source | Trunk source | Defaulted fields |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
{tree_rows}

## Outputs

- Tree placements: {placement_path}
- Species library: {library_path}
- Manifest: {manifest_path}
- Trunk STL: {trunks_stl_path}
- Crown STL: {crowns_stl_path}
- Combined STL: {combined_stl_path}
- Species crown STL surfaces:
{species_crown_lines}
- 3D preview: {preview_path}
- STL surface frame: local City4CFD origin at ({surface_origin_x:.3f}, {surface_origin_y:.3f})
- Terrain geometry projection: {terrain_geometry_path if terrain_geometry_path is not None else "not provided"}

## Assumptions

- Tree ground elevation defaults to z=0 when no terrain geometry is provided.
- Tree STL surfaces are translated to the same local projected origin used by the City4CFD handoff, while the placement GeoJSON remains in EPSG:25832.
- When a terrain geometry file is provided, tree bases are projected onto that terrain and placed just below the local surface.
- Trees with species tags must resolve through the configured species/category mapping.
- Trees without species tags use the configured fallback species ({config.trees.default}) through the same species/category mapping.
- Missing dimensions keep default values from the selected category model; available parseable tags override only the corresponding dimension.
- The generated STL files are low-poly CFD handoff geometry and graphical QA artifacts, not botanically detailed assets.
"""
