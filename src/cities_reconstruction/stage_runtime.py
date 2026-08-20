"""Typed execution adapters for pipeline stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from .config import (
    AppConfig,
    SupplementalShapefileConfig,
    validate_config,
)
from .stage_contract import StageOutput
from .stages import (
    air_purifiers,
    city_models,
    point_cloud,
    shapefiles,
    trees,
    visual_enrichment,
)


@dataclass(frozen=True)
class StageRunOptions:
    """CLI-independent overrides for one stage execution request."""

    overpass_json: Path | None = None
    streets_shapefile: Path | None = None
    streets_shapefile_crs: str | None = None
    green_areas_shapefile: Path | None = None
    green_areas_shapefile_crs: str | None = None
    segmentation_geojson: Path | None = None
    sat2lod2_geojson: Path | None = None
    tree_canopy_overlay: Path | None = None
    building_footprints_geojson: Path | None = None
    tree_terrain_geometry: Path | None = None
    model_library: Path | None = None
    terrain_geometry: Path | None = None
    city_models_lod: str | None = None
    city_models_top_height: float | None = None
    city_models_bnd_type_bpg: str | None = None
    city_models_bpg_blockage_ratio: bool | None = None
    city_models_flow_direction: tuple[float, float] | None = None
    city_models_buffer_region: float | None = None
    city_models_reconstruct_boundaries: bool | None = None
    city_models_terrain_thinning: float | None = None
    city_models_smooth_terrain_iterations: int | None = None
    city_models_smooth_terrain_max_pts: int | None = None
    city_models_building_percentile: float | None = None
    city_models_edge_max_len: float | None = None
    city_models_reconstruction_influence_region: float | None = None
    city_models_reconstruction_complexity_factor: float | None = None
    city_models_reconstruction_validate: bool | None = None
    city_models_filters_min_area: float | None = None
    city_models_filters_min_height: float | None = None
    city_models_output_file_name: str | None = None
    city_models_output_format: str | None = None
    city_models_output_separately: bool | None = None
    city_models_output_log: bool | None = None
    city_models_log_file: str | None = None
    city_models_docker_image: str | None = None


StageRunner = Callable[[AppConfig, StageRunOptions], StageOutput]


def run_shapefiles(
    config: AppConfig,
    options: StageRunOptions,
) -> StageOutput:
    """Run the shapefiles stage with CLI overrides applied."""

    config = _apply_shapefile_input_overrides(config, options)
    return shapefiles.run(config, overpass_json_path=options.overpass_json)


def run_visual_enrichment(
    config: AppConfig,
    options: StageRunOptions,
) -> StageOutput:
    """Run visual enrichment with optional external polygons."""

    return visual_enrichment.run(
        config,
        segmentation_geojson_path=options.segmentation_geojson,
        sat2lod2_geojson_path=options.sat2lod2_geojson,
    )


def run_point_cloud(
    config: AppConfig,
    options: StageRunOptions,
) -> StageOutput:
    """Run point-cloud preparation with CLI input overrides applied."""

    if options.tree_canopy_overlay is not None:
        config = replace(
            config,
            inputs=replace(
                config.inputs,
                tree_canopy_overlay_path=options.tree_canopy_overlay,
            ),
        )
    building_footprints_path = options.building_footprints_geojson
    if (
        building_footprints_path is not None
        and not building_footprints_path.is_absolute()
    ):
        building_footprints_path = (
            config.path.parent / building_footprints_path
        ).resolve()
    return point_cloud.run(
        config,
        building_footprints_path=building_footprints_path,
    )


def run_city_models(
    config: AppConfig,
    options: StageRunOptions,
) -> StageOutput:
    """Run City4CFD with validated CLI configuration overrides."""

    return city_models.run(_apply_city_models_overrides(config, options))


def run_trees(
    config: AppConfig,
    options: StageRunOptions,
) -> StageOutput:
    """Run tree generation with an optional terrain override."""

    if options.tree_terrain_geometry is not None:
        terrain_geometry_path = options.tree_terrain_geometry
        if not terrain_geometry_path.is_absolute():
            terrain_geometry_path = (
                config.path.parent / terrain_geometry_path
            ).resolve()
        config = replace(
            config,
            inputs=replace(
                config.inputs,
                tree_terrain_geometry_path=terrain_geometry_path,
            ),
        )
    return trees.run(config)


def run_air_purifiers(
    config: AppConfig,
    options: StageRunOptions,
) -> StageOutput:
    """Run air-purifier generation with config-relative overrides."""

    return air_purifiers.run(
        config,
        model_library_path=_resolve_config_relative_path(
            config,
            options.model_library,
        ),
        terrain_geometry_path=_resolve_config_relative_path(
            config,
            options.terrain_geometry,
        ),
    )


def _resolve_config_relative_path(
    config: AppConfig,
    path: Path | None,
) -> Path | None:
    if path is None or path.is_absolute():
        return path
    return (config.path.parent / path).resolve()


def _apply_shapefile_input_overrides(
    config: AppConfig,
    options: StageRunOptions,
) -> AppConfig:
    shapefiles_config = config.shapefiles
    specifications = (
        ("streets", "roads", "street_area"),
        ("green_areas", "green_areas", "green_area"),
    )
    for name, category, group_tag in specifications:
        path = getattr(options, f"{name}_shapefile")
        crs = getattr(options, f"{name}_shapefile_crs")
        existing = next(
            (
                surface
                for surface in shapefiles_config.supplemental
                if surface.name == name
            ),
            None,
        )
        if path is None and crs is None:
            continue
        if path is None:
            if existing is None:
                continue
            path = existing.path
        elif not path.is_absolute():
            path = (config.path.parent / path).resolve()
        surface = SupplementalShapefileConfig(
            name=name,
            path=path,
            crs=(
                crs
                or (existing.crs if existing is not None else config.region.crs)
            ).upper(),
            category=category,
            group_tag=group_tag,
            enabled=True,
        )
        surfaces = [
            item
            for item in shapefiles_config.supplemental
            if item.name != name
        ]
        surfaces.append(surface)
        precedence = list(shapefiles_config.surface_precedence)
        selector = f"supplemental:{name}"
        if selector not in precedence:
            if category in precedence:
                precedence.insert(precedence.index(category), selector)
            else:
                precedence.append(selector)
        shapefiles_config = replace(
            shapefiles_config,
            supplemental=tuple(surfaces),
            surface_precedence=tuple(precedence),
        )
    return replace(config, shapefiles=shapefiles_config)


def _apply_city_models_overrides(
    config: AppConfig,
    options: StageRunOptions,
) -> AppConfig:
    city_models_config = config.city_models
    smooth_terrain = city_models_config.smooth_terrain
    reconstruction_region = city_models_config.reconstruction_region
    filters = city_models_config.filters

    if options.city_models_lod is not None:
        city_models_config = replace(
            city_models_config,
            lod=options.city_models_lod,
        )
    if options.city_models_top_height is not None:
        city_models_config = replace(
            city_models_config,
            top_height=options.city_models_top_height,
        )
    if options.city_models_bnd_type_bpg is not None:
        city_models_config = replace(
            city_models_config,
            bnd_type_bpg=options.city_models_bnd_type_bpg,
        )
    if options.city_models_bpg_blockage_ratio is not None:
        city_models_config = replace(
            city_models_config,
            bpg_blockage_ratio=options.city_models_bpg_blockage_ratio,
        )
    if options.city_models_flow_direction is not None:
        city_models_config = replace(
            city_models_config,
            flow_direction=options.city_models_flow_direction,
        )
    if options.city_models_buffer_region is not None:
        city_models_config = replace(
            city_models_config,
            buffer_region=options.city_models_buffer_region,
        )
    if options.city_models_reconstruct_boundaries is not None:
        city_models_config = replace(
            city_models_config,
            reconstruct_boundaries=options.city_models_reconstruct_boundaries,
        )
    if options.city_models_terrain_thinning is not None:
        city_models_config = replace(
            city_models_config,
            terrain_thinning=options.city_models_terrain_thinning,
        )
    if options.city_models_smooth_terrain_iterations is not None:
        smooth_terrain = replace(
            smooth_terrain,
            iterations=options.city_models_smooth_terrain_iterations,
        )
    if options.city_models_smooth_terrain_max_pts is not None:
        smooth_terrain = replace(
            smooth_terrain,
            max_pts=options.city_models_smooth_terrain_max_pts,
        )
    if options.city_models_building_percentile is not None:
        city_models_config = replace(
            city_models_config,
            building_percentile=options.city_models_building_percentile,
        )
    if options.city_models_edge_max_len is not None:
        city_models_config = replace(
            city_models_config,
            edge_max_len=options.city_models_edge_max_len,
        )
    if options.city_models_reconstruction_influence_region is not None:
        reconstruction_region = replace(
            reconstruction_region,
            influence_region_m=options.city_models_reconstruction_influence_region,
        )
    if options.city_models_reconstruction_complexity_factor is not None:
        reconstruction_region = replace(
            reconstruction_region,
            complexity_factor=options.city_models_reconstruction_complexity_factor,
        )
    if options.city_models_reconstruction_validate is not None:
        reconstruction_region = replace(
            reconstruction_region,
            validate=options.city_models_reconstruction_validate,
        )
    if options.city_models_filters_min_area is not None:
        filters = replace(filters, min_area=options.city_models_filters_min_area)
    if options.city_models_filters_min_height is not None:
        filters = replace(filters, min_height=options.city_models_filters_min_height)
    if options.city_models_output_file_name is not None:
        city_models_config = replace(
            city_models_config,
            output_file_name=options.city_models_output_file_name,
        )
    if options.city_models_output_format is not None:
        city_models_config = replace(
            city_models_config,
            output_format=options.city_models_output_format,
        )
    if options.city_models_output_separately is not None:
        city_models_config = replace(
            city_models_config,
            output_separately=options.city_models_output_separately,
        )
    if options.city_models_output_log is not None:
        city_models_config = replace(
            city_models_config,
            output_log=options.city_models_output_log,
        )
    if options.city_models_log_file is not None:
        city_models_config = replace(
            city_models_config,
            log_file=options.city_models_log_file,
        )
    if options.city_models_docker_image is not None:
        city_models_config = replace(
            city_models_config,
            docker_image=options.city_models_docker_image,
        )

    city_models_config = replace(
        city_models_config,
        smooth_terrain=smooth_terrain,
        reconstruction_region=reconstruction_region,
        filters=filters,
    )
    return validate_config(replace(config, city_models=city_models_config))
