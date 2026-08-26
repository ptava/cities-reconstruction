"""TOML configuration loading and validation."""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from .errors import ConfigError

_TomlTable: TypeAlias = Mapping[str, object]


@dataclass(frozen=True)
class RegionConfig:
    name: str
    center_lat: float
    center_lon: float
    crs: str
    inner_diameter_m: float | None
    outer_diameter_m: float


@dataclass(frozen=True)
class InputConfig:
    overpass_url: str
    overpass_timeout_s: float
    overpass_max_attempts: int
    overpass_retry_backoff_s: float
    dtm_directory: Path | None
    dsm_directory: Path | None
    point_cloud_path: Path | None
    tree_overlap_tolerance_m: float
    tree_canopy_overlay_path: Path | None
    tree_terrain_geometry_path: Path | None


@dataclass(frozen=True)
class FeatureClassificationRule:
    category: str
    group_tag: str
    match_any: tuple[str, ...]


@dataclass(frozen=True)
class SupplementalShapefileConfig:
    name: str
    path: Path
    crs: str
    category: str
    group_tag: str | None
    enabled: bool


@dataclass(frozen=True)
class UrbanPlanningInputConfig:
    name: str
    path: Path
    crs: str
    enabled: bool


@dataclass(frozen=True)
class UrbanPlanningConfig:
    inputs: tuple[UrbanPlanningInputConfig, ...]


@dataclass(frozen=True)
class ShapefilesConfig:
    classification_rules: tuple[FeatureClassificationRule, ...]
    surface_precedence: tuple[str, ...]
    supplemental: tuple[SupplementalShapefileConfig, ...]


@dataclass(frozen=True)
class TreeConfig:
    default: str
    model_library_path: Path
    category_mapping_path: Path | None


@dataclass(frozen=True)
class AirPurifiersConfig:
    model_library_path: Path | None
    terrain_geometry_path: Path | None


@dataclass(frozen=True)
class OutputConfig:
    root_directory: Path


@dataclass(frozen=True)
class ImagerySourceConfig:
    name: str
    type: str
    url: str
    layer: str
    enabled: bool
    crs: str
    format: str
    width: int
    height: int
    style: str
    transparent: bool


@dataclass(frozen=True)
class ImageryConfig:
    sources: tuple[ImagerySourceConfig, ...]


@dataclass(frozen=True)
class CityModelsSmoothTerrainConfig:
    iterations: int
    max_pts: int


@dataclass(frozen=True)
class CityModelsReconstructionRegionConfig:
    influence_region_m: float | None
    complexity_factor: float
    validate: bool


@dataclass(frozen=True)
class CityModelsFilterConfig:
    min_area: float
    min_height: float


@dataclass(frozen=True)
class CityModelsConfig:
    lod: str
    domain_bnd: float | None
    building_roof_default_base_height_m: float
    top_height: float
    bnd_type_bpg: str
    bpg_blockage_ratio: bool
    flow_direction: tuple[float, float]
    buffer_region: float
    reconstruct_boundaries: bool
    terrain_thinning: float
    smooth_terrain: CityModelsSmoothTerrainConfig
    building_percentile: float
    edge_max_len: float
    reconstruction_region: CityModelsReconstructionRegionConfig
    filters: CityModelsFilterConfig
    output_file_name: str
    output_format: str
    output_separately: bool
    output_log: bool
    log_file: str
    docker_image: str | None


@dataclass(frozen=True)
class AppConfig:
    path: Path
    region: RegionConfig
    inputs: InputConfig
    shapefiles: ShapefilesConfig
    urban_planning: UrbanPlanningConfig
    trees: TreeConfig
    air_purifiers: AirPurifiersConfig
    output: OutputConfig
    imagery: ImageryConfig
    city_models: CityModelsConfig


def load_config(path: str | Path) -> AppConfig:
    """Load and validate an application configuration file."""

    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"configuration file does not exist: {config_path}")

    try:
        with config_path.open("rb") as handle:
            raw_value: object = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    raw = _validated_table(raw_value, "configuration root must be a TOML table")

    _reject_unknown_keys(
        raw,
        {
            "region",
            "inputs",
            "shapefiles",
            "urban_planning",
            "trees",
            "air_purifiers",
            "output",
            "imagery",
            "city_models",
        },
        "",
    )
    base_dir = config_path.parent
    region = _parse_region(_required_table(raw, "region"))
    inputs = _parse_inputs(_required_table(raw, "inputs"), base_dir)
    shapefiles = _parse_shapefiles(_required_table(raw, "shapefiles"), base_dir)
    urban_planning = _parse_urban_planning(raw.get("urban_planning", {}), base_dir)
    trees = _parse_trees(_required_table(raw, "trees"), base_dir)
    air_purifiers = _parse_air_purifiers(raw.get("air_purifiers", {}), base_dir)
    output = _parse_output(_required_table(raw, "output"), base_dir)
    imagery = _parse_imagery(_required_table(raw, "imagery"))
    city_models = _parse_city_models(_required_table(raw, "city_models"))

    return validate_config(AppConfig(
        path=config_path,
        region=region,
        inputs=inputs,
        shapefiles=shapefiles,
        urban_planning=urban_planning,
        trees=trees,
        air_purifiers=air_purifiers,
        output=output,
        imagery=imagery,
        city_models=city_models,
    ))


def validate_config(config: AppConfig) -> AppConfig:
    """Validate an assembled config, including programmatic replacements."""

    validate_city_models_config(config.city_models)
    return config


def validate_city_models_config(config: CityModelsConfig) -> CityModelsConfig:
    """Apply the shared City4CFD value contract to TOML and CLI values."""

    _require_choice("city_models.lod", config.lod, {"1.2", "1.3", "2.2"})
    _require_choice(
        "city_models.bnd_type_bpg",
        config.bnd_type_bpg,
        {"Rectangle", "Round", "Oval"},
    )
    _require_choice(
        "city_models.output_format",
        config.output_format,
        {"obj", "stl", "cityjson"},
    )
    _require_filename("city_models.output_file_name", config.output_file_name)
    _require_filename("city_models.log_file", config.log_file)
    validate_city4cfd_docker_image(config.docker_image)

    if config.domain_bnd is not None:
        _require_positive("city_models.domain_bnd", config.domain_bnd)
    _require_nonnegative(
        "city_models.building_roof_default_base_height_m",
        config.building_roof_default_base_height_m,
    )
    _require_positive("city_models.top_height", config.top_height)
    _require_finite("city_models.buffer_region", config.buffer_region)
    _require_range("city_models.terrain_thinning", config.terrain_thinning, 0.0, 100.0)
    _require_range("city_models.building_percentile", config.building_percentile, 0.0, 100.0)
    _require_positive("city_models.edge_max_len", config.edge_max_len)
    _require_range(
        "city_models.reconstruction_region.complexity_factor",
        config.reconstruction_region.complexity_factor,
        0.0,
        1.0,
    )
    influence = config.reconstruction_region.influence_region_m
    if influence is None:
        raise ConfigError(
            "city_models.reconstruction_region.influence_region_m must be positive"
        )
    _require_positive("city_models.reconstruction_region.influence_region_m", influence)
    _require_positive("city_models.filters.min_area", config.filters.min_area)
    _require_positive("city_models.filters.min_height", config.filters.min_height)
    _require_positive_integer(
        "city_models.smooth_terrain.iterations",
        config.smooth_terrain.iterations,
    )
    _require_positive_integer(
        "city_models.smooth_terrain.max_pts",
        config.smooth_terrain.max_pts,
    )

    if len(config.flow_direction) != 2:
        raise ConfigError("city_models.flow_direction must contain exactly two numbers")
    flow_x, flow_y = config.flow_direction
    _require_finite("city_models.flow_direction[0]", flow_x)
    _require_finite("city_models.flow_direction[1]", flow_y)
    flow_required = config.bnd_type_bpg in {"Rectangle", "Oval"} or (
        config.bnd_type_bpg == "Round" and config.bpg_blockage_ratio
    )
    if flow_required and flow_x == 0.0 and flow_y == 0.0:
        raise ConfigError(
            "city_models.flow_direction must be non-zero for Rectangle/Oval domains "
            "and Round domains with blockage-ratio handling enabled"
        )
    return config


def validate_city4cfd_docker_image(value: str | None) -> str | None:
    """Validate an image selected from TOML, CLI, or the environment."""

    if value is not None:
        _require_text("city_models.docker_image", value)
        if value.startswith("-"):
            raise ConfigError("city_models.docker_image must not begin with '-'")
    return value


def _require_choice(name: str, value: str, choices: set[str]) -> None:
    _require_text(name, value)
    if value not in choices:
        rendered = ", ".join(sorted(choices))
        raise ConfigError(f"{name} must be one of: {rendered}")


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")


def _require_filename(name: str, value: str) -> None:
    _require_text(name, value)
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ConfigError(f"{name} must be a filename, not a path")


def _require_finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"{name} must be finite")


def _require_positive(name: str, value: float) -> None:
    _require_finite(name, value)
    if value <= 0:
        raise ConfigError(f"{name} must be positive")


def _require_nonnegative(name: str, value: float) -> None:
    _require_finite(name, value)
    if value < 0:
        raise ConfigError(f"{name} must be non-negative")


def _require_positive_integer(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")


def _require_range(name: str, value: float, minimum: float, maximum: float) -> None:
    _require_finite(name, value)
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum:g} and {maximum:g} inclusive")


def _parse_region(table: _TomlTable) -> RegionConfig:
    _reject_unknown_keys(
        table,
        {"name", "center_lat", "center_lon", "crs", "inner_diameter_m", "outer_diameter_m"},
        "region",
    )
    name = _required_str(table, "name", "region")
    center_lat = _required_number(table, "center_lat", "region")
    center_lon = _required_number(table, "center_lon", "region")
    crs = _required_str(table, "crs", "region")
    inner_diameter_m = _optional_number(table, "inner_diameter_m", "region")
    outer_diameter_m = _required_number(table, "outer_diameter_m", "region")

    if not -90.0 <= center_lat <= 90.0:
        raise ConfigError("region.center_lat must be between -90 and 90")
    if not -180.0 <= center_lon <= 180.0:
        raise ConfigError("region.center_lon must be between -180 and 180")
    if inner_diameter_m is not None and inner_diameter_m <= 0:
        raise ConfigError("region.inner_diameter_m must be positive")
    if outer_diameter_m <= 0:
        raise ConfigError("region.outer_diameter_m must be positive")
    if inner_diameter_m is not None and outer_diameter_m < inner_diameter_m:
        raise ConfigError("region.outer_diameter_m must be greater than or equal to inner_diameter_m")
    if not crs:
        raise ConfigError("region.crs must not be empty")

    return RegionConfig(
        name=name,
        center_lat=center_lat,
        center_lon=center_lon,
        crs=crs,
        inner_diameter_m=inner_diameter_m,
        outer_diameter_m=outer_diameter_m,
    )


def _parse_inputs(table: _TomlTable, base_dir: Path) -> InputConfig:
    _reject_unknown_keys(
        table,
        {
            "overpass_url",
            "overpass_timeout_s",
            "overpass_max_attempts",
            "overpass_retry_backoff_s",
            "dtm_directory",
            "dsm_directory",
            "point_cloud_path",
            "tree_overlap_tolerance_m",
            "tree_canopy_overlay_path",
            "tree_terrain_geometry_path",
        },
        "inputs",
    )
    overpass_url = _required_str(table, "overpass_url", "inputs")
    overpass_timeout_s = _required_number(table, "overpass_timeout_s", "inputs")
    if overpass_timeout_s <= 0:
        raise ConfigError("inputs.overpass_timeout_s must be positive")
    overpass_max_attempts = table.get("overpass_max_attempts", 3)
    if (
        isinstance(overpass_max_attempts, bool)
        or not isinstance(overpass_max_attempts, int)
        or overpass_max_attempts <= 0
    ):
        raise ConfigError("inputs.overpass_max_attempts must be a positive integer")
    overpass_retry_backoff_s = table.get("overpass_retry_backoff_s", 2.0)
    if (
        isinstance(overpass_retry_backoff_s, bool)
        or not isinstance(overpass_retry_backoff_s, (int, float))
        or not math.isfinite(overpass_retry_backoff_s)
        or overpass_retry_backoff_s < 0
    ):
        raise ConfigError("inputs.overpass_retry_backoff_s must be non-negative")

    tree_overlap_tolerance_m = _required_number(table, "tree_overlap_tolerance_m", "inputs")
    if tree_overlap_tolerance_m < 0:
        raise ConfigError("inputs.tree_overlap_tolerance_m must be non-negative")

    return InputConfig(
        overpass_url=overpass_url,
        overpass_timeout_s=overpass_timeout_s,
        overpass_max_attempts=overpass_max_attempts,
        overpass_retry_backoff_s=float(overpass_retry_backoff_s),
        dtm_directory=_optional_path(table, "dtm_directory", "inputs", base_dir),
        dsm_directory=_optional_path(table, "dsm_directory", "inputs", base_dir),
        point_cloud_path=_optional_path(table, "point_cloud_path", "inputs", base_dir),
        tree_overlap_tolerance_m=tree_overlap_tolerance_m,
        tree_canopy_overlay_path=_optional_path(table, "tree_canopy_overlay_path", "inputs", base_dir),
        tree_terrain_geometry_path=_optional_path(table, "tree_terrain_geometry_path", "inputs", base_dir),
    )


def _parse_shapefiles(table: _TomlTable, base_dir: Path) -> ShapefilesConfig:
    _reject_unknown_keys(
        table,
        {
            "classification_rules",
            "surface_precedence",
            "supplemental",
        },
        "shapefiles",
    )
    raw_rules = _required_list(table, "classification_rules", "shapefiles")
    if not raw_rules:
        raise ConfigError("shapefiles.classification_rules must contain at least one rule")

    supported_categories = {
        "buildings",
        "roads",
        "green_areas",
        "concrete",
        "water",
        "trees",
        "other_terrain",
    }
    rules: list[FeatureClassificationRule] = []
    for index, raw_rule in enumerate(raw_rules, start=1):
        section = f"shapefiles.classification_rules[{index}]"
        raw_rule = _validated_table(raw_rule, f"{section} must be a TOML table")
        _reject_unknown_keys(raw_rule, {"category", "group_tag", "match_any"}, section)
        category = _required_str(raw_rule, "category", section)
        if category not in supported_categories:
            allowed = ", ".join(sorted(supported_categories))
            raise ConfigError(f"{section}.category must be one of: {allowed}")
        match_any = _required_list(raw_rule, "match_any", section)
        if not match_any:
            raise ConfigError(f"{section}.match_any must contain at least one tag expression")
        expressions: list[str] = []
        for expression_index, expression in enumerate(match_any, start=1):
            if not isinstance(expression, str) or not expression:
                raise ConfigError(f"{section}.match_any[{expression_index}] must be a non-empty string")
            key, separator, value = expression.partition("=")
            if not key or (separator and not value):
                raise ConfigError(
                    f"{section}.match_any[{expression_index}] must be 'key' or 'key=value'"
                )
            expressions.append(expression)
        rules.append(
            FeatureClassificationRule(
                category=category,
                group_tag=_required_str(raw_rule, "group_tag", section),
                match_any=tuple(expressions),
            )
        )
    supplemental = _parse_supplemental_shapefiles(table.get("supplemental", []), base_dir)
    supplemental_names = {item.name for item in supplemental}
    raw_precedence = _required_list(table, "surface_precedence", "shapefiles")
    surface_categories = {"buildings", "water", "green_areas", "roads", "concrete", "other_terrain"}
    classified_surface_categories = {
        rule.category for rule in rules if rule.category in surface_categories
    }
    precedence: list[str] = []
    covered_categories: set[str] = set()
    for index, entry in enumerate(raw_precedence, start=1):
        if not isinstance(entry, str) or not entry:
            raise ConfigError(f"shapefiles.surface_precedence[{index}] must be a non-empty string")
        category, separator, group_tag = entry.partition(":")
        if category == "supplemental":
            if not separator or group_tag not in supplemental_names:
                raise ConfigError(
                    f"shapefiles.surface_precedence[{index}] references undeclared supplemental input: {group_tag or entry}"
                )
        elif category not in surface_categories or (separator and not group_tag):
            allowed = ", ".join(sorted(surface_categories))
            raise ConfigError(
                f"shapefiles.surface_precedence[{index}] must be a supported category "
                f"or category:group_tag; categories: {allowed}"
            )
        if entry in precedence:
            raise ConfigError(f"shapefiles.surface_precedence contains duplicate entry: {entry}")
        precedence.append(entry)
        if category != "supplemental" and not separator:
            covered_categories.add(category)
    missing_categories = classified_surface_categories - covered_categories
    if missing_categories:
        missing = ", ".join(sorted(missing_categories))
        raise ConfigError(
            "shapefiles.surface_precedence must include a category-wide fallback for "
            "configured polygon classification categories: " + missing
        )
    for item in supplemental:
        if not item.enabled or item.category == "trees":
            continue
        selector = f"supplemental:{item.name}"
        if selector not in precedence and item.category not in covered_categories:
            raise ConfigError(
                f"enabled shapefiles.supplemental[{item.name}] requires either "
                f"'{selector}' or the '{item.category}' category fallback in "
                "shapefiles.surface_precedence"
            )
    return ShapefilesConfig(
        classification_rules=tuple(rules),
        surface_precedence=tuple(precedence),
        supplemental=supplemental,
    )


def _parse_supplemental_shapefiles(
    raw_inputs: object,
    base_dir: Path,
) -> tuple[SupplementalShapefileConfig, ...]:
    if not isinstance(raw_inputs, list):
        raise ConfigError("shapefiles.supplemental must be a list of input tables")
    supported_categories = {
        "buildings",
        "roads",
        "green_areas",
        "concrete",
        "water",
        "trees",
        "other_terrain",
    }
    supported_crs = {"EPSG:4326", "EPSG:25832", "EPSG:3003"}
    inputs: list[SupplementalShapefileConfig] = []
    names: set[str] = set()
    for index, raw_input in enumerate(raw_inputs, start=1):
        section = f"shapefiles.supplemental[{index}]"
        raw_input = _validated_table(raw_input, f"{section} must be a TOML table")
        _reject_unknown_keys(
            raw_input,
            {"name", "path", "crs", "category", "group_tag", "enabled"},
            section,
        )
        name = _required_str(raw_input, "name", section)
        if name in names:
            raise ConfigError(f"shapefiles.supplemental contains duplicate name: {name}")
        names.add(name)
        category = _required_str(raw_input, "category", section)
        if category not in supported_categories:
            allowed = ", ".join(sorted(supported_categories))
            raise ConfigError(f"{section}.category must be one of: {allowed}")
        crs = _required_str(raw_input, "crs", section).upper()
        if crs not in supported_crs:
            raise ConfigError(f"{section}.crs must be one of: {', '.join(sorted(supported_crs))}")
        path = _required_path(raw_input, "path", section, base_dir)
        if path.suffix.lower() != ".shp":
            raise ConfigError(f"{section}.path must point to an ESRI .shp file")
        if category == "trees":
            if "group_tag" in raw_input:
                raise ConfigError(f"{section}.group_tag is not allowed for trees")
            group_tag = None
        else:
            if "group_tag" not in raw_input:
                raise ConfigError(f"{section}.group_tag is required for non-tree categories")
            group_tag = _required_str(raw_input, "group_tag", section)
        enabled = raw_input.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{section}.enabled must be a boolean")
        inputs.append(
            SupplementalShapefileConfig(
                name=name,
                path=path,
                crs=crs,
                category=category,
                group_tag=group_tag,
                enabled=enabled,
            )
        )
    return tuple(inputs)


def _parse_urban_planning(raw_table: object, base_dir: Path) -> UrbanPlanningConfig:
    table = _validated_table(raw_table, "urban_planning must be a TOML table")
    _reject_unknown_keys(table, {"inputs"}, "urban_planning")
    raw_inputs = table.get("inputs", [])
    if not isinstance(raw_inputs, list):
        raise ConfigError("urban_planning.inputs must be a list of input tables")
    supported_crs = {"EPSG:4326", "EPSG:3857"}
    inputs: list[UrbanPlanningInputConfig] = []
    names: set[str] = set()
    for index, raw_input in enumerate(raw_inputs, start=1):
        section = f"urban_planning.inputs[{index}]"
        raw_input = _validated_table(raw_input, f"{section} must be a TOML table")
        _reject_unknown_keys(raw_input, {"name", "path", "crs", "enabled"}, section)
        name = _required_str(raw_input, "name", section)
        if name in names:
            raise ConfigError(f"urban_planning.inputs contains duplicate name: {name}")
        names.add(name)
        path = _required_path(raw_input, "path", section, base_dir)
        if path.suffix.lower() != ".geojson":
            raise ConfigError(f"{section}.path must point to a .geojson file")
        crs = raw_input.get("crs", "EPSG:4326")
        if not isinstance(crs, str) or crs.upper() not in supported_crs:
            raise ConfigError(f"{section}.crs must be one of: {', '.join(sorted(supported_crs))}")
        enabled = raw_input.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{section}.enabled must be a boolean")
        inputs.append(
            UrbanPlanningInputConfig(
                name=name,
                path=path,
                crs=crs.upper(),
                enabled=enabled,
            )
        )
    return UrbanPlanningConfig(tuple(inputs))


def _parse_trees(table: _TomlTable, base_dir: Path) -> TreeConfig:
    _reject_unknown_keys(
        table,
        {"default", "model_library_path", "category_mapping_path"},
        "trees",
    )
    return TreeConfig(
        default=_required_str(table, "default", "trees"),
        model_library_path=_required_path(table, "model_library_path", "trees", base_dir),
        category_mapping_path=_required_path(table, "category_mapping_path", "trees", base_dir),
    )


def _parse_air_purifiers(
    raw_table: object,
    base_dir: Path,
) -> AirPurifiersConfig:
    table = _validated_table(raw_table, "[air_purifiers] must be a TOML table")
    _reject_unknown_keys(
        table,
        {"model_library_path", "terrain_geometry_path"},
        "air_purifiers",
    )
    return AirPurifiersConfig(
        model_library_path=_optional_path(
            table,
            "model_library_path",
            "air_purifiers",
            base_dir,
        ),
        terrain_geometry_path=_optional_path(
            table,
            "terrain_geometry_path",
            "air_purifiers",
            base_dir,
        ),
    )


def _parse_output(table: _TomlTable, base_dir: Path) -> OutputConfig:
    _reject_unknown_keys(table, {"root_directory"}, "output")
    return OutputConfig(
        root_directory=_required_path(table, "root_directory", "output", base_dir),
    )


def _parse_imagery(table: _TomlTable) -> ImageryConfig:
    _reject_unknown_keys(table, {"sources"}, "imagery")
    raw_sources = _required_list(table, "sources", "imagery")
    if not isinstance(raw_sources, list):
        raise ConfigError("imagery.sources must be a list of source tables")

    sources: list[ImagerySourceConfig] = []
    for index, source in enumerate(raw_sources, start=1):
        section = f"imagery.sources[{index}]"
        source = _validated_table(source, f"{section} must be a TOML table")
        _reject_unknown_keys(
            source,
            {
                "name",
                "type",
                "url",
                "layer",
                "enabled",
                "crs",
                "format",
                "width",
                "height",
                "style",
                "transparent",
            },
            section,
        )
        source_type = _required_str(source, "type", section).lower()
        if source_type != "wms":
            raise ConfigError(f"{section}.type currently supports only 'wms'")
        enabled = _required_bool(source, "enabled", section)
        width = _required_int(source, "width", section)
        height = _required_int(source, "height", section)
        if width <= 0 or height <= 0:
            raise ConfigError(f"{section}.width and {section}.height must be positive")
        crs = _required_str(source, "crs", section)
        if crs.upper() != "EPSG:4326":
            raise ConfigError(f"{section}.crs currently supports only EPSG:4326")
        image_format = _required_str(source, "format", section)
        style = source.get("style", "")
        if not isinstance(style, str):
            raise ConfigError(f"{section}.style must be a string")
        transparent = _required_bool(source, "transparent", section)
        sources.append(
            ImagerySourceConfig(
                name=_required_str(source, "name", section),
                type=source_type,
                url=_required_str(source, "url", section),
                layer=_required_str(source, "layer", section),
                enabled=enabled,
                crs=crs,
                format=image_format,
                width=width,
                height=height,
                style=style,
                transparent=transparent,
            )
        )

    return ImageryConfig(sources=tuple(sources))


def _parse_city_models(table: _TomlTable) -> CityModelsConfig:
    _reject_unknown_keys(
        table,
        {
            "lod",
            "domain_bnd",
            "building_roof_default_base_height_m",
            "top_height",
            "bnd_type_bpg",
            "bpg_blockage_ratio",
            "flow_direction",
            "buffer_region",
            "reconstruct_boundaries",
            "terrain_thinning",
            "smooth_terrain",
            "building_percentile",
            "edge_max_len",
            "reconstruction_region",
            "filters",
            "output_file_name",
            "output_format",
            "output_separately",
            "output_log",
            "log_file",
            "docker_image",
        },
        "city_models",
    )
    smooth_terrain_table = _required_table(table, "smooth_terrain", section="city_models")
    reconstruction_region_table = _required_table(table, "reconstruction_region", section="city_models")
    filters_table = _required_table(table, "filters", section="city_models")
    _reject_unknown_keys(
        smooth_terrain_table,
        {"iterations", "max_pts"},
        "city_models.smooth_terrain",
    )
    _reject_unknown_keys(
        reconstruction_region_table,
        {"influence_region_m", "complexity_factor", "validate"},
        "city_models.reconstruction_region",
    )
    _reject_unknown_keys(
        filters_table,
        {"min_area", "min_height"},
        "city_models.filters",
    )

    flow_direction = _required_float_pair(table, "flow_direction", "city_models")
    domain_bnd_raw = table.get("domain_bnd")
    if domain_bnd_raw is None:
        domain_bnd = None
    elif isinstance(domain_bnd_raw, bool) or not isinstance(domain_bnd_raw, (int, float)):
        raise ConfigError("city_models.domain_bnd must be a positive radius in metres when set")
    else:
        domain_bnd = float(domain_bnd_raw)
    building_roof_default_base_height_m = _required_number(
        table,
        "building_roof_default_base_height_m",
        "city_models",
    )
    top_height = _required_number(table, "top_height", "city_models")
    buffer_region = _required_number(table, "buffer_region", "city_models")
    terrain_thinning = _required_number(table, "terrain_thinning", "city_models")
    building_percentile = _required_number(table, "building_percentile", "city_models")
    edge_max_len = _required_number(table, "edge_max_len", "city_models")
    bpg_blockage_ratio = _required_bool(table, "bpg_blockage_ratio", "city_models")
    reconstruct_boundaries = _required_bool(table, "reconstruct_boundaries", "city_models")
    output_separately = _required_bool(table, "output_separately", "city_models")
    output_log = _required_bool(table, "output_log", "city_models")

    influence_region_m = _required_number(reconstruction_region_table, "influence_region_m", "city_models.reconstruction_region")

    complexity_factor = _required_number(
        reconstruction_region_table,
        "complexity_factor",
        "city_models.reconstruction_region",
    )
    validate = _required_bool(reconstruction_region_table, "validate", "city_models.reconstruction_region")

    smooth_iterations = _required_int(smooth_terrain_table, "iterations", "city_models.smooth_terrain")
    smooth_max_pts = _required_int(smooth_terrain_table, "max_pts", "city_models.smooth_terrain")

    min_area = _required_number(filters_table, "min_area", "city_models.filters")
    min_height = _required_number(filters_table, "min_height", "city_models.filters")

    lod = _required_str(table, "lod", "city_models")
    bnd_type_bpg = _required_str(table, "bnd_type_bpg", "city_models")
    output_file_name = _required_str(table, "output_file_name", "city_models")
    output_format = _required_str(table, "output_format", "city_models")
    log_file = _required_str(table, "log_file", "city_models")
    docker_image = table.get("docker_image")
    if docker_image is not None and not isinstance(docker_image, str):
        raise ConfigError("city_models.docker_image must be a non-empty string when set")

    return CityModelsConfig(
        lod=lod,
        domain_bnd=domain_bnd,
        building_roof_default_base_height_m=building_roof_default_base_height_m,
        top_height=top_height,
        bnd_type_bpg=bnd_type_bpg,
        bpg_blockage_ratio=bpg_blockage_ratio,
        flow_direction=flow_direction,
        buffer_region=buffer_region,
        reconstruct_boundaries=reconstruct_boundaries,
        terrain_thinning=terrain_thinning,
        smooth_terrain=CityModelsSmoothTerrainConfig(
            iterations=smooth_iterations,
            max_pts=smooth_max_pts,
        ),
        building_percentile=building_percentile,
        edge_max_len=edge_max_len,
        reconstruction_region=CityModelsReconstructionRegionConfig(
            influence_region_m=influence_region_m,
            complexity_factor=complexity_factor,
            validate=validate,
        ),
        filters=CityModelsFilterConfig(min_area=min_area, min_height=min_height),
        output_file_name=output_file_name,
        output_format=output_format,
        output_separately=output_separately,
        output_log=output_log,
        log_file=log_file,
        docker_image=docker_image,
    )


def _validated_table(value: object, error_message: str) -> _TomlTable:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigError(error_message)
    return value


def _required_table(root: _TomlTable, name: str, section: str | None = None) -> _TomlTable:
    table = root.get(name)
    table_name = f"{section}.{name}" if section else name
    return _validated_table(table, f"missing required [{table_name}] table")


def _reject_unknown_keys(table: _TomlTable, allowed: set[str], section: str) -> None:
    unknown = sorted(set(table) - allowed)
    if not unknown:
        return
    qualified = [f"{section}.{key}" if section else key for key in unknown]
    label = "key" if len(qualified) == 1 else "keys"
    raise ConfigError(f"unknown configuration {label}: {', '.join(qualified)}")


def _required_str(table: _TomlTable, key: str, section: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{section}.{key} must be a non-empty string")
    return value


def _required_number(table: _TomlTable, key: str, section: str) -> float:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{section}.{key} must be a number")
    return float(value)


def _optional_number(table: _TomlTable, key: str, section: str) -> float | None:
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{section}.{key} must be a number when set")
    return float(value)


def _required_int(table: _TomlTable, key: str, section: str) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{section}.{key} must be an integer")
    return value


def _required_bool(table: _TomlTable, key: str, section: str) -> bool:
    value = table.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{section}.{key} must be a boolean")
    return value


def _required_list(table: _TomlTable, key: str, section: str) -> list[object]:
    value = table.get(key)
    if not isinstance(value, list):
        raise ConfigError(f"{section}.{key} must be a list")
    return value


def _required_float_pair(
    table: _TomlTable,
    key: str,
    section: str,
) -> tuple[float, float]:
    value = table.get(key)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError(f"{section}.{key} must be a list or tuple of exactly two numbers")
    first, second = value
    if (
        isinstance(first, bool)
        or isinstance(second, bool)
        or not isinstance(first, (int, float))
        or not isinstance(second, (int, float))
    ):
        raise ConfigError(f"{section}.{key} must contain numbers")
    return float(first), float(second)


def _required_path(table: _TomlTable, key: str, section: str, base_dir: Path) -> Path:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{section}.{key} must be a non-empty path string")
    return _resolve_path(value, base_dir)


def _optional_path(table: _TomlTable, key: str, section: str, base_dir: Path) -> Path | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{section}.{key} must be a non-empty path string when set")
    return _resolve_path(value, base_dir)


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()
