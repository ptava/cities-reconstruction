"""Pure summaries for tree-model selection and parameter provenance."""

from __future__ import annotations

from typing import Any

from cities_reconstruction.stages.trees.models import TreeInstance


def species_counts(instances: list[TreeInstance]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for instance in instances:
        counts[instance.species] = counts.get(instance.species, 0) + 1
    return counts


def category_counts(instances: list[TreeInstance]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for instance in instances:
        counts[instance.model_category] = counts.get(instance.model_category, 0) + 1
    return counts


def information_summary(instances: list[TreeInstance]) -> dict[str, Any]:
    species_tag_model_count = sum(1 for instance in instances if instance.model_source.startswith("tag:"))
    fallback_model_count = sum(1 for instance in instances if instance.model_source.startswith("default:"))
    planning_model_count = sum(
        1 for instance in instances if instance.model_source.startswith("urban_planning:")
    )
    tag_counts = {
        "species_model": species_tag_model_count,
        "height_m": sum(1 for instance in instances if instance.height_source.startswith("tag:")),
        "crown_radius_m": sum(1 for instance in instances if instance.crown_radius_source.startswith("tag:")),
        "trunk_radius_m": sum(1 for instance in instances if instance.trunk_radius_source.startswith("tag:")),
    }
    allometry_counts = {
        "height_m": sum(1 for instance in instances if instance.height_source.startswith("allometry:")),
        "trunk_radius_m": sum(1 for instance in instances if instance.trunk_radius_source.startswith("allometry:")),
    }
    default_counts = {
        "species_model": fallback_model_count,
        "height_m": sum(1 for instance in instances if instance.height_source.startswith("default:")),
        "crown_radius_m": sum(1 for instance in instances if instance.crown_radius_source.startswith("default:")),
        "trunk_radius_m": sum(1 for instance in instances if instance.trunk_radius_source.startswith("default:")),
    }
    planning_counts = {
        "species_model": planning_model_count,
        "height_m": sum(1 for instance in instances if instance.height_source.startswith("urban_planning:")),
        "crown_radius_m": sum(
            1 for instance in instances if instance.crown_radius_source.startswith("urban_planning:")
        ),
        "trunk_radius_m": sum(
            1 for instance in instances if instance.trunk_radius_source.startswith("urban_planning:")
        ),
    }
    any_information = sum(
        1
        for instance in instances
        if instance.used_tags
        or instance.model_source.startswith("urban_planning:")
        or instance.height_source.startswith(("allometry:", "urban_planning:"))
        or instance.crown_radius_source.startswith("urban_planning:")
        or instance.trunk_radius_source.startswith(("allometry:", "urban_planning:"))
    )
    full_tag_information = sum(
        1
        for instance in instances
        if instance.model_source.startswith("tag:")
        and instance.height_source.startswith("tag:")
        and instance.crown_radius_source.startswith("tag:")
        and instance.trunk_radius_source.startswith("tag:")
    )
    return {
        "tree_count": len(instances),
        "trees_with_any_model_input_tags_or_allometry": any_information,
        "trees_with_species_tag_model": species_tag_model_count,
        "trees_with_direct_planning_model": planning_model_count,
        "trees_with_fallback_species_model": fallback_model_count,
        "trees_with_all_primary_values_from_tags": full_tag_information,
        "tag_value_counts": tag_counts,
        "allometry_value_counts": allometry_counts,
        "default_value_counts": default_counts,
        "planning_value_counts": planning_counts,
        "default_model_count": fallback_model_count,
        "fallback_model_count": fallback_model_count,
    }


def tree_information_payload(instance: TreeInstance) -> dict[str, Any]:
    return {
        "tree_id": instance.tree_id,
        "osm_id": instance.osm_id,
        "species": instance.species,
        "species_model": instance.model_category,
        "source_species": instance.source_species,
        "model_category": instance.model_category,
        "crown_shape": instance.crown_shape,
        "model_source": instance.model_source,
        "height_source": instance.height_source,
        "crown_radius_source": instance.crown_radius_source,
        "trunk_radius_source": instance.trunk_radius_source,
        "used_tags": list(instance.used_tags),
        "defaulted_fields": list(instance.defaulted_fields),
    }
