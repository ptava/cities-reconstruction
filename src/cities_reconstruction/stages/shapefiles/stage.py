"""Geospatial feature retrieval for the first pipeline stage.

The implemented output format is GeoJSON. The stage keeps the surrounding
module name because the project-level workflow calls this step "shapefiles";
true ESRI Shapefile export will be added behind the same stage boundary when a
GIS stack is introduced.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.validation import make_valid

from cities_reconstruction.artifacts import (
    atomic_write_text,
    lightweight_state_fingerprint,
    stage_output_lock,
)
from cities_reconstruction.config import AppConfig
from cities_reconstruction.stage_contract import (
    ArtifactReference,
    JsonValue,
    StageManifest,
    StageStatus,
    invalidate_stage_manifests,
)
from cities_reconstruction.stage_layout import StageId, stage_output_directory
from cities_reconstruction.stage_result import StageResult
from . import policy as shapefiles_policy
from .diagnostics import (
    build_geometry_diagnostics,
    build_summary,
    non_contributing_features,
    supplemental_surface_input_diagnostics,
    supplemental_tree_input_diagnostics,
    urban_planning_diagnostics,
)
from .inputs import (
    fetch_imagery_diagnostics,
    load_or_fetch_geometry_batches,
    load_or_fetch_overpass,
)
from .publication import (
    ShapefilesPublicationInput,
    publish_shapefiles_manifest,
)
from .rendering import (
    render_imagery_overlay_html,
    render_preview_html,
)
from .reporting import render_report
from .supplemental import (
    load_supplemental_surface_features as _load_supplemental_surface_features,
)
from .supplemental import (
    load_supplemental_tree_features as _load_supplemental_tree_features,
)
from .transformation import (
    EARTH_RADIUS_M,
    _circle_polygon_m,
    _extract_polygons,
    _feature_to_shapely_polygons,
    _point_norm_m,
    _polygon_m_to_lonlat_coordinates,
    _reconstruction_scope,
    build_tag_inventory,
    overpass_to_features,
)
from .transformation import (
    _project_coordinate_m as _project_coordinate_m,
)
from cities_reconstruction.urban_planning import load_inputs as load_urban_planning_inputs

STAGE_ID = StageId.SHAPEFILES

_feature_union_m = shapefiles_policy.feature_union_m
_remove_overpass_trees_overlapping_supplemental_trees = (
    shapefiles_policy.remove_overpass_trees_overlapping_supplemental_trees
)
_resolve_surface_overlaps = shapefiles_policy.resolve_surface_overlaps
_surface_precedence_rank = shapefiles_policy.surface_precedence_rank

CATEGORIES = (
    "buildings",
    "roads",
    "green_areas",
    "concrete",
    "water",
    "trees",
    "other_terrain",
    "gap_fill",
)

OVERPASS_FEATURE_KEY_PATTERN = (
    "building|building:part|highway|landuse|leisure|natural|waterway|water|"
    "surface|amenity|tourism|historic|man_made"
)
OVERPASS_GEOMETRY_SELECTOR_TEMPLATES = (
    f'way(around:{{radius}},{{center}})[~"^({OVERPASS_FEATURE_KEY_PATTERN})$"~"."];',
    f'relation(around:{{radius}},{{center}})[~"^({OVERPASS_FEATURE_KEY_PATTERN})$"~"."];',
    'way(around:{radius},{center})["place"="square"];',
    'relation(around:{radius},{center})["place"="square"];',
    'way(around:{radius},{center})["area"="yes"];',
    'relation(around:{radius},{center})["area"="yes"];',
    'node(around:{radius},{center})["natural"="tree"];',
)
OVERPASS_GEOMETRY_BATCH_SIZE = 7

@dataclass(frozen=True)
class ShapefilesStageOutput:
    manifest: StageManifest
    tag_inventory_query_path: Path
    tag_inventory_raw_path: Path
    tag_inventory_path: Path
    query_path: Path
    raw_overpass_path: Path
    all_features_path: Path
    urban_planning_path: Path
    air_purifiers_path: Path
    category_paths: dict[str, Path]
    region_paths: dict[str, Path]
    diagnostics_path: Path
    diagnostics_geojson_path: Path
    imagery_diagnostics_path: Path
    imagery_overlay_path: Path
    summary_path: Path
    source: str
    raw_element_count: int
    accepted_feature_count: int
    skipped_feature_count: int

    @property
    def stage(self) -> str:
        return self.manifest.stage

    @property
    def status(self) -> StageStatus:
        return self.manifest.status

    @property
    def output_directory(self) -> Path:
        return self.manifest.output_directory

    @property
    def manifest_path(self) -> Path:
        return self.manifest.manifest_path

    @property
    def report_path(self) -> Path:
        return self.manifest.report_path

    @property
    def preview_path(self) -> Path:
        return self.manifest.preview_path

    @property
    def artifacts(self) -> tuple[ArtifactReference, ...]:
        return self.manifest.artifacts

    @property
    def metrics(self) -> dict[str, JsonValue]:
        return self.manifest.metrics

    @property
    def details(self) -> dict[str, JsonValue]:
        return self.manifest.details

    def to_dict(self) -> dict[str, JsonValue]:
        return self.manifest.to_dict()


def plan(config: AppConfig) -> StageResult:
    region = config.region
    output = stage_output_directory(config.output.root_directory, STAGE_ID)
    region_actions: tuple[str, ...]
    if region.inner_diameter_m is None:
        region_actions = (
            f"Keep all target features uniformly inside the {region.outer_diameter_m:g} m outer diameter.",
            "Write one full-region GeoJSON output; do not apply inner/annular reconstruction rules.",
        )
    else:
        region_actions = (
            f"Keep all target features inside {region.inner_diameter_m:g} m diameter.",
            f"Keep all target features between {region.inner_diameter_m:g} m and {region.outer_diameter_m:g} m as annular context.",
            "Write separate inner-region and annular-region GeoJSON outputs.",
        )
    return StageResult(
        stage=STAGE_ID.value,
        summary="Plan Overpass/GIS feature retrieval and clipping.",
        planned_actions=(
            f"Query {config.inputs.overpass_url} around {region.center_lat:g}, {region.center_lon:g}.",
            *region_actions,
            "Write clipped buildings, roads, green areas, concrete, water, and tree features as GeoJSON.",
            "Append every enabled supplemental tree shapefile.",
            "Integrate supplemental polygon shapefiles at their explicit precedence positions.",
            "Write a self-contained HTML/SVG preview for graphical feedback.",
            "Fetch configured aerial imagery as diagnostic evidence when imagery sources are enabled.",
        ),
        expected_outputs=(output,),
    )


def run(config: AppConfig, overpass_json_path: Path | None = None) -> ShapefilesStageOutput:
    """Execute the first pipeline stage and write retrieved feature artifacts."""

    output_dir = stage_output_directory(config.output.root_directory, STAGE_ID)
    with stage_output_lock(output_dir, STAGE_ID.value):
        invalidate_stage_manifests(output_dir)
        return _run_locked(config, output_dir, overpass_json_path)


def _run_locked(
    config: AppConfig,
    output_dir: Path,
    overpass_json_path: Path | None,
) -> ShapefilesStageOutput:
    """Execute shapefiles work while the caller owns the stage-output lock."""

    overpass_json_path = overpass_json_path.resolve() if overpass_json_path is not None else None

    tag_inventory_query = build_tag_inventory_query(config)
    tag_inventory_query_path = output_dir / "tag_inventory_query.txt"
    atomic_write_text(tag_inventory_query_path, tag_inventory_query)

    tag_inventory_raw_path = output_dir / "tag_inventory_raw.json"
    reusing_stage_geometry_cache = (
        overpass_json_path is not None
        and overpass_json_path.resolve() == (output_dir / "overpass_raw.json").resolve()
    )
    if tag_inventory_raw_path.exists() and (overpass_json_path is None or reusing_stage_geometry_cache):
        with tag_inventory_raw_path.open("r", encoding="utf-8") as handle:
            tag_inventory_raw_data = json.load(handle)
        tag_inventory_source = f"existing tag-inventory cache: {tag_inventory_raw_path}"
    else:
        tag_inventory_raw_data, tag_inventory_source = load_or_fetch_overpass(
            config,
            tag_inventory_query,
            overpass_json_path,
            cached_source_label="cached file used for tag inventory",
        )
    atomic_write_text(tag_inventory_raw_path, json.dumps(tag_inventory_raw_data, indent=2, sort_keys=True))
    tag_inventory = build_tag_inventory(tag_inventory_raw_data, source=tag_inventory_source, config=config)
    tag_inventory_path = output_dir / "tag_inventory.json"
    atomic_write_text(tag_inventory_path, json.dumps(tag_inventory, indent=2, sort_keys=True))

    query = build_overpass_query(config)
    query_path = output_dir / "overpass_query.txt"
    atomic_write_text(query_path, query)

    raw_data, source = load_or_fetch_geometry_batches(
        config,
        output_dir,
        overpass_json_path,
        query=query,
        batch_queries=build_overpass_query_batches(config),
    )
    raw_path = output_dir / "overpass_raw.json"
    atomic_write_text(raw_path, json.dumps(raw_data, indent=2, sort_keys=True))
    for temporary_path in (
        *output_dir.glob("overpass_raw_batch_*.json"),
        *output_dir.glob("overpass_query_batch_*.txt"),
    ):
        temporary_path.unlink(missing_ok=True)

    features, skipped_count, skipped_by_reason = overpass_to_features(raw_data, config)
    urban_planning = load_urban_planning_inputs(config)
    planning_features = list(urban_planning.accepted_features)
    planning_tree_features = [
        _route_urban_planning_feature(feature)
        for feature in planning_features
        if feature["properties"]["kind"] == "tree"
    ]
    purifier_features = [
        _route_urban_planning_feature(feature)
        for feature in planning_features
        if feature["properties"]["kind"] == "air_purifier"
    ]
    loaded_supplements = {
        item.name: (
            _load_supplemental_tree_features(config, item)
            if item.category == "trees"
            else _load_supplemental_surface_features(config, item)
        ) if item.enabled else []
        for item in config.shapefiles.supplemental
    }
    supplemental_tree_features = [
        feature
        for item in config.shapefiles.supplemental
        if item.category == "trees"
        for feature in loaded_supplements[item.name]
    ]
    features, tree_overlap_filter = _remove_overpass_trees_overlapping_supplemental_trees(
        features,
        supplemental_tree_features,
        config.inputs.tree_overlap_tolerance_m,
    )
    if tree_overlap_filter["removed_overpass_tree_count"]:
        skipped_by_reason["overpass_tree_overlaps_supplemental_tree"] = tree_overlap_filter["removed_overpass_tree_count"]
        skipped_count += tree_overlap_filter["removed_overpass_tree_count"]
    features = [*features, *supplemental_tree_features, *planning_tree_features]
    supplemental_surface_features = [
        feature
        for item in config.shapefiles.supplemental
        if item.category != "trees"
        for feature in loaded_supplements[item.name]
    ]
    features.extend(supplemental_surface_features)
    tree_input_diagnostics = supplemental_tree_input_diagnostics(config, loaded_supplements)
    surface_input_diagnostics = supplemental_surface_input_diagnostics(config, loaded_supplements)
    features, surface_overlap_diagnostics = _resolve_surface_overlaps(features, config)
    gap_fill_features = _build_gap_fill_features(features, config)
    features = [*features, *gap_fill_features]
    category_features: dict[str, list[dict[str, Any]]] = {category: [] for category in CATEGORIES}
    for feature in features:
        category_features[feature["properties"]["category"]].append(feature)

    reference_features = [*features, *purifier_features]
    diagnostics = build_geometry_diagnostics(reference_features, generated_gap_fill_count=len(gap_fill_features))

    all_features_path = output_dir / "all_features.geojson"
    _write_geojson(all_features_path, reference_features)
    urban_planning_path = output_dir / "urban_planning.geojson"
    _write_geojson(urban_planning_path, planning_features)
    air_purifiers_path = output_dir / "air_purifiers.geojson"
    _write_geojson(air_purifiers_path, purifier_features)

    category_paths: dict[str, Path] = {}
    for category, items in category_features.items():
        path = output_dir / f"{category}.geojson"
        _write_geojson(path, items)
        category_paths[category] = path
    region_features = _features_by_region(reference_features)
    for stale_region_name in {"full_region", "inner_region", "annular_region"} - set(region_features):
        (output_dir / f"{stale_region_name}.geojson").unlink(missing_ok=True)
    region_paths: dict[str, Path] = {}
    for region_name, items in region_features.items():
        path = output_dir / f"{region_name}.geojson"
        _write_geojson(path, items)
        region_paths[region_name] = path
    diagnostics_path = output_dir / "geometry_diagnostics.json"
    atomic_write_text(diagnostics_path, json.dumps(diagnostics, indent=2, sort_keys=True))
    diagnostics_geojson_path = output_dir / "non_contributing_features.geojson"
    _write_geojson(diagnostics_geojson_path, non_contributing_features(reference_features))
    imagery_diagnostics = fetch_imagery_diagnostics(
        config,
        output_dir,
        _roi_bbox_lon_lat(config),
    )
    imagery_diagnostics_path = output_dir / "imagery_diagnostics.json"
    atomic_write_text(imagery_diagnostics_path, json.dumps(imagery_diagnostics, indent=2, sort_keys=True))
    imagery_overlay_path = output_dir / "imagery_overlay.html"
    atomic_write_text(
        imagery_overlay_path,
        render_imagery_overlay_html(
            config,
            reference_features,
            imagery_diagnostics,
            tree_overlap_filter,
            categories=CATEGORIES,
        ),
    )

    raw_element_count = len(raw_data.get("elements", []))
    summary = build_summary(
        config=config,
        features=reference_features,
        raw_element_count=raw_element_count,
        skipped_count=skipped_count,
        skipped_by_reason=skipped_by_reason,
        category_features=category_features,
        source=_feature_source_label(
            source,
            config,
            loaded_supplements,
            urban_planning,
        ),
        tag_inventory=tag_inventory,
        geometry_diagnostics=diagnostics,
        tree_overlap_filter=tree_overlap_filter,
        tree_input_diagnostics=tree_input_diagnostics,
        surface_input_diagnostics=surface_input_diagnostics,
        surface_overlap_diagnostics=surface_overlap_diagnostics,
        urban_planning_diagnostics=urban_planning_diagnostics(config, urban_planning),
    )
    summary_path = output_dir / "summary.json"
    atomic_write_text(summary_path, json.dumps(summary, indent=2, sort_keys=True))

    preview_path = output_dir / "preview.html"
    atomic_write_text(
        preview_path,
        render_preview_html(config, reference_features, summary, categories=CATEGORIES),
    )

    report_path = output_dir / "report.md"
    atomic_write_text(
        report_path,
        render_report(
            config=config,
            summary=summary,
            categories=CATEGORIES,
            tag_inventory_query_path=tag_inventory_query_path,
            tag_inventory_raw_path=tag_inventory_raw_path,
            tag_inventory_path=tag_inventory_path,
            query_path=query_path,
            raw_path=raw_path,
            all_features_path=all_features_path,
            urban_planning_path=urban_planning_path,
            air_purifiers_path=air_purifiers_path,
            category_paths=category_paths,
            region_paths=region_paths,
            diagnostics_path=diagnostics_path,
            diagnostics_geojson_path=diagnostics_geojson_path,
            imagery_diagnostics_path=imagery_diagnostics_path,
            imagery_overlay_path=imagery_overlay_path,
            summary_path=summary_path,
            preview_path=preview_path,
        ),
    )

    feature_source = _feature_source_label(
        source,
        config,
        loaded_supplements,
        urban_planning,
    )
    manifest = publish_shapefiles_manifest(
        ShapefilesPublicationInput(
            output_directory=output_dir,
            report_path=report_path,
            preview_path=preview_path,
            input_state_fingerprint=_shapefiles_input_fingerprint(config, overpass_json_path),
            all_features_path=all_features_path,
            urban_planning_path=urban_planning_path,
            air_purifiers_path=air_purifiers_path,
            category_paths=category_paths,
            region_paths=region_paths,
            tag_inventory_query_path=tag_inventory_query_path,
            tag_inventory_raw_path=tag_inventory_raw_path,
            tag_inventory_path=tag_inventory_path,
            query_path=query_path,
            raw_path=raw_path,
            diagnostics_path=diagnostics_path,
            diagnostics_geojson_path=diagnostics_geojson_path,
            imagery_diagnostics_path=imagery_diagnostics_path,
            imagery_overlay_path=imagery_overlay_path,
            imagery_diagnostics=imagery_diagnostics,
            summary_path=summary_path,
            raw_element_count=raw_element_count,
            accepted_feature_count=len(reference_features),
            skipped_feature_count=skipped_count,
            source=feature_source,
        )
    )

    return ShapefilesStageOutput(
        manifest=manifest,
        tag_inventory_query_path=tag_inventory_query_path,
        tag_inventory_raw_path=tag_inventory_raw_path,
        tag_inventory_path=tag_inventory_path,
        query_path=query_path,
        raw_overpass_path=raw_path,
        all_features_path=all_features_path,
        urban_planning_path=urban_planning_path,
        air_purifiers_path=air_purifiers_path,
        category_paths=category_paths,
        region_paths=region_paths,
        diagnostics_path=diagnostics_path,
        diagnostics_geojson_path=diagnostics_geojson_path,
        imagery_diagnostics_path=imagery_diagnostics_path,
        imagery_overlay_path=imagery_overlay_path,
        summary_path=summary_path,
        source=feature_source,
        raw_element_count=raw_element_count,
        accepted_feature_count=len(reference_features),
        skipped_feature_count=skipped_count,
    )


def _shapefiles_input_fingerprint(
    config: AppConfig,
    overpass_json_path: Path | None,
) -> dict[str, JsonValue]:
    """Fingerprint configuration and resolved local sources, never generated outputs."""

    paths = [config.path]
    stage_dir = stage_output_directory(config.output.root_directory, STAGE_ID)
    stage_cache_paths = {
        (stage_dir / "overpass_raw.json").resolve(),
        (stage_dir / "tag_inventory_raw.json").resolve(),
    }
    is_stage_owned_cache = overpass_json_path is not None and overpass_json_path.resolve() in stage_cache_paths
    if overpass_json_path is not None and not is_stage_owned_cache:
        paths.append(overpass_json_path)
    for supplemental in config.shapefiles.supplemental:
        if supplemental.enabled:
            paths.append(supplemental.path)
            dbf_path = supplemental.path.with_suffix(".dbf")
            if dbf_path.is_file():
                paths.append(dbf_path)
    for planning_input in config.urban_planning.inputs:
        if planning_input.enabled:
            paths.append(planning_input.path)
    if any(planning_input.enabled for planning_input in config.urban_planning.inputs):
        paths.append(config.trees.model_library_path)
        if config.air_purifiers.model_library_path is not None:
            paths.append(config.air_purifiers.model_library_path)
    return lightweight_state_fingerprint(
        {
            "stage": "shapefiles",
            "runtime_configuration": _shapefiles_runtime_configuration(config),
            "overpass_source": (
                "stage-owned-cache"
                if is_stage_owned_cache
                else str(overpass_json_path.resolve())
                if overpass_json_path is not None
                else config.inputs.overpass_url
            ),
        },
        paths,
    )


def _shapefiles_runtime_configuration(config: AppConfig) -> dict[str, Any]:
    """Return canonical effective settings that can change Stage 1 outputs."""

    return {
        "region": {
            "name": config.region.name,
            "center_lat": config.region.center_lat,
            "center_lon": config.region.center_lon,
            "crs": config.region.crs,
            "inner_diameter_m": config.region.inner_diameter_m,
            "outer_diameter_m": config.region.outer_diameter_m,
        },
        "inputs": {
            "overpass_url": config.inputs.overpass_url,
            "overpass_timeout_s": config.inputs.overpass_timeout_s,
            "overpass_max_attempts": config.inputs.overpass_max_attempts,
            "overpass_retry_backoff_s": config.inputs.overpass_retry_backoff_s,
            "tree_overlap_tolerance_m": config.inputs.tree_overlap_tolerance_m,
        },
        "shapefiles": {
            "classification_rules": [
                {
                    "category": rule.category,
                    "group_tag": rule.group_tag,
                    "match_any": list(rule.match_any),
                }
                for rule in config.shapefiles.classification_rules
            ],
            "surface_precedence": list(config.shapefiles.surface_precedence),
            "supplemental": [
                {
                    "name": supplemental.name,
                    "path": str(supplemental.path.resolve()),
                    "crs": supplemental.crs,
                    "category": supplemental.category,
                    "group_tag": supplemental.group_tag,
                    "enabled": supplemental.enabled,
                }
                for supplemental in config.shapefiles.supplemental
            ],
        },
        "urban_planning": {
            "inputs": [
                {
                    "name": planning_input.name,
                    "path": str(planning_input.path.resolve()),
                    "crs": planning_input.crs,
                    "enabled": planning_input.enabled,
                }
                for planning_input in config.urban_planning.inputs
            ],
            "tree_model_library_path": str(config.trees.model_library_path.resolve()),
            "air_purifier_model_library_path": (
                str(config.air_purifiers.model_library_path.resolve())
                if config.air_purifiers.model_library_path is not None
                else None
            ),
        },
        "imagery": {
            "sources": [
                {
                    "name": source.name,
                    "type": source.type,
                    "url": source.url,
                    "layer": source.layer,
                    "enabled": source.enabled,
                    "crs": source.crs,
                    "format": source.format,
                    "width": source.width,
                    "height": source.height,
                    "style": source.style,
                    "transparent": source.transparent,
                }
                for source in config.imagery.sources
            ]
        },
        "building_roof_default_base_height_m": (
            config.city_models.building_roof_default_base_height_m
        ),
    }


def build_tag_inventory_query(config: AppConfig) -> str:
    region = config.region
    radius_m = region.outer_diameter_m / 2.0
    center = f"{region.center_lat:.8f},{region.center_lon:.8f}"
    timeout_s = int(math.ceil(config.inputs.overpass_timeout_s))
    return "\n".join(
        (
            f"[out:json][timeout:{timeout_s}];",
            "(",
            f"  nwr(around:{radius_m:.1f},{center});",
            ");",
            "out tags center;",
        )
    )


def build_overpass_query(config: AppConfig) -> str:
    return _build_overpass_query_from_selectors(config, OVERPASS_GEOMETRY_SELECTOR_TEMPLATES)


def build_overpass_query_batches(config: AppConfig) -> list[str]:
    return [
        _build_overpass_query_from_selectors(
            config,
            OVERPASS_GEOMETRY_SELECTOR_TEMPLATES[start:start + OVERPASS_GEOMETRY_BATCH_SIZE],
        )
        for start in range(0, len(OVERPASS_GEOMETRY_SELECTOR_TEMPLATES), OVERPASS_GEOMETRY_BATCH_SIZE)
    ]


def _build_overpass_query_from_selectors(
    config: AppConfig,
    selectors: tuple[str, ...],
) -> str:
    region = config.region
    radius_m = region.outer_diameter_m / 2.0
    center = f"{region.center_lat:.8f},{region.center_lon:.8f}"
    body = "\n  ".join(selector.format(radius=f"{radius_m:.1f}", center=center) for selector in selectors)
    timeout_s = int(math.ceil(config.inputs.overpass_timeout_s))
    return "\n".join(
        (
            f"[out:json][timeout:{timeout_s}];",
            "(",
            f"  {body}",
            ");",
            "out body geom;",
        )
    )


def _feature_source_label(
    overpass_source: str,
    config: AppConfig,
    loaded_supplements: dict[str, list[dict[str, Any]]],
    urban_planning: Any,
) -> str:
    labels = [overpass_source]
    for item in config.shapefiles.supplemental:
        if not item.enabled:
            continue
        geometry_label = "tree points" if item.category == "trees" else "ROI-clipped polygons before overlap resolution"
        labels.append(
            f"supplemental shapefile {item.name}: {item.path} "
            f"({len(loaded_supplements.get(item.name, []))} accepted {geometry_label})"
        )
    for planning_input in config.urban_planning.inputs:
        if not planning_input.enabled:
            continue
        labels.append(
            f"urban-planning GeoJSON {planning_input.name}: {planning_input.path} "
            f"({urban_planning.per_input[planning_input.name]['accepted_features']} accepted planning points)"
        )
    return "; ".join(labels)


def _write_geojson(path: Path, features: list[dict[str, Any]]) -> None:
    collection = {
        "type": "FeatureCollection",
        "features": features,
    }
    atomic_write_text(path, json.dumps(collection, indent=2, sort_keys=True))


def _features_by_region(features: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    zones = {feature["properties"]["roi_zone"] for feature in features}
    if zones <= {"full"}:
        return {"full_region": features}
    return {
        "inner_region": [
            feature
            for feature in features
            if feature["properties"]["roi_zone"] == "inner"
        ],
        "annular_region": [
            feature
            for feature in features
            if feature["properties"]["roi_zone"] == "annular"
        ],
    }


def _build_gap_fill_features(features: list[dict[str, Any]], config: AppConfig) -> list[dict[str, Any]]:
    outer_roi_polygon = _circle_polygon_m(config.region.outer_diameter_m / 2.0)
    contributing_polygons = [
        geometry
        for feature in features
        if feature["properties"]["contributes_to_geometry"]
        for geometry in _feature_to_shapely_polygons(feature, config)
    ]
    if contributing_polygons:
        occupied = unary_union(contributing_polygons)
        missing = outer_roi_polygon.difference(occupied)
    else:
        missing = outer_roi_polygon
    missing = make_valid(missing)
    if config.region.inner_diameter_m is None:
        zone_polygons = [
            ("full", polygon)
            for polygon in _extract_polygons(missing)
        ]
    else:
        inner_roi_polygon = _circle_polygon_m(config.region.inner_diameter_m / 2.0)
        annular_roi_polygon = outer_roi_polygon.difference(inner_roi_polygon)
        zone_polygons = [
            ("inner", polygon)
            for polygon in _extract_polygons(make_valid(missing.intersection(inner_roi_polygon)))
        ]
        zone_polygons.extend(
            ("annular", polygon)
            for polygon in _extract_polygons(make_valid(missing.intersection(annular_roi_polygon)))
        )
    gap_fill_features: list[dict[str, Any]] = []
    for roi_zone, polygon in zone_polygons:
        if not polygon.is_empty and polygon.area > 0.01:
            gap_fill_features.append(_gap_fill_feature_from_polygon(polygon, len(gap_fill_features) + 1, config, roi_zone))
    return gap_fill_features


def _gap_fill_feature_from_polygon(polygon: Polygon, index: int, config: AppConfig, roi_zone: str) -> dict[str, Any]:
    coordinates = _polygon_m_to_lonlat_coordinates(polygon, config)
    centroid = polygon.centroid
    roi_distance_m = _point_norm_m((centroid.x, centroid.y))
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coordinates},
        "properties": {
            "osm_type": "generated",
            "osm_id": f"gap_fill_{index}",
            "category": "gap_fill",
            "group_tag": "gap_fill",
            "source_tag": "generated=roi_difference",
            "source": "roi_polygon_difference",
            "review_status": "generated",
            "contributes_to_geometry": True,
            "geometry_role": "generated_gap_fill_contributing_polygon",
            "roi_zone": roi_zone,
            "reconstruction_scope": _reconstruction_scope(roi_zone),
            "include_in_building_lod22_reconstruction": False,
            "centroid_distance_m": round(roi_distance_m, 3),
            "roi_distance_m": round(roi_distance_m, 3),
            "area_m2": round(polygon.area, 3),
            "tags": {},
        },
    }


def _route_urban_planning_feature(feature: dict[str, Any]) -> dict[str, Any]:
    """Add the established Stage 1 reference properties for a planning point."""

    routed: dict[str, Any] = {
        "type": "Feature",
        "geometry": dict(feature["geometry"]),
        "properties": dict(feature["properties"]),
    }
    properties = routed["properties"]
    kind = properties["kind"]
    feature_id = properties["id"]
    input_id = properties["urban_planning_input_id"]
    properties.update(
        {
            "osm_type": "urban_planning",
            "osm_id": feature_id,
            "category": "trees" if kind == "tree" else "air_purifiers",
            "group_tag": "tree" if kind == "tree" else "air_purifier",
            "source_tag": f"urban_planning={input_id}",
            "source_type": "urban_planning",
            "geometry_role": (
                "tree_reference_point" if kind == "tree" else "air_purifier_reference_point"
            ),
            "reconstruction_scope": _reconstruction_scope(properties["roi_zone"]),
            "include_in_building_lod22_reconstruction": False,
            "centroid_distance_m": properties["roi_distance_m"],
        }
    )
    if kind == "tree":
        properties["direct_model_category"] = properties["model"]
        properties["tags"] = {}
    else:
        properties["purifier_id"] = feature_id
    return routed


def _roi_bbox_lon_lat(config: AppConfig) -> tuple[float, float, float, float]:
    radius_m = config.region.outer_diameter_m / 2.0
    lat_delta = math.degrees(radius_m / EARTH_RADIUS_M)
    lon_delta = math.degrees(radius_m / (EARTH_RADIUS_M * math.cos(math.radians(config.region.center_lat))))
    return (
        config.region.center_lon - lon_delta,
        config.region.center_lat - lat_delta,
        config.region.center_lon + lon_delta,
        config.region.center_lat + lat_delta,
    )
