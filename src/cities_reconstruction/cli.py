"""Command line interface for the cities reconstruction application."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from .config import (
    AppConfig,
    ConfigError,
    SupplementalShapefileConfig,
    load_config,
    validate_config,
)
from .pipeline import EXECUTABLE_STAGE_NAMES, STAGE_NAMES, dry_run
from .stage_contract import StageOutput, StageStatus
from .stage_result import StageResult
from .stages import air_purifiers, city_models, point_cloud, shapefiles, trees, visual_enrichment

RunStageHandler = Callable[[AppConfig, argparse.Namespace], StageOutput]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cities-reconstruction",
        description="Plan city reconstruction workflows for CFD-ready OpenFOAM domains.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate-config",
        help="Validate a TOML configuration file.",
    )
    _add_config_argument(validate)

    dry = subparsers.add_parser(
        "dry-run",
        help="Print the planned pipeline without running external tools.",
    )
    _add_config_argument(dry)
    dry.add_argument(
        "--stage",
        choices=["all", *STAGE_NAMES],
        default="all",
        help="Limit the dry-run to a single pipeline stage.",
    )
    dry.add_argument(
        "--json",
        action="store_true",
        help="Emit dry-run output as JSON.",
    )

    run_stage = subparsers.add_parser(
        "run-stage",
        help="Run one implemented pipeline stage.",
    )
    _add_config_argument(run_stage)
    run_stage.add_argument(
        "stage",
        choices=EXECUTABLE_STAGE_NAMES,
        help="Stage to execute.",
    )
    run_stage.add_argument(
        "--overpass-json",
        type=Path,
        help="Use a cached Overpass JSON file instead of making a network request.",
    )
    run_stage.add_argument(
        "--streets-shapefile",
        type=Path,
        help="Override the conventional named supplemental input 'streets' for this shapefiles-stage run.",
    )
    run_stage.add_argument(
        "--streets-shapefile-crs",
        help="Override the CRS of the conventional named supplemental input 'streets'.",
    )
    run_stage.add_argument(
        "--green-areas-shapefile",
        type=Path,
        help="Override the conventional named supplemental input 'green_areas' for this shapefiles-stage run.",
    )
    run_stage.add_argument(
        "--green-areas-shapefile-crs",
        help="Override the CRS of the conventional named supplemental input 'green_areas'.",
    )
    run_stage.add_argument(
        "--segmentation-geojson",
        type=Path,
        help="Use external segmentation polygons for the visual-enrichment stage.",
    )
    run_stage.add_argument(
        "--sat2lod2-geojson",
        type=Path,
        help="Use external SAT2LoD2/LOD2BuildingModel 2D building polygons for the visual-enrichment stage.",
    )
    run_stage.add_argument(
        "--tree-canopy-overlay",
        type=Path,
        help=(
            "Override inputs.tree_canopy_overlay_path for the point-cloud stage. "
            "When omitted and not configured in TOML, tree-point filtering is skipped."
        ),
    )
    run_stage.add_argument(
        "--building-footprints-geojson",
        type=Path,
        help=(
            "Explicitly override the Stage 1 building-footprint GeoJSON for this point-cloud run. "
            "Relative paths are resolved from the configuration directory."
        ),
    )
    run_stage.add_argument(
        "--tree-terrain-geometry",
        type=Path,
        help=(
            "Override inputs.tree_terrain_geometry_path for the trees stage. "
            "The OBJ or ASCII STL geometry must use the local City4CFD coordinate frame."
        ),
    )
    run_stage.add_argument(
        "--model-library",
        type=Path,
        help=(
            "Override air_purifiers.model_library_path for the air-purifiers stage. "
            "Relative paths are resolved from the configuration directory."
        ),
    )
    run_stage.add_argument(
        "--terrain-geometry",
        type=Path,
        help=(
            "Override air_purifiers.terrain_geometry_path for the air-purifiers stage. "
            "Relative paths are resolved from the configuration directory."
        ),
    )
    _add_city_models_arguments(run_stage)
    run_stage.add_argument(
        "--json",
        action="store_true",
        help="Emit execution summary as JSON.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "validate-config":
        inner_description = (
            f"inner {config.region.inner_diameter_m:g} m, "
            if config.region.inner_diameter_m is not None
            else "no inner diameter, "
        )
        print(
            "Configuration is valid: "
            f"{config.region.name} ({config.region.crs}, "
            f"{inner_description}"
            f"outer {config.region.outer_diameter_m:g} m)"
        )
        return 0

    if args.command == "dry-run":
        stages = None if args.stage == "all" else [args.stage]
        results = dry_run(config, stages=stages)
        if args.json:
            print(json.dumps([result.to_dict() for result in results], indent=2))
        else:
            _print_dry_run(config, results)
        return 0

    if args.command == "run-stage":
        if args.model_library is not None and args.stage != "air-purifiers":
            print("--model-library is valid only for the air-purifiers stage", file=sys.stderr)
            return 2
        if args.terrain_geometry is not None and args.stage != "air-purifiers":
            print("--terrain-geometry is valid only for the air-purifiers stage", file=sys.stderr)
            return 2
        if args.building_footprints_geojson is not None and args.stage != "point-cloud":
            print("--building-footprints-geojson is valid only for the point-cloud stage", file=sys.stderr)
            return 2
        try:
            result = RUN_STAGE_HANDLERS[args.stage](config, args)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        _emit_stage_result(result, as_json=args.json)
        return _stage_exit_code(result)
    parser.error(f"Unhandled command: {args.command}")
    return 2


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        required=True,
        help="Path to a user-provided TOML configuration file.",
    )


def _run_shapefiles(
    config: AppConfig,
    args: argparse.Namespace,
) -> StageOutput:
    config = _apply_shapefile_input_overrides(config, args)
    return shapefiles.run(config, overpass_json_path=args.overpass_json)


def _run_visual_enrichment(
    config: AppConfig,
    args: argparse.Namespace,
) -> StageOutput:
    return visual_enrichment.run(
        config,
        segmentation_geojson_path=args.segmentation_geojson,
        sat2lod2_geojson_path=args.sat2lod2_geojson,
    )


def _run_point_cloud(
    config: AppConfig,
    args: argparse.Namespace,
) -> StageOutput:
    if args.tree_canopy_overlay is not None:
        config = replace(
            config,
            inputs=replace(
                config.inputs,
                tree_canopy_overlay_path=args.tree_canopy_overlay,
            ),
        )
    building_footprints_path = args.building_footprints_geojson
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


def _run_city_models(
    config: AppConfig,
    args: argparse.Namespace,
) -> StageOutput:
    return city_models.run(_apply_city_models_overrides(config, args))


def _run_trees(
    config: AppConfig,
    args: argparse.Namespace,
) -> StageOutput:
    if args.tree_terrain_geometry is not None:
        terrain_geometry_path = args.tree_terrain_geometry
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


def _run_air_purifiers(
    config: AppConfig,
    args: argparse.Namespace,
) -> StageOutput:
    return air_purifiers.run(
        config,
        model_library_path=_resolve_config_relative_path(config, args.model_library),
        terrain_geometry_path=_resolve_config_relative_path(
            config,
            args.terrain_geometry,
        ),
    )


RUN_STAGE_HANDLERS: dict[str, RunStageHandler] = dict(
    zip(
        EXECUTABLE_STAGE_NAMES,
        (
            _run_shapefiles,
            _run_visual_enrichment,
            _run_point_cloud,
            _run_city_models,
            _run_trees,
            _run_air_purifiers,
        ),
        strict=True,
    )
)


def _emit_stage_result(
    result: StageOutput,
    *,
    as_json: bool,
) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(result.report_path.read_text(encoding="utf-8").rstrip())


def _stage_exit_code(result: StageOutput) -> int:
    return 0 if result.status is StageStatus.COMPLETED else 1


def _resolve_config_relative_path(
    config: AppConfig,
    path: Path | None,
) -> Path | None:
    if path is None or path.is_absolute():
        return path
    return (config.path.parent / path).resolve()


def _apply_shapefile_input_overrides(
    config: AppConfig,
    args: argparse.Namespace,
) -> AppConfig:
    shapefiles_config = config.shapefiles
    specifications = (
        ("streets", "roads", "street_area"),
        ("green_areas", "green_areas", "green_area"),
    )
    for name, category, group_tag in specifications:
        path = getattr(args, f"{name}_shapefile")
        crs = getattr(args, f"{name}_shapefile_crs")
        existing = next((surface for surface in shapefiles_config.supplemental if surface.name == name), None)
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
            crs=(crs or (existing.crs if existing is not None else config.region.crs)).upper(),
            category=category,
            group_tag=group_tag,
            enabled=True,
        )
        surfaces = [item for item in shapefiles_config.supplemental if item.name != name]
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


def _add_city_models_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--city-models-lod",
        help="Override the City4CFD reconstruction LOD.",
    )
    parser.add_argument(
        "--city-models-top-height",
        type=float,
        help="Override the City4CFD domain top height.",
    )
    parser.add_argument(
        "--city-models-bnd-type-bpg",
        help="Override the City4CFD boundary type for the building-point-generation domain.",
    )
    parser.add_argument(
        "--city-models-bpg-blockage-ratio",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable City4CFD blockage-ratio handling.",
    )
    parser.add_argument(
        "--city-models-flow-direction",
        nargs=2,
        type=float,
        metavar=("X", "Y"),
        help="Override the City4CFD flow direction vector.",
    )
    parser.add_argument(
        "--city-models-buffer-region",
        type=float,
        help="Override the City4CFD buffer region.",
    )
    parser.add_argument(
        "--city-models-reconstruct-boundaries",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable City4CFD boundary reconstruction.",
    )
    parser.add_argument(
        "--city-models-terrain-thinning",
        type=float,
        help="Override the City4CFD terrain thinning distance.",
    )
    parser.add_argument(
        "--city-models-smooth-terrain-iterations",
        type=int,
        help="Override the number of City4CFD terrain smoothing iterations.",
    )
    parser.add_argument(
        "--city-models-smooth-terrain-max-pts",
        type=int,
        help="Override the City4CFD terrain smoothing point limit.",
    )
    parser.add_argument(
        "--city-models-building-percentile",
        type=float,
        help="Override the City4CFD building elevation percentile.",
    )
    parser.add_argument(
        "--city-models-edge-max-len",
        type=float,
        help="Override the City4CFD maximum edge length.",
    )
    parser.add_argument(
        "--city-models-reconstruction-influence-region",
        type=float,
        help="Override the City4CFD reconstruction influence region radius.",
    )
    parser.add_argument(
        "--city-models-reconstruction-complexity-factor",
        type=float,
        help="Override the City4CFD reconstruction complexity factor.",
    )
    parser.add_argument(
        "--city-models-reconstruction-validate",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable City4CFD reconstruction validation.",
    )
    parser.add_argument(
        "--city-models-filters-min-area",
        type=float,
        help="Override the minimum filtered polygon area.",
    )
    parser.add_argument(
        "--city-models-filters-min-height",
        type=float,
        help="Override the minimum filtered polygon height.",
    )
    parser.add_argument(
        "--city-models-output-file-name",
        help="Override the City4CFD output file base name.",
    )
    parser.add_argument(
        "--city-models-output-format",
        help="Override the City4CFD output mesh format.",
    )
    parser.add_argument(
        "--city-models-output-separately",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable separate City4CFD outputs.",
    )
    parser.add_argument(
        "--city-models-output-log",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable City4CFD log output.",
    )
    parser.add_argument(
        "--city-models-log-file",
        help="Override the City4CFD log file name.",
    )
    parser.add_argument(
        "--city-models-docker-image",
        help="Override the Docker image used by the City4CFD fallback script.",
    )


def _apply_city_models_overrides(
    config: AppConfig,
    args: argparse.Namespace,
) -> AppConfig:
    city_models_config = config.city_models
    smooth_terrain = city_models_config.smooth_terrain
    reconstruction_region = city_models_config.reconstruction_region
    filters = city_models_config.filters

    if args.city_models_lod is not None:
        city_models_config = replace(city_models_config, lod=args.city_models_lod)
    if args.city_models_top_height is not None:
        city_models_config = replace(city_models_config, top_height=args.city_models_top_height)
    if args.city_models_bnd_type_bpg is not None:
        city_models_config = replace(city_models_config, bnd_type_bpg=args.city_models_bnd_type_bpg)
    if args.city_models_bpg_blockage_ratio is not None:
        city_models_config = replace(city_models_config, bpg_blockage_ratio=args.city_models_bpg_blockage_ratio)
    if args.city_models_flow_direction is not None:
        city_models_config = replace(city_models_config, flow_direction=tuple(args.city_models_flow_direction))
    if args.city_models_buffer_region is not None:
        city_models_config = replace(city_models_config, buffer_region=args.city_models_buffer_region)
    if args.city_models_reconstruct_boundaries is not None:
        city_models_config = replace(
            city_models_config,
            reconstruct_boundaries=args.city_models_reconstruct_boundaries,
        )
    if args.city_models_terrain_thinning is not None:
        city_models_config = replace(city_models_config, terrain_thinning=args.city_models_terrain_thinning)
    if args.city_models_smooth_terrain_iterations is not None:
        smooth_terrain = replace(smooth_terrain, iterations=args.city_models_smooth_terrain_iterations)
    if args.city_models_smooth_terrain_max_pts is not None:
        smooth_terrain = replace(smooth_terrain, max_pts=args.city_models_smooth_terrain_max_pts)
    if args.city_models_building_percentile is not None:
        city_models_config = replace(city_models_config, building_percentile=args.city_models_building_percentile)
    if args.city_models_edge_max_len is not None:
        city_models_config = replace(city_models_config, edge_max_len=args.city_models_edge_max_len)
    if args.city_models_reconstruction_influence_region is not None:
        reconstruction_region = replace(
            reconstruction_region,
            influence_region_m=args.city_models_reconstruction_influence_region,
        )
    if args.city_models_reconstruction_complexity_factor is not None:
        reconstruction_region = replace(
            reconstruction_region,
            complexity_factor=args.city_models_reconstruction_complexity_factor,
        )
    if args.city_models_reconstruction_validate is not None:
        reconstruction_region = replace(reconstruction_region, validate=args.city_models_reconstruction_validate)
    if args.city_models_filters_min_area is not None:
        filters = replace(filters, min_area=args.city_models_filters_min_area)
    if args.city_models_filters_min_height is not None:
        filters = replace(filters, min_height=args.city_models_filters_min_height)
    if args.city_models_output_file_name is not None:
        city_models_config = replace(city_models_config, output_file_name=args.city_models_output_file_name)
    if args.city_models_output_format is not None:
        city_models_config = replace(city_models_config, output_format=args.city_models_output_format)
    if args.city_models_output_separately is not None:
        city_models_config = replace(city_models_config, output_separately=args.city_models_output_separately)
    if args.city_models_output_log is not None:
        city_models_config = replace(city_models_config, output_log=args.city_models_output_log)
    if args.city_models_log_file is not None:
        city_models_config = replace(city_models_config, log_file=args.city_models_log_file)
    if args.city_models_docker_image is not None:
        city_models_config = replace(city_models_config, docker_image=args.city_models_docker_image)

    city_models_config = replace(
        city_models_config,
        smooth_terrain=smooth_terrain,
        reconstruction_region=reconstruction_region,
        filters=filters,
    )
    return validate_config(replace(config, city_models=city_models_config))


def _print_dry_run(
    config: AppConfig,
    results: Sequence[StageResult],
) -> None:
    print(f"Dry run for {config.region.name}")
    print(f"CRS: {config.region.crs}")
    inner_description = (
        f"inner {config.region.inner_diameter_m:g} m, "
        if config.region.inner_diameter_m is not None
        else "no inner diameter, "
    )
    print(
        "Region: "
        f"lat {config.region.center_lat:g}, lon {config.region.center_lon:g}, "
        f"{inner_description}"
        f"outer {config.region.outer_diameter_m:g} m"
    )
    print()

    for result in results:
        print(f"[{result.stage}] {result.summary}")
        for action in result.planned_actions:
            print(f"  - {action}")
        if result.expected_outputs:
            print("  expected outputs:")
            for output in result.expected_outputs:
                print(f"    - {output}")
        print()
