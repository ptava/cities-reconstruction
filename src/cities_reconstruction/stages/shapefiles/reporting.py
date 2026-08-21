"""Markdown reporting for the shapefiles retrieval stage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cities_reconstruction.config import AppConfig


def render_report(
    *,
    config: AppConfig,
    summary: dict[str, Any],
    categories: tuple[str, ...],
    tag_inventory_query_path: Path,
    tag_inventory_raw_path: Path,
    tag_inventory_path: Path,
    query_path: Path,
    raw_path: Path,
    all_features_path: Path,
    urban_planning_path: Path,
    air_purifiers_path: Path,
    category_paths: dict[str, Path],
    region_paths: dict[str, Path],
    diagnostics_path: Path,
    diagnostics_geojson_path: Path,
    imagery_diagnostics_path: Path,
    imagery_overlay_path: Path,
    summary_path: Path,
    preview_path: Path,
) -> str:
    """Render the human-readable report from completed stage data."""

    counts = summary["feature_counts"]
    category_counts = counts["by_category"]
    group_tag_counts = counts["by_group_tag"]
    unmapped_counts = counts["available_not_mapped_to_core"]
    zone_counts = counts["by_roi_zone"]
    skipped_by_reason = counts["skipped_by_reason"]
    tag_inventory = summary["tag_inventory"]
    diagnostics = summary["geometry_diagnostics"]
    tree_overlap_filter = summary["tree_overlap_filter"]
    tree_inputs = summary["tree_input_diagnostics"]
    surface_inputs = summary["surface_input_diagnostics"]
    surface_overlaps = summary["surface_overlap_diagnostics"]
    planning = summary["urban_planning"]
    supplemental_surface_lines = "\n".join(
        (
            f"- `{name}`: category `{details['category']}`, group `{details['group_tag']}`, "
            f"enabled `{str(details['enabled']).lower()}`, loaded {details['loaded_features']}, "
            f"accepted {surface_overlaps['by_supplemental'].get(name, {}).get('accepted_features', 0)}, "
            f"clipped {surface_overlaps['by_supplemental'].get(name, {}).get('clipped_features', 0)}, "
            f"removed {surface_overlaps['by_supplemental'].get(name, {}).get('removed_features', 0)}"
        )
        for name, details in surface_inputs["surfaces"].items()
    ) or "- none configured"
    supplemental_tree_lines = "\n".join(
        (
            f"- `{name}`: enabled `{str(details['enabled']).lower()}`, "
            f"loaded {details['loaded_features']}, CRS `{details['crs']}`, path `{details['path']}`"
        )
        for name, details in tree_inputs["inputs"].items()
    ) or "- none configured"
    planning_input_lines = "\n".join(
        (
            f"- `{name}`: enabled `{str(details['enabled']).lower()}`, "
            f"accepted trees {details['accepted_by_kind']['tree']}, "
            f"accepted air purifiers {details['accepted_by_kind']['air_purifier']}, "
            f"outside ROI {details['outside_roi']}, "
            f"CRS `{details['crs']}`, path `{details['path']}`"
        )
        for name, details in planning["inputs"].items()
    ) or "- none configured"
    planning_outside_lines = "\n".join(
        (
            f"- `{record['urban_planning_input_id']}` feature {record['source_feature_index']} "
            f"(`{record['id']}`, `{record['kind']}`): coordinates "
            f"`{record['coordinates']}`, distance {record['roi_distance_m']:g} m"
        )
        for record in planning["outside_records"]
    ) or "- none"
    category_lines = "\n".join(
        f"- {category}: {category_counts.get(category, 0)}"
        for category in categories
    )
    zone_lines = "\n".join(
        f"- {zone}: {count}"
        for zone, count in sorted(zone_counts.items())
    ) or "- none: 0"
    group_tag_lines = "\n".join(
        f"- {group_tag}: {count}"
        for group_tag, count in sorted(group_tag_counts.items())
    ) or "- none: 0"
    unmapped_lines = "\n".join(
        f"- {source_tag}: {count}"
        for source_tag, count in sorted(unmapped_counts.items())
    ) or "- none: 0"
    skipped_lines = "\n".join(
        f"- {reason}: {count}"
        for reason, count in sorted(skipped_by_reason.items())
    ) or "- none: 0"
    contributing_lines = _count_lines(diagnostics["contributing_by_category"], limit=20)
    non_contributing_lines = _count_lines(diagnostics["non_contributing_by_category"], limit=20)
    non_contributing_type_lines = _count_lines(diagnostics["non_contributing_by_geometry_type"], limit=20)
    top_tag_key_lines = _count_lines(tag_inventory["tag_key_counts"], limit=20)
    top_unclassified_lines = _count_lines(tag_inventory["unclassified_feature_like_tag_value_counts"], limit=20)
    classification_rule_lines = "\n".join(
        f"{index}. `{rule['category']}` / `{rule['group_tag']}`: "
        + ", ".join(f"`{expression}`" for expression in rule["match_any"])
        for index, rule in enumerate(summary["classification_rules"], start=1)
    )
    output_lines = "\n".join(
        [
            f"- Tag inventory query: `{tag_inventory_query_path}`",
            f"- Raw tag inventory response: `{tag_inventory_raw_path}`",
            f"- Full tag inventory: `{tag_inventory_path}`",
            f"- Overpass query: `{query_path}`",
            f"- Raw Overpass response: `{raw_path}`",
            f"- All accepted features: `{all_features_path}`",
            f"- Normalized urban-planning inputs: `{urban_planning_path}`",
            f"- Normalized air purifiers: `{air_purifiers_path}`",
            *[
                f"- {category} GeoJSON: `{path}`"
                for category, path in category_paths.items()
            ],
            *[
                f"- {region_name} GeoJSON: `{path}`"
                for region_name, path in region_paths.items()
            ],
            f"- Geometry diagnostics: `{diagnostics_path}`",
            f"- Non-contributing reference features: `{diagnostics_geojson_path}`",
            f"- Imagery diagnostics: `{imagery_diagnostics_path}`",
            f"- Imagery overlay preview: `{imagery_overlay_path}`",
            f"- Machine-readable summary: `{summary_path}`",
            f"- Graphical preview: `{preview_path}`",
        ]
    )
    assumptions = "\n".join(f"- {item}" for item in summary["assumptions"])
    inner_diameter_line = (
        f"- Inner diameter: {config.region.inner_diameter_m:g} m"
        if config.region.inner_diameter_m is not None
        else "- Inner diameter: not set (uniform treatment across the outer ROI)"
    )
    return f"""# Feature Retrieval Report

## Region

- Name: {config.region.name}
- Center: {config.region.center_lat:g}, {config.region.center_lon:g}
- CRS: {config.region.crs}
{inner_diameter_line}
- Outer diameter: {config.region.outer_diameter_m:g} m
- Source: {summary["source"]}

## Result

- Tag inventory raw elements: {tag_inventory["raw_elements"]}
- Tag inventory tagged elements: {tag_inventory["tagged_elements"]}
- Raw Overpass elements: {counts["raw_overpass_elements"]}
- Accepted features: {counts["accepted"]}
- Geometry-contributing polygon features: {diagnostics["contributing_feature_count"]}
- Reference-only non-contributing features: {diagnostics["non_contributing_feature_count"]}
- Generated gap-fill features: {diagnostics["generated_gap_fill_feature_count"]}
- Skipped elements: {counts["skipped"]}
- Overpass trees removed as supplemental-tree duplicates: {tree_overlap_filter["removed_overpass_tree_count"]} (tolerance {tree_overlap_filter["tolerance_m"]:g} m)

## Supplemental Tree Shapefiles

{supplemental_tree_lines}

## Urban-Planning GeoJSON Inputs

- Accepted planned trees: {planning["accepted_by_kind"]["tree"]}
- Accepted air purifiers: {planning["accepted_by_kind"]["air_purifier"]}
- Points outside the ROI: {planning["outside_roi"]}

{planning_input_lines}

### Outside-ROI Planning Records

{planning_outside_lines}

## Supplemental Surface Shapefiles

{supplemental_surface_lines}

## Surface Superposition Resolution

- Configured precedence: {" > ".join(surface_overlaps["precedence"])}
- Input contributing polygons: {surface_overlaps["input_polygon_features"]}
- Accepted mutually disjoint polygons: {surface_overlaps["accepted_polygon_features"]}
- Partially clipped polygons: {surface_overlaps["clipped_polygon_features"]}
- Fully covered polygons removed: {surface_overlaps["removed_polygon_features"]}
- Total superposed area removed: {surface_overlaps["removed_overlap_area_m2"]:g} m2

{surface_overlaps["policy"]}

## Available OSM Tag Inventory

Top available tag keys in the outer ROI:

{top_tag_key_lines}

Full key and key-value counts are saved in `tag_inventory.json`.

## Available OSM Tags Not Classified as Features

{top_unclassified_lines}

These are feature-like OSM tags from the first inventory query that are not currently used to create feature geometries. The complete inventory, including metadata tags, is saved in `tag_inventory.json`.

## Configured Classification Rules

Rules are evaluated in this order and the first match wins. A `key` expression matches any value; `key=value` requires an exact value.

{classification_rule_lines}

## Counts by Category

{category_lines}

## Counts by ROI Zone

{zone_lines}

## Counts by Group Tag

{group_tag_lines}

## Geometry Contribution Diagnostic

{diagnostics["contribution_rule"]}

Contributing features by category:

{contributing_lines}

Reference-only non-contributing features by category:

{non_contributing_lines}

Reference-only non-contributing features by geometry type:

{non_contributing_type_lines}

Gap-fill policy:

{diagnostics["gap_fill_policy"]}

## Available Terrain Tags Not Mapped to Core Groups

{unmapped_lines}

Features in this section are not dropped. They are kept in `other_terrain.geojson` and drawn as other terrain in the preview so the user can decide whether they need a dedicated group later.

## Skipped Elements

{skipped_lines}

## Outputs

{output_lines}

## Assumptions

{assumptions}
"""


def _count_lines(counts: dict[str, int], limit: int) -> str:
    if not counts:
        return "- none: 0"
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    lines = [f"- {key}: {count}" for key, count in ordered[:limit]]
    remaining = len(ordered) - limit
    if remaining > 0:
        lines.append(f"- ... {remaining} more entries in tag_inventory.json")
    return "\n".join(lines)
