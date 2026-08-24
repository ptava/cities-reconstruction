"""Payload assembly and manifest-last publication for the trees stage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cities_reconstruction.artifacts import lightweight_state_fingerprint
from cities_reconstruction.config import AppConfig
from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    JsonValue,
    StageManifest,
    StageStatus,
    publish_stage_manifest,
)
from cities_reconstruction.stage_layout import StageId
from cities_reconstruction.stages.trees.diagnostics import (
    category_counts,
    information_summary,
    tree_information_payload,
)
from cities_reconstruction.stages.trees.inputs import (
    configured_species_models,
    match_category,
    normalize_species_name,
    species_category_mapping,
)
from cities_reconstruction.stages.trees.models import TreeInstance


@dataclass(frozen=True)
class TreesPublicationInput:
    config: AppConfig
    output_directory: Path
    tree_features_path: Path
    placement_path: Path
    library_path: Path
    report_path: Path
    preview_path: Path
    trunks_stl_path: Path
    crowns_stl_path: Path
    combined_stl_path: Path
    species_crown_paths: dict[str, Path]
    instances: list[TreeInstance]
    species_counts: dict[str, int]
    surface_origin_x: float
    surface_origin_y: float
    terrain_geometry_path: Path | None


def placement_geojson(instances: list[TreeInstance]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(instance.x, 3), round(instance.y, 3), round(instance.z, 3)],
                },
                "properties": {
                    "tree_id": instance.tree_id,
                    "species": instance.species,
                    "species_model": instance.model_category,
                    "source_species": instance.source_species,
                    "model_category": instance.model_category,
                    "crown_shape": instance.crown_shape,
                    "height_m": round(instance.height_m, 3),
                    "crown_radius_m": round(instance.crown_radius_m, 3),
                    "trunk_radius_m": round(instance.trunk_radius_m, 3),
                    "trunk_height_m": round(instance.trunk_height_m, 3),
                    "roi_zone": instance.roi_zone,
                    "osm_id": instance.osm_id,
                    "projected_crs": "EPSG:25832",
                    "model_source": instance.model_source,
                    "height_source": instance.height_source,
                    "crown_radius_source": instance.crown_radius_source,
                    "trunk_radius_source": instance.trunk_radius_source,
                    "used_tags": list(instance.used_tags),
                    "defaulted_fields": list(instance.defaulted_fields),
                },
            }
            for instance in instances
        ],
    }


def library_payload(config: AppConfig) -> dict[str, Any]:
    models = configured_species_models(config)
    category_mapping = species_category_mapping(config)
    default_species = config.trees.default.strip()
    default_model = match_category(
        category_mapping.get(normalize_species_name(default_species)),
        models,
    )
    assumptions = [
        "Species are represented by low-poly parametric models for CFD geometry preparation.",
        "Planning features with a direct model category bypass species mapping.",
        "Tree features with species tags must resolve through the configured species/category mapping.",
        "Available OSM height, crown diameter, trunk diameter, and circumference tags override defaults when parseable; missing fields keep species defaults.",
    ]
    if default_model is not None:
        assumptions.insert(
            3,
            f"Tree features without species tags use the configured default species {default_species!r}, mapped to category {default_model.name!r}.",
        )
    return {
        "configured_default_species": default_species,
        "configured_default_category": default_model.name if default_model is not None else None,
        "model_library_path": str(config.trees.model_library_path) if config.trees.model_library_path is not None else None,
        "category_mapping_path": str(config.trees.category_mapping_path) if config.trees.category_mapping_path is not None else None,
        "supported_species": {
            name: {
                "aliases": list(model.aliases),
                "default_height_m": model.default_height_m,
                "default_crown_radius_m": model.default_crown_radius_m,
                "default_trunk_radius_m": model.default_trunk_radius_m,
                "crown_base_fraction": model.crown_base_fraction,
                "crown_shape": model.crown_shape,
            }
            for name, model in sorted((model.name, model) for model in models)
        },
        "assumptions": assumptions,
    }


def publish_trees_manifest(publication: TreesPublicationInput) -> StageManifest:
    artifacts = (
        ArtifactReference("trees-combined-surface", publication.combined_stl_path, ArtifactKind.HANDOFF),
        ArtifactReference("tree-trunks-surface", publication.trunks_stl_path, ArtifactKind.HANDOFF),
        ArtifactReference("tree-crowns-surface", publication.crowns_stl_path, ArtifactKind.HANDOFF),
        *(
            ArtifactReference(
                f"species-crown-{path.stem.removesuffix('_crowns')}",
                path,
                ArtifactKind.HANDOFF,
            )
            for _species, path in sorted(publication.species_crown_paths.items())
        ),
        ArtifactReference("tree-placements", publication.placement_path, ArtifactKind.SUPPORTING),
        ArtifactReference("species-library", publication.library_path, ArtifactKind.SUPPORTING),
        ArtifactReference("report", publication.report_path, ArtifactKind.REPORT),
        ArtifactReference("preview", publication.preview_path, ArtifactKind.PREVIEW),
    )
    return publish_stage_manifest(
        stage=StageId.TREES.value,
        status=StageStatus.COMPLETED,
        output_directory=publication.output_directory,
        report_path=publication.report_path,
        preview_path=publication.preview_path,
        input_state_fingerprint=_trees_input_fingerprint(
            publication.config,
            publication.tree_features_path,
            publication.terrain_geometry_path,
        ),
        artifacts=artifacts,
        metrics={
            "tree_count": len(publication.instances),
            "species_counts": _json_counts(publication.species_counts),
            "category_counts": _json_counts(category_counts(publication.instances)),
        },
        details=_manifest_payload(publication),
    )


def _manifest_payload(publication: TreesPublicationInput) -> dict[str, Any]:
    config = publication.config
    information = information_summary(publication.instances)
    return {
        "region": config.region.name,
        "crs": config.region.crs,
        "source_tree_features": str(publication.tree_features_path),
        "placement_geojson": str(publication.placement_path),
        "species_library": str(publication.library_path),
        "surfaces": {
            "trunks": str(publication.trunks_stl_path),
            "crowns": str(publication.crowns_stl_path),
            "combined": str(publication.combined_stl_path),
            "species_crowns": {
                species: str(path) for species, path in publication.species_crown_paths.items()
            },
        },
        "tree_count": len(publication.instances),
        "species_counts": publication.species_counts,
        "category_counts": category_counts(publication.instances),
        "information_summary": information,
        "fallback": {
            "default_species": config.trees.default,
            "model_source": f"default:{config.trees.default}:species_category_mapping",
            "tree_count": information["fallback_model_count"],
        },
        "surface_frame": {
            "name": "city4cfd_local_origin",
            "origin_x": round(publication.surface_origin_x, 3),
            "origin_y": round(publication.surface_origin_y, 3),
            "description": "Tree STL surfaces are translated to the same local projected origin used by the City4CFD handoff.",
        },
        "terrain_geometry_path": (
            str(publication.terrain_geometry_path)
            if publication.terrain_geometry_path is not None
            else None
        ),
        "tree_information": [tree_information_payload(instance) for instance in publication.instances],
    }


def _trees_input_fingerprint(
    config: AppConfig,
    tree_features_path: Path,
    terrain_geometry_path: Path | None,
) -> dict[str, JsonValue]:
    paths = [config.path, tree_features_path]
    if config.trees.model_library_path is not None:
        paths.append(config.trees.model_library_path)
    if config.trees.category_mapping_path is not None:
        paths.append(config.trees.category_mapping_path)
    if terrain_geometry_path is not None:
        paths.append(terrain_geometry_path)
    return lightweight_state_fingerprint(
        {
            "stage": "trees",
            "crs": config.region.crs,
            "default_species": config.trees.default,
            "terrain_geometry_path": str(terrain_geometry_path) if terrain_geometry_path else None,
        },
        paths,
    )


def _json_counts(counts: dict[str, int]) -> dict[str, JsonValue]:
    return {name: count for name, count in counts.items()}
