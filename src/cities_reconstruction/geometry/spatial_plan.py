"""Inspect dataset coordinate evidence and resolve an immutable processing plan."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from cities_reconstruction.errors import ConfigError
from cities_reconstruction.geometry.crs import (
    WGS84,
    automatic_utm_crs,
    canonical_crs,
    crs_equal,
    crs_label,
    project_lonlat,
    validate_working_crs,
)
from cities_reconstruction.geometry.crs_inputs import projection_sidecar, raster_crs, source_crs

if TYPE_CHECKING:
    from cities_reconstruction.config import AppConfig
    from cities_reconstruction.stage_contract import JsonValue


@dataclass(frozen=True)
class DatasetCoordinates:
    name: str
    path: Path
    source_crs: str
    evidence: str
    target_crs: str
    action: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "path": str(self.path),
            "source_crs": self.source_crs,
            "evidence": self.evidence,
            "target_crs": self.target_crs,
            "action": self.action,
        }


@dataclass(frozen=True)
class SpatialPlan:
    working_crs: str
    selection_reason: str
    input_mode: str
    datasets: tuple[DatasetCoordinates, ...]
    local_origin: tuple[float, float]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "working_crs": self.working_crs,
            "selection_reason": self.selection_reason,
            "input_mode": self.input_mode,
            "datasets": [item.to_dict() for item in self.datasets],
            "local_origin": {"x": self.local_origin[0], "y": self.local_origin[1]},
            "roi_source_crs": WGS84,
            "vertical": "Compatible metre heights required; no vertical-datum conversion.",
        }

    def describe(self) -> str:
        mode = {
            "terrain_rasters": "terrain rasters (DTM + DSM)",
            "prepared_clouds": "prepared ground/building clouds (user-classified)",
            "no_elevation": "no elevation input configured",
        }[self.input_mode]
        lines = [
            "Coordinate plan",
            "  ROI: WGS84 latitude/longitude",
            f"  Input mode: {mode}",
            f"  Reconstruction: {crs_label(self.working_crs)} — {self.selection_reason}",
        ]
        for dataset in self.datasets:
            lines.append(f"  {dataset.name}: {crs_label(dataset.source_crs)} ({dataset.evidence}); {dataset.action}")
        lines.append("  Heights: compatible metres required; no vertical-datum conversion.")
        return "\n".join(lines)


def resolve_spatial_plan(config: AppConfig) -> SpatialPlan:
    """Resolve source facts independently from the user's optional target choice."""
    inputs = config.inputs
    working = config.reconstruction.working_crs
    reason = "explicit reconstruction override" if working is not None else ""
    if working is not None:
        working = validate_working_crs(working)
    raster_sources: list[tuple[str, Path, str, str]] = []
    cloud_sources: list[tuple[str, Path, str, str]] = []
    has_clouds = inputs.ground_point_cloud_path is not None or inputs.building_point_cloud_path is not None
    has_rasters = inputs.dtm_directory is not None or inputs.dsm_directory is not None
    if inputs.point_cloud_path is not None:
        raise ConfigError(
            "A raw single point_cloud_path is unsupported; supply separate ground_point_cloud_path and building_point_cloud_path with point_cloud_source_crs, or DTM/DSM rasters"
        )
    if has_clouds:
        if inputs.ground_point_cloud_path is None or inputs.building_point_cloud_path is None:
            raise ConfigError("supply both ground_point_cloud_path and building_point_cloud_path")
        if has_rasters:
            raise ConfigError("prepared clouds and terrain rasters are mutually exclusive input modes")
        if inputs.point_cloud_source_crs is None:
            raise ConfigError(
                "inputs.point_cloud_source_crs is required for prepared clouds: declare the CRS already used by their X/Y coordinates"
            )
        if inputs.tree_canopy_overlay_path is not None:
            raise ConfigError(
                "tree_canopy_overlay_path is a raster-classification option; remove it for prepared clouds"
            )
        cloud_crs = canonical_crs(inputs.point_cloud_source_crs)
        for name, path in (
            ("ground cloud", inputs.ground_point_cloud_path),
            ("building cloud", inputs.building_point_cloud_path),
        ):
            cloud_sources.append((name, path, cloud_crs, "declared"))
        if working is None:
            try:
                working = validate_working_crs(cloud_crs)
                reason = "retained prepared-cloud source CRS"
            except ConfigError:
                pass  # A valid source can be unsuitable as a metric working plane.
    if has_rasters and not has_clouds and (inputs.dtm_directory is None or inputs.dsm_directory is None):
        raise ConfigError("terrain raster mode requires both dtm_directory and dsm_directory")
    for name, directory, declaration in (
        ("DTM", inputs.dtm_directory, inputs.dtm_source_crs),
        ("DSM", inputs.dsm_directory, inputs.dsm_source_crs),
    ):
        if directory is None:
            continue
        resolved = raster_crs(directory, declaration)
        evidence = (
            "metadata + declaration"
            if resolved.sidecars and declaration
            else "metadata"
            if resolved.sidecars
            else "declared"
        )
        raster_sources.append((name, directory, resolved.crs, evidence))
        if working is None:
            working = validate_working_crs(resolved.crs)
            reason = "selected from terrain raster source CRS"
        if not crs_equal(resolved.crs, working):
            raise ConfigError(
                f"{name} source CRS {crs_label(resolved.crs)} differs from working CRS {crs_label(working)}; prepare aligned DTM/DSM grids in one suitable projected metre CRS. Automatic raster resampling is not supported; changing a source declaration does not reproject data."
            )
    if working is None:
        working = automatic_utm_crs(config.region.center_lon, config.region.center_lat)
        reason = "UTM selected from ROI center"
    datasets: list[DatasetCoordinates] = []
    for name, path, source, evidence in raster_sources + cloud_sources:
        action = (
            "retain coordinates" if crs_equal(source, working) else f"transform X/Y to {crs_label(working)}; retain Z"
        )
        datasets.append(DatasetCoordinates(name, path, source, evidence, working, action))
    for item in config.shapefiles.supplemental:
        if item.enabled:
            source = source_crs(item.path, item.crs)
            evidence = (
                "metadata + declaration"
                if projection_sidecar(item.path) and item.crs
                else "metadata"
                if projection_sidecar(item.path)
                else "declared"
            )
            datasets.append(
                DatasetCoordinates(
                    item.name,
                    item.path,
                    source,
                    evidence,
                    working,
                    f"normalize to WGS84; project downstream to {crs_label(working)}",
                )
            )
    for planning_input in config.urban_planning.inputs:
        if planning_input.enabled:
            datasets.append(
                DatasetCoordinates(
                    planning_input.name,
                    planning_input.path,
                    planning_input.crs,
                    "declared" if planning_input.crs_declared else "GeoJSON WGS84 default",
                    working,
                    f"normalize to WGS84; project downstream to {crs_label(working)}",
                )
            )
    mode = "terrain_rasters" if has_rasters else "prepared_clouds" if has_clouds else "no_elevation"
    origin = project_lonlat(config.region.center_lon, config.region.center_lat, working)
    return SpatialPlan(working, reason, mode, tuple(datasets), origin)


def prepare_config(config: AppConfig) -> AppConfig:
    """Return settings with fresh evidence; never rewrite the requested CRS."""
    return replace(config, spatial_plan=resolve_spatial_plan(config))


def verify_spatial_plan(config: AppConfig) -> None:
    """Reject changed coordinate evidence rather than silently changing the plan."""
    if resolve_spatial_plan(config) != config.coordinate_plan:
        raise ConfigError(
            "Coordinate settings or dataset metadata changed after preparation; reload the configuration and review the coordinate plan before running again."
        )
