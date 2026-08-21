"""Pure diagnostic summaries for the shapefiles retrieval stage."""

from __future__ import annotations

from typing import Any

from cities_reconstruction.config import AppConfig
from cities_reconstruction.urban_planning import UrbanPlanningLoadResult


def supplemental_surface_input_diagnostics(
    config: AppConfig,
    loaded: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    surfaces = {
        surface.name: {
            "enabled": surface.enabled,
            "path": str(surface.path),
            "crs": surface.crs,
            "category": surface.category,
            "group_tag": surface.group_tag,
            "loaded_features": len(loaded.get(surface.name, [])),
        }
        for surface in config.shapefiles.supplemental
        if surface.category != "trees"
    }
    surface_inputs = tuple(item for item in config.shapefiles.supplemental if item.category != "trees")
    return {
        "enabled": any(surface.enabled for surface in surface_inputs),
        "configured_surfaces": len(surface_inputs),
        "enabled_surfaces": sum(surface.enabled for surface in surface_inputs),
        "loaded_features": sum(len(loaded.get(surface.name, [])) for surface in surface_inputs),
        "surfaces": surfaces,
    }


def supplemental_tree_input_diagnostics(
    config: AppConfig,
    loaded: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    inputs = {
        tree_input.name: {
            "enabled": tree_input.enabled,
            "path": str(tree_input.path),
            "crs": tree_input.crs,
            "loaded_features": len(loaded.get(tree_input.name, [])),
        }
        for tree_input in config.shapefiles.supplemental
        if tree_input.category == "trees"
    }
    tree_inputs = tuple(item for item in config.shapefiles.supplemental if item.category == "trees")
    return {
        "enabled": any(tree_input.enabled for tree_input in tree_inputs),
        "configured_inputs": len(tree_inputs),
        "enabled_inputs": sum(tree_input.enabled for tree_input in tree_inputs),
        "loaded_features": sum(len(loaded.get(tree_input.name, [])) for tree_input in tree_inputs),
        "inputs": inputs,
    }


def urban_planning_diagnostics(config: AppConfig, result: UrbanPlanningLoadResult) -> dict[str, Any]:
    accepted_by_kind = {"tree": 0, "air_purifier": 0}
    outside_by_kind = {"tree": 0, "air_purifier": 0}
    input_accepted_by_kind = {
        item.name: {"tree": 0, "air_purifier": 0}
        for item in config.urban_planning.inputs
    }
    input_outside_by_kind = {
        item.name: {"tree": 0, "air_purifier": 0}
        for item in config.urban_planning.inputs
    }
    for feature in result.accepted_features:
        properties = feature["properties"]
        kind = properties["kind"]
        input_name = properties["urban_planning_input_id"]
        accepted_by_kind[kind] += 1
        input_accepted_by_kind[input_name][kind] += 1
    for feature in result.outside_roi_features:
        properties = feature["properties"]
        kind = properties["kind"]
        input_name = properties["urban_planning_input_id"]
        outside_by_kind[kind] += 1
        input_outside_by_kind[input_name][kind] += 1

    inputs = {
        item.name: {
            "enabled": item.enabled,
            "path": str(item.path),
            "crs": item.crs,
            **result.per_input[item.name],
            "accepted_by_kind": input_accepted_by_kind[item.name],
            "outside_by_kind": input_outside_by_kind[item.name],
        }
        for item in config.urban_planning.inputs
    }
    outside_records = [
        {
            "urban_planning_input_id": properties["urban_planning_input_id"],
            "source_feature_index": properties["source_feature_index"],
            "id": properties["id"],
            "kind": properties["kind"],
            "coordinates": list(feature["geometry"]["coordinates"]),
            "roi_distance_m": properties["roi_distance_m"],
        }
        for feature in result.outside_roi_features
        for properties in (feature["properties"],)
    ]
    return {
        "enabled": any(item.enabled for item in config.urban_planning.inputs),
        "configured_inputs": len(config.urban_planning.inputs),
        "enabled_inputs": sum(item.enabled for item in config.urban_planning.inputs),
        "source_features": sum(details["source_features"] for details in result.per_input.values()),
        "accepted_features": len(result.accepted_features),
        "outside_roi": len(result.outside_roi_features),
        "accepted_by_kind": accepted_by_kind,
        "outside_by_kind": outside_by_kind,
        "outside_records": outside_records,
        "inputs": inputs,
    }


def build_geometry_diagnostics(features: list[dict[str, Any]], generated_gap_fill_count: int) -> dict[str, Any]:
    contributing_by_category: dict[str, int] = {}
    non_contributing_by_category: dict[str, int] = {}
    geometry_type_counts: dict[str, int] = {}
    non_contributing_by_type: dict[str, int] = {}
    contributing_count = 0
    non_contributing_count = 0

    for feature in features:
        geometry_type = feature["geometry"]["type"]
        category = feature["properties"]["category"]
        _increment(geometry_type_counts, geometry_type)
        if feature["properties"]["contributes_to_geometry"]:
            contributing_count += 1
            _increment(contributing_by_category, category)
        else:
            non_contributing_count += 1
            _increment(non_contributing_by_category, category)
            _increment(non_contributing_by_type, geometry_type)

    return {
        "contribution_rule": "Only Polygon and MultiPolygon features contribute to geometry reconstruction in this stage. LineString and Point features are retained for reference only.",
        "contributing_feature_count": contributing_count,
        "non_contributing_feature_count": non_contributing_count,
        "geometry_type_counts": dict(sorted(geometry_type_counts.items())),
        "contributing_by_category": dict(sorted(contributing_by_category.items())),
        "non_contributing_by_category": dict(sorted(non_contributing_by_category.items())),
        "non_contributing_by_geometry_type": dict(sorted(non_contributing_by_type.items())),
        "generated_gap_fill_feature_count": generated_gap_fill_count,
        "gap_fill_policy": "Gap-fill surfaces are generated by subtracting retrieved contributing polygons from the outer ROI polygon in a local meter plane. They are marked as generated gap_fill features and contribute to watertight surface coverage.",
    }


def non_contributing_features(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        feature
        for feature in features
        if not feature["properties"]["contributes_to_geometry"]
    ]


def build_summary(
    config: AppConfig,
    features: list[dict[str, Any]],
    raw_element_count: int,
    skipped_count: int,
    skipped_by_reason: dict[str, int],
    category_features: dict[str, list[dict[str, Any]]],
    source: str,
    tag_inventory: dict[str, Any],
    geometry_diagnostics: dict[str, Any],
    tree_overlap_filter: dict[str, Any],
    tree_input_diagnostics: dict[str, Any],
    surface_input_diagnostics: dict[str, Any],
    surface_overlap_diagnostics: dict[str, Any],
    urban_planning_diagnostics: dict[str, Any],
) -> dict[str, Any]:
    zone_counts: dict[str, int] = {}
    group_tag_counts: dict[str, int] = {}
    source_tag_counts: dict[str, int] = {}
    unmapped_terrain_counts: dict[str, int] = {}
    for feature in features:
        properties = feature["properties"]
        zone = properties["roi_zone"]
        zone_counts[zone] = zone_counts.get(zone, 0) + 1
        group_tag = properties["group_tag"]
        source_tag = properties["source_tag"]
        group_tag_counts[group_tag] = group_tag_counts.get(group_tag, 0) + 1
        source_tag_counts[source_tag] = source_tag_counts.get(source_tag, 0) + 1
        if properties["category"] == "other_terrain":
            unmapped_terrain_counts[source_tag] = unmapped_terrain_counts.get(source_tag, 0) + 1
    return {
        "region": {
            "name": config.region.name,
            "center_lat": config.region.center_lat,
            "center_lon": config.region.center_lon,
            "crs": config.region.crs,
            "inner_diameter_m": config.region.inner_diameter_m,
            "outer_diameter_m": config.region.outer_diameter_m,
        },
        "assumptions": [
            *_region_assumptions(config),
            "OSM tags are associated with feature categories by the ordered shapefiles.classification_rules from the selected TOML file; the first matching rule wins.",
            "Gap-fill surfaces are generated from the exact remaining ROI area after subtracting all retrieved contributing polygons, and are marked as generated gap_fill features.",
            "The Overpass query requests broad way/relation terrain candidates and nodes only for individual trees.",
            "Surface-like OSM features that do not map to a core group are preserved as other_terrain and listed for user review.",
            "Every enabled supplemental tree shapefile is merged into trees.geojson; Overpass tree nodes within inputs.tree_overlap_tolerance_m of any supplemental tree are treated as duplicate observations.",
            "Every enabled supplemental surface is clipped to the ROI and resolved at its configured supplemental:name or category fallback position.",
            "GeoJSON is written now; ESRI Shapefile export is a documented follow-up pending the GIS dependency choice.",
        ],
        "feature_counts": {
            "raw_overpass_elements": raw_element_count,
            "accepted": len(features),
            "skipped": skipped_count,
            "skipped_by_reason": skipped_by_reason,
            "by_category": {category: len(items) for category, items in category_features.items()},
            "by_group_tag": dict(sorted(group_tag_counts.items())),
            "by_source_tag": dict(sorted(source_tag_counts.items())),
            "by_roi_zone": zone_counts,
            "available_not_mapped_to_core": dict(sorted(unmapped_terrain_counts.items())),
        },
        "classification_rules": [
            {
                "category": rule.category,
                "group_tag": rule.group_tag,
                "match_any": list(rule.match_any),
            }
            for rule in config.shapefiles.classification_rules
        ],
        "geometry_diagnostics": geometry_diagnostics,
        "tree_overlap_filter": tree_overlap_filter,
        "tree_input_diagnostics": tree_input_diagnostics,
        "surface_input_diagnostics": surface_input_diagnostics,
        "surface_overlap_diagnostics": surface_overlap_diagnostics,
        "urban_planning": urban_planning_diagnostics,
        "tag_inventory": tag_inventory,
        "source": source,
    }


def _region_assumptions(config: AppConfig) -> list[str]:
    if config.region.inner_diameter_m is None:
        return [
            "No inner diameter is configured, so all supported features inside the outer ROI receive uniform full-region treatment.",
            "All buildings inside the outer ROI use reconstruction_scope=primary_roi and include_in_building_lod22_reconstruction=true.",
        ]
    return [
        "Features are assigned to inner or annular regions by closest geometry distance to the configured center, so polygons crossing an ROI boundary are retained.",
        "All supported feature categories are retained inside the inner diameter and in the annular region between inner and outer diameters.",
        "Annular-region buildings are retained as context with include_in_building_lod22_reconstruction=false so downstream building reconstruction can ignore them.",
    ]


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1
