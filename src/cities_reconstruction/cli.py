"""Command line interface for the cities reconstruction application."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import (
    AppConfig,
    ConfigError,
    load_config,
)
from .pipeline import (
    EXECUTABLE_STAGE_NAMES,
    OPTIONAL_STAGE_NAMES,
    STAGE_BY_NAME,
    STAGE_NAMES,
    STAGE_SPECS,
    dry_run,
)
from .pipeline_execution import (
    ExecutionPlan,
    PipelineExecution,
    execute_pipeline,
    resolve_execution_plan,
)
from .stage_contract import StageOutput, StageStatus
from .stage_result import StageResult
from .stage_runtime import StageRunOptions


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
    _add_execution_arguments(run_stage)
    run_stage.add_argument(
        "--json",
        action="store_true",
        help="Emit execution summary as JSON.",
    )

    run = subparsers.add_parser(
        "run",
        help="Run the default pipeline or a dependency-aware target.",
    )
    _add_config_argument(run)
    run.add_argument(
        "--target",
        choices=EXECUTABLE_STAGE_NAMES,
        help="Run one executable stage and its required dependency closure.",
    )
    run.add_argument(
        "--include",
        action="append",
        choices=OPTIONAL_STAGE_NAMES,
        default=[],
        help="Add an optional stage to the default or targeted execution plan.",
    )
    _add_execution_arguments(run)
    run.add_argument(
        "--json",
        action="store_true",
        help="Emit the aggregate execution summary as JSON.",
    )

    return parser


def _add_execution_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--overpass-json",
        type=Path,
        help="Use a cached Overpass JSON file instead of making a network request.",
    )
    parser.add_argument(
        "--streets-shapefile",
        type=Path,
        help="Override the conventional named supplemental input 'streets' for this shapefiles-stage run.",
    )
    parser.add_argument(
        "--streets-shapefile-crs",
        help="Override the CRS of the conventional named supplemental input 'streets'.",
    )
    parser.add_argument(
        "--green-areas-shapefile",
        type=Path,
        help="Override the conventional named supplemental input 'green_areas' for this shapefiles-stage run.",
    )
    parser.add_argument(
        "--green-areas-shapefile-crs",
        help="Override the CRS of the conventional named supplemental input 'green_areas'.",
    )
    parser.add_argument(
        "--segmentation-geojson",
        type=Path,
        help="Use external segmentation polygons for the visual-enrichment stage.",
    )
    parser.add_argument(
        "--sat2lod2-geojson",
        type=Path,
        help="Use external SAT2LoD2/LOD2BuildingModel 2D building polygons for the visual-enrichment stage.",
    )
    parser.add_argument(
        "--tree-canopy-overlay",
        type=Path,
        help=(
            "Override inputs.tree_canopy_overlay_path for the point-cloud stage. "
            "When omitted and not configured in TOML, tree-point filtering is skipped."
        ),
    )
    parser.add_argument(
        "--building-footprints-geojson",
        type=Path,
        help=(
            "Explicitly override the Stage 1 building-footprint GeoJSON for this point-cloud run. "
            "Relative paths are resolved from the configuration directory."
        ),
    )
    parser.add_argument(
        "--tree-terrain-geometry",
        type=Path,
        help=(
            "Override inputs.tree_terrain_geometry_path for the trees stage. "
            "The OBJ or ASCII STL geometry must use the local City4CFD coordinate frame."
        ),
    )
    parser.add_argument(
        "--model-library",
        type=Path,
        help=(
            "Override air_purifiers.model_library_path for the air-purifiers stage. "
            "Relative paths are resolved from the configuration directory."
        ),
    )
    parser.add_argument(
        "--terrain-geometry",
        type=Path,
        help=(
            "Override air_purifiers.terrain_geometry_path for the air-purifiers stage. "
            "Relative paths are resolved from the configuration directory."
        ),
    )
    _add_city_models_arguments(parser)


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

    if args.command == "run":
        try:
            plan = resolve_execution_plan(
                target=args.target,
                includes=args.include,
                supplied_overrides=_supplied_registry_overrides(args),
            )
            validation_error = _validate_plan_scoped_options(args, plan)
            if validation_error is not None:
                print(validation_error, file=sys.stderr)
                return 2
            _emit_execution_plan(plan, as_json=args.json)
            execution = execute_pipeline(config, plan, _stage_run_options(args))
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        _emit_pipeline_execution(execution, as_json=args.json)
        return 0 if execution.completed else 1

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
            runner = STAGE_BY_NAME[args.stage].runner
            if runner is None:
                parser.error(f"Stage is not executable: {args.stage}")
            result = runner(config, _stage_run_options(args))
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


def _stage_run_options(args: argparse.Namespace) -> StageRunOptions:
    flow_direction = (
        None
        if args.city_models_flow_direction is None
        else tuple(args.city_models_flow_direction)
    )
    return StageRunOptions(
        overpass_json=args.overpass_json,
        streets_shapefile=args.streets_shapefile,
        streets_shapefile_crs=args.streets_shapefile_crs,
        green_areas_shapefile=args.green_areas_shapefile,
        green_areas_shapefile_crs=args.green_areas_shapefile_crs,
        segmentation_geojson=args.segmentation_geojson,
        sat2lod2_geojson=args.sat2lod2_geojson,
        tree_canopy_overlay=args.tree_canopy_overlay,
        building_footprints_geojson=args.building_footprints_geojson,
        tree_terrain_geometry=args.tree_terrain_geometry,
        model_library=args.model_library,
        terrain_geometry=args.terrain_geometry,
        city_models_lod=args.city_models_lod,
        city_models_top_height=args.city_models_top_height,
        city_models_bnd_type_bpg=args.city_models_bnd_type_bpg,
        city_models_bpg_blockage_ratio=args.city_models_bpg_blockage_ratio,
        city_models_flow_direction=flow_direction,
        city_models_buffer_region=args.city_models_buffer_region,
        city_models_reconstruct_boundaries=args.city_models_reconstruct_boundaries,
        city_models_terrain_thinning=args.city_models_terrain_thinning,
        city_models_smooth_terrain_iterations=(
            args.city_models_smooth_terrain_iterations
        ),
        city_models_smooth_terrain_max_pts=args.city_models_smooth_terrain_max_pts,
        city_models_building_percentile=args.city_models_building_percentile,
        city_models_edge_max_len=args.city_models_edge_max_len,
        city_models_reconstruction_influence_region=(
            args.city_models_reconstruction_influence_region
        ),
        city_models_reconstruction_complexity_factor=(
            args.city_models_reconstruction_complexity_factor
        ),
        city_models_reconstruction_validate=(
            args.city_models_reconstruction_validate
        ),
        city_models_filters_min_area=args.city_models_filters_min_area,
        city_models_filters_min_height=args.city_models_filters_min_height,
        city_models_output_file_name=args.city_models_output_file_name,
        city_models_output_format=args.city_models_output_format,
        city_models_output_separately=args.city_models_output_separately,
        city_models_output_log=args.city_models_output_log,
        city_models_log_file=args.city_models_log_file,
        city_models_docker_image=args.city_models_docker_image,
    )


def _supplied_registry_overrides(args: argparse.Namespace) -> frozenset[str]:
    supplied: set[str] = set()
    for spec in STAGE_SPECS:
        for input_spec in spec.inputs:
            override = input_spec.override
            if override is None or not override.startswith("--"):
                continue
            destination = override.removeprefix("--").replace("-", "_")
            if getattr(args, destination, None) is not None:
                supplied.add(override)
    return frozenset(supplied)


def _validate_plan_scoped_options(
    args: argparse.Namespace,
    plan: ExecutionPlan,
) -> str | None:
    stage_names = frozenset(plan.stage_names)
    if args.model_library is not None and "air-purifiers" not in stage_names:
        return "--model-library requires air-purifiers in the execution plan"
    if args.terrain_geometry is not None and "air-purifiers" not in stage_names:
        return "--terrain-geometry requires air-purifiers in the execution plan"
    if (
        args.building_footprints_geojson is not None
        and "point-cloud" not in stage_names
    ):
        return "--building-footprints-geojson requires point-cloud in the execution plan"
    return None


def _emit_execution_plan(plan: ExecutionPlan, *, as_json: bool) -> None:
    destination = sys.stderr if as_json else sys.stdout
    print(f"Execution plan: {' -> '.join(plan.stage_names)}", file=destination)


def _emit_pipeline_execution(
    execution: PipelineExecution,
    *,
    as_json: bool,
) -> None:
    if as_json:
        print(json.dumps(execution.to_dict(), indent=2))
        return
    for result in execution.results:
        print(result.report_path.read_text(encoding="utf-8").rstrip())


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
