"""Declarative command-line options owned by individual pipeline stages."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class CliValueKind(StrEnum):
    """Supported parser shapes for stage execution overrides."""

    TEXT = "text"
    PATH = "path"
    FLOAT = "float"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    FLOAT_PAIR = "float_pair"


@dataclass(frozen=True)
class StageCliOption:
    """Describe one stage-owned CLI override and register it on demand."""

    name: str
    help: str
    kind: CliValueKind = CliValueKind.TEXT

    @property
    def destination(self) -> str:
        """Return the argparse namespace destination for this option."""

        return self.name.removeprefix("--").replace("-", "_")

    @property
    def option_strings(self) -> tuple[str, ...]:
        """Return every accepted spelling, including boolean negation."""

        if self.kind is CliValueKind.BOOLEAN:
            return (self.name, f"--no-{self.name.removeprefix('--')}")
        return (self.name,)

    @property
    def value_count(self) -> int:
        """Return how many following tokens this option consumes."""

        if self.kind is CliValueKind.BOOLEAN:
            return 0
        if self.kind is CliValueKind.FLOAT_PAIR:
            return 2
        return 1

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        """Register this option without exposing any other stage's options."""

        match self.kind:
            case CliValueKind.TEXT:
                parser.add_argument(self.name, help=self.help)
            case CliValueKind.PATH:
                parser.add_argument(self.name, type=Path, help=self.help)
            case CliValueKind.FLOAT:
                parser.add_argument(self.name, type=float, help=self.help)
            case CliValueKind.INTEGER:
                parser.add_argument(self.name, type=int, help=self.help)
            case CliValueKind.BOOLEAN:
                parser.add_argument(
                    self.name,
                    action=argparse.BooleanOptionalAction,
                    default=None,
                    help=self.help,
                )
            case CliValueKind.FLOAT_PAIR:
                parser.add_argument(
                    self.name,
                    nargs=2,
                    type=float,
                    metavar=("X", "Y"),
                    help=self.help,
                )


SHAPEFILES_CLI_OPTIONS = (
    StageCliOption(
        "--overpass-json",
        "Use a cached Overpass JSON file instead of making a network request.",
        CliValueKind.PATH,
    ),
    StageCliOption(
        "--streets-shapefile",
        "Override the conventional named supplemental input 'streets' for this shapefiles-stage run.",
        CliValueKind.PATH,
    ),
    StageCliOption(
        "--streets-shapefile-crs",
        "Override the CRS of the conventional named supplemental input 'streets'.",
    ),
    StageCliOption(
        "--green-areas-shapefile",
        "Override the conventional named supplemental input 'green_areas' for this shapefiles-stage run.",
        CliValueKind.PATH,
    ),
    StageCliOption(
        "--green-areas-shapefile-crs",
        "Override the CRS of the conventional named supplemental input 'green_areas'.",
    ),
)

VISUAL_ENRICHMENT_CLI_OPTIONS = (
    StageCliOption(
        "--segmentation-geojson",
        "Use external segmentation polygons for the visual-enrichment stage.",
        CliValueKind.PATH,
    ),
    StageCliOption(
        "--sat2lod2-geojson",
        "Use external SAT2LoD2/LOD2BuildingModel 2D building polygons for the visual-enrichment stage.",
        CliValueKind.PATH,
    ),
)

POINT_CLOUD_CLI_OPTIONS = (
    StageCliOption(
        "--tree-canopy-overlay",
        (
            "Override inputs.tree_canopy_overlay_path for the point-cloud stage. "
            "When omitted and not configured in TOML, tree-point filtering is skipped."
        ),
        CliValueKind.PATH,
    ),
    StageCliOption(
        "--building-footprints-geojson",
        (
            "Explicitly override the Stage 1 building-footprint GeoJSON for this point-cloud run. "
            "Relative paths are resolved from the configuration directory."
        ),
        CliValueKind.PATH,
    ),
)

CITY_MODELS_CLI_OPTIONS = (
    StageCliOption(
        "--city-models-lod",
        "Override the City4CFD reconstruction LOD.",
    ),
    StageCliOption(
        "--city-models-top-height",
        "Override the City4CFD domain top height.",
        CliValueKind.FLOAT,
    ),
    StageCliOption(
        "--city-models-bnd-type-bpg",
        "Override the City4CFD boundary type for the building-point-generation domain.",
    ),
    StageCliOption(
        "--city-models-bpg-blockage-ratio",
        "Enable or disable City4CFD blockage-ratio handling.",
        CliValueKind.BOOLEAN,
    ),
    StageCliOption(
        "--city-models-flow-direction",
        "Override the City4CFD flow direction vector.",
        CliValueKind.FLOAT_PAIR,
    ),
    StageCliOption(
        "--city-models-buffer-region",
        "Override the City4CFD buffer region.",
        CliValueKind.FLOAT,
    ),
    StageCliOption(
        "--city-models-reconstruct-boundaries",
        "Enable or disable City4CFD boundary reconstruction.",
        CliValueKind.BOOLEAN,
    ),
    StageCliOption(
        "--city-models-terrain-thinning",
        "Override the City4CFD terrain thinning distance.",
        CliValueKind.FLOAT,
    ),
    StageCliOption(
        "--city-models-smooth-terrain-iterations",
        "Override the number of City4CFD terrain smoothing iterations.",
        CliValueKind.INTEGER,
    ),
    StageCliOption(
        "--city-models-smooth-terrain-max-pts",
        "Override the City4CFD terrain smoothing point limit.",
        CliValueKind.INTEGER,
    ),
    StageCliOption(
        "--city-models-building-percentile",
        "Override the City4CFD building elevation percentile.",
        CliValueKind.FLOAT,
    ),
    StageCliOption(
        "--city-models-edge-max-len",
        "Override the City4CFD maximum edge length.",
        CliValueKind.FLOAT,
    ),
    StageCliOption(
        "--city-models-reconstruction-influence-region",
        "Override the City4CFD reconstruction influence region radius.",
        CliValueKind.FLOAT,
    ),
    StageCliOption(
        "--city-models-reconstruction-complexity-factor",
        "Override the City4CFD reconstruction complexity factor.",
        CliValueKind.FLOAT,
    ),
    StageCliOption(
        "--city-models-reconstruction-validate",
        "Enable or disable City4CFD reconstruction validation.",
        CliValueKind.BOOLEAN,
    ),
    StageCliOption(
        "--city-models-filters-min-area",
        "Override the minimum filtered polygon area.",
        CliValueKind.FLOAT,
    ),
    StageCliOption(
        "--city-models-filters-min-height",
        "Override the minimum filtered polygon height.",
        CliValueKind.FLOAT,
    ),
    StageCliOption(
        "--city-models-output-file-name",
        "Override the City4CFD output file base name.",
    ),
    StageCliOption(
        "--city-models-output-format",
        "Override the City4CFD output mesh format.",
    ),
    StageCliOption(
        "--city-models-output-separately",
        "Enable or disable separate City4CFD outputs.",
        CliValueKind.BOOLEAN,
    ),
    StageCliOption(
        "--city-models-output-log",
        "Enable or disable City4CFD log output.",
        CliValueKind.BOOLEAN,
    ),
    StageCliOption(
        "--city-models-log-file",
        "Override the City4CFD log file name.",
    ),
    StageCliOption(
        "--city-models-docker-image",
        "Override the Docker image used by the City4CFD fallback script.",
    ),
)

TREES_CLI_OPTIONS = (
    StageCliOption(
        "--tree-terrain-geometry",
        (
            "Override inputs.tree_terrain_geometry_path for the trees stage. "
            "The OBJ or ASCII STL geometry must use the local City4CFD coordinate frame."
        ),
        CliValueKind.PATH,
    ),
)

AIR_PURIFIERS_CLI_OPTIONS = (
    StageCliOption(
        "--model-library",
        (
            "Override air_purifiers.model_library_path for the air-purifiers stage. "
            "Relative paths are resolved from the configuration directory."
        ),
        CliValueKind.PATH,
    ),
    StageCliOption(
        "--terrain-geometry",
        (
            "Override air_purifiers.terrain_geometry_path for the air-purifiers stage. "
            "Relative paths are resolved from the configuration directory."
        ),
        CliValueKind.PATH,
    ),
)
