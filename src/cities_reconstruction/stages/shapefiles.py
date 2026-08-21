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

from shapely.geometry import MultiPolygon, Polygon, mapping
from shapely.ops import unary_union
from shapely.validation import make_valid

from cities_reconstruction.artifacts import lightweight_state_fingerprint
from cities_reconstruction.config import (
    AppConfig,
    ConfigError,
    SupplementalShapefileConfig,
)
from cities_reconstruction.stage_contract import (
    ArtifactReference,
    JsonValue,
    StageManifest,
    StageStatus,
    invalidate_stage_manifests,
)
from cities_reconstruction.stage_layout import StageId, stage_output_directory
from cities_reconstruction.stage_result import StageResult
from cities_reconstruction.stages.shapefiles_diagnostics import (
    build_geometry_diagnostics,
    build_summary,
    non_contributing_features,
    supplemental_surface_input_diagnostics,
    supplemental_tree_input_diagnostics,
    urban_planning_diagnostics,
)
from cities_reconstruction.stages.shapefiles_inputs import (
    fetch_imagery_diagnostics,
    load_or_fetch_geometry_batches,
    load_or_fetch_overpass,
    read_dbf_attributes,
    read_point_records,
    read_polygon_records,
)
from cities_reconstruction.stages.shapefiles_publication import (
    ShapefilesPublicationInput,
    publish_shapefiles_manifest,
)
from cities_reconstruction.stages.shapefiles_rendering import (
    render_imagery_overlay_html,
    render_preview_html,
)
from cities_reconstruction.stages.shapefiles_reporting import render_report
from cities_reconstruction.stages.shapefiles_transformation import (
    EARTH_RADIUS_M,
    _centroid,
    _distance_m,
    _geometry_distance_to_region_center_m,
    _geometry_role,
    _include_in_building_lod22_reconstruction,
    _point_norm_m,
    _project_coordinate_m,
    _reconstruction_scope,
    _roi_zone,
    build_tag_inventory,
    overpass_to_features,
)
from cities_reconstruction.urban_planning import load_inputs as load_urban_planning_inputs

ROI_FILL_SEGMENTS = 256
TREE_SPECIES_ATTRIBUTE_KEYS = ("species", "specie", "genus", "taxon", "nome_specie")
TREE_DBH_ATTRIBUTE_KEYS = (
    "dbh",
    "dbh_cm",
    "diameter_breast_height",
    "diametro",
    "diametro_cm",
    "diameter",
    "diameter_m",
    "diam_m",
    "trunk_diameter",
)
TREE_CIRCUMFERENCE_ATTRIBUTE_KEYS = ("circumference", "circumference_cm", "circonf", "circonf_cm", "circonferenza", "circonferenza_cm")
STAGE_ID = StageId.SHAPEFILES

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
    output_dir.mkdir(parents=True, exist_ok=True)
    invalidate_stage_manifests(output_dir)
    overpass_json_path = overpass_json_path.resolve() if overpass_json_path is not None else None

    tag_inventory_query = build_tag_inventory_query(config)
    tag_inventory_query_path = output_dir / "tag_inventory_query.txt"
    tag_inventory_query_path.write_text(tag_inventory_query, encoding="utf-8")

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
    tag_inventory_raw_path.write_text(json.dumps(tag_inventory_raw_data, indent=2, sort_keys=True), encoding="utf-8")
    tag_inventory = build_tag_inventory(tag_inventory_raw_data, source=tag_inventory_source, config=config)
    tag_inventory_path = output_dir / "tag_inventory.json"
    tag_inventory_path.write_text(json.dumps(tag_inventory, indent=2, sort_keys=True), encoding="utf-8")

    query = build_overpass_query(config)
    query_path = output_dir / "overpass_query.txt"
    query_path.write_text(query, encoding="utf-8")

    raw_data, source = load_or_fetch_geometry_batches(
        config,
        output_dir,
        overpass_json_path,
        query=query,
        batch_queries=build_overpass_query_batches(config),
    )
    raw_path = output_dir / "overpass_raw.json"
    raw_path.write_text(json.dumps(raw_data, indent=2, sort_keys=True), encoding="utf-8")
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
    diagnostics_path.write_text(json.dumps(diagnostics, indent=2, sort_keys=True), encoding="utf-8")
    diagnostics_geojson_path = output_dir / "non_contributing_features.geojson"
    _write_geojson(diagnostics_geojson_path, non_contributing_features(reference_features))
    imagery_diagnostics = fetch_imagery_diagnostics(
        config,
        output_dir,
        _roi_bbox_lon_lat(config),
    )
    imagery_diagnostics_path = output_dir / "imagery_diagnostics.json"
    imagery_diagnostics_path.write_text(json.dumps(imagery_diagnostics, indent=2, sort_keys=True), encoding="utf-8")
    imagery_overlay_path = output_dir / "imagery_overlay.html"
    imagery_overlay_path.write_text(
        render_imagery_overlay_html(
            config,
            reference_features,
            imagery_diagnostics,
            tree_overlap_filter,
            categories=CATEGORIES,
        ),
        encoding="utf-8",
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
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    preview_path = output_dir / "preview.html"
    preview_path.write_text(
        render_preview_html(config, reference_features, summary, categories=CATEGORIES),
        encoding="utf-8",
    )

    report_path = output_dir / "report.md"
    report_path.write_text(
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
        encoding="utf-8",
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


def _load_supplemental_tree_features(
    config: AppConfig,
    tree_input: SupplementalShapefileConfig,
) -> list[dict[str, Any]]:
    path = tree_input.path
    if path.suffix.lower() != ".shp":
        raise ConfigError(
            f"shapefiles.supplemental[{tree_input.name}].path must point to an ESRI .shp file: {path}"
        )
    if not path.exists():
        raise ConfigError(f"supplemental input '{tree_input.name}' shapefile does not exist: {path}")

    records = read_point_records(path, tree_input.name)
    attributes = read_dbf_attributes(path.with_suffix(".dbf"))
    features: list[dict[str, Any]] = []
    record_index = 0
    for shape_index, (record_number, points, _is_null) in enumerate(records):
        record_attributes = attributes[shape_index] if shape_index < len(attributes) else {}
        if record_attributes is None:
            continue
        for point_index, (x, y) in enumerate(points, start=1):
            lon, lat = _shapefile_xy_to_lonlat(
                x,
                y,
                tree_input.crs,
                f"shapefiles.supplemental[{tree_input.name}].crs",
            )
            feature = _tree_shapefile_feature(
                lon=lon,
                lat=lat,
                config=config,
                tree_input=tree_input,
                path=path,
                attributes=record_attributes,
                record_number=record_number,
                point_index=point_index,
                sequence_index=record_index + 1,
            )
            if feature is not None:
                features.append(feature)
            record_index += 1
    return features


def _load_supplemental_surface_features(
    config: AppConfig,
    surface: SupplementalShapefileConfig,
) -> list[dict[str, Any]]:
    path = surface.path
    if path.suffix.lower() != ".shp":
        raise ConfigError(f"shapefiles.supplemental[{surface.name}].path must point to an ESRI .shp file: {path}")
    if not path.exists():
        raise ConfigError(f"supplemental input '{surface.name}' shapefile does not exist: {path}")

    records = read_polygon_records(path, surface.name)
    attributes = read_dbf_attributes(path.with_suffix(".dbf"))
    features: list[dict[str, Any]] = []
    for shape_index, (record_number, polygons) in enumerate(records):
        record_attributes = attributes[shape_index] if shape_index < len(attributes) else {}
        if record_attributes is None:
            continue
        for polygon_index, polygon in enumerate(polygons, start=1):
            transformed = Polygon(
                [
                    _shapefile_xy_to_lonlat(x, y, surface.crs, f"shapefiles.supplemental[{surface.name}].crs")
                    for x, y in polygon.exterior.coords
                ],
                [
                    [
                        _shapefile_xy_to_lonlat(x, y, surface.crs, f"shapefiles.supplemental[{surface.name}].crs")
                        for x, y in ring.coords
                    ]
                    for ring in polygon.interiors
                ],
            )
            feature = _surface_shapefile_feature(
                polygon=transformed,
                config=config,
                path=path,
                source_crs=surface.crs,
                attributes=record_attributes,
                record_number=record_number,
                polygon_index=polygon_index,
                category=surface.category,
                group_tag=surface.group_tag or "",
                source_name=surface.name,
            )
            if feature is not None:
                features.append(feature)
    return features


def _surface_shapefile_feature(
    *,
    polygon: Polygon,
    config: AppConfig,
    path: Path,
    source_crs: str,
    attributes: dict[str, Any],
    record_number: int,
    polygon_index: int,
    category: str,
    group_tag: str,
    source_name: str,
) -> dict[str, Any] | None:
    local_polygon = _coordinates_to_polygon_m(mapping(polygon)["coordinates"], config)
    clipped = make_valid(local_polygon).intersection(_circle_polygon_m(config.region.outer_diameter_m / 2.0))
    polygons = [item for item in _extract_polygons(clipped) if item.area > 0.01]
    if not polygons:
        return None
    geometry: dict[str, Any]
    coordinates = [_polygon_m_to_lonlat_coordinates(item, config) for item in polygons]
    if len(coordinates) == 1:
        geometry = {"type": "Polygon", "coordinates": coordinates[0]}
    else:
        geometry = {"type": "MultiPolygon", "coordinates": coordinates}
    roi_distance_m = _geometry_distance_to_region_center_m(geometry, config)
    roi_zone = _roi_zone(roi_distance_m, config)
    if roi_zone is None:
        return None
    centroid = _centroid(geometry)
    centroid_distance_m = _distance_m(
        config.region.center_lat,
        config.region.center_lon,
        centroid[1],
        centroid[0],
    )
    feature_id = f"{path.stem}_{record_number}"
    if polygon_index > 1:
        feature_id = f"{feature_id}_{polygon_index}"
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "osm_type": "supplemental",
            "osm_id": feature_id,
            "category": category,
            "group_tag": group_tag,
            "source_tag": f"supplemental={source_name}",
            "source_type": "supplemental",
            "supplemental_input_id": source_name,
            "source": str(path),
            "source_crs": source_crs,
            "source_attributes": attributes,
            "record_number": record_number,
            "contributes_to_geometry": True,
            "geometry_role": "polygon_surface",
            "roi_zone": roi_zone,
            "reconstruction_scope": _reconstruction_scope(roi_zone),
            "include_in_building_lod22_reconstruction": _include_in_building_lod22_reconstruction(category, roi_zone),
            "centroid_distance_m": round(centroid_distance_m, 3),
            "roi_distance_m": round(roi_distance_m, 3),
            "area_m2": round(sum(item.area for item in polygons), 3),
            "tags": {},
        },
    }


def _shapefile_xy_to_lonlat(
    x: float,
    y: float,
    source_crs: str,
    config_key: str,
) -> tuple[float, float]:
    crs = _normalized_crs(source_crs)
    if crs == "EPSG:4326":
        return x, y
    if crs == "EPSG:25832":
        return _transverse_mercator_to_lonlat(
            x,
            y,
            semi_major=6378137.0,
            inverse_flattening=298.257223563,
            central_meridian_deg=9.0,
            scale=0.9996,
            false_easting=500000.0,
        )
    if crs == "EPSG:3003":
        lon, lat = _transverse_mercator_to_lonlat(
            x,
            y,
            semi_major=6378388.0,
            inverse_flattening=297.0,
            central_meridian_deg=9.0,
            scale=0.9996,
            false_easting=1500000.0,
        )
        return _helmert_to_wgs84_lonlat(
            lon,
            lat,
            semi_major=6378388.0,
            inverse_flattening=297.0,
            tx=-104.1,
            ty=-49.1,
            tz=-9.9,
            rx_arcsec=0.971,
            ry_arcsec=-2.917,
            rz_arcsec=0.714,
            scale_ppm=-11.68,
        )
    raise ConfigError(
        f"{config_key} currently supports EPSG:4326, EPSG:25832, and EPSG:3003"
    )


def _normalized_crs(value: str) -> str:
    return value.strip().upper().replace("::", ":")


def _transverse_mercator_to_lonlat(
    easting: float,
    northing: float,
    *,
    semi_major: float,
    inverse_flattening: float,
    central_meridian_deg: float,
    scale: float,
    false_easting: float,
) -> tuple[float, float]:
    flattening = 1.0 / inverse_flattening
    eccentricity_sq = flattening * (2.0 - flattening)
    second_eccentricity_sq = eccentricity_sq / (1.0 - eccentricity_sq)
    x = easting - false_easting
    meridional_arc = northing / scale
    mu = meridional_arc / (
        semi_major
        * (
            1.0
            - eccentricity_sq / 4.0
            - 3.0 * eccentricity_sq**2 / 64.0
            - 5.0 * eccentricity_sq**3 / 256.0
        )
    )
    e1 = (1.0 - math.sqrt(1.0 - eccentricity_sq)) / (1.0 + math.sqrt(1.0 - eccentricity_sq))
    footpoint_lat = (
        mu
        + (3.0 * e1 / 2.0 - 27.0 * e1**3 / 32.0) * math.sin(2.0 * mu)
        + (21.0 * e1**2 / 16.0 - 55.0 * e1**4 / 32.0) * math.sin(4.0 * mu)
        + (151.0 * e1**3 / 96.0) * math.sin(6.0 * mu)
        + (1097.0 * e1**4 / 512.0) * math.sin(8.0 * mu)
    )
    sin_lat = math.sin(footpoint_lat)
    cos_lat = math.cos(footpoint_lat)
    tan_lat = math.tan(footpoint_lat)
    n1 = semi_major / math.sqrt(1.0 - eccentricity_sq * sin_lat**2)
    r1 = semi_major * (1.0 - eccentricity_sq) / (1.0 - eccentricity_sq * sin_lat**2) ** 1.5
    t1 = tan_lat**2
    c1 = second_eccentricity_sq * cos_lat**2
    d = x / (n1 * scale)
    lat = footpoint_lat - (n1 * tan_lat / r1) * (
        d**2 / 2.0
        - (5.0 + 3.0 * t1 + 10.0 * c1 - 4.0 * c1**2 - 9.0 * second_eccentricity_sq) * d**4 / 24.0
        + (
            61.0
            + 90.0 * t1
            + 298.0 * c1
            + 45.0 * t1**2
            - 252.0 * second_eccentricity_sq
            - 3.0 * c1**2
        )
        * d**6
        / 720.0
    )
    lon = math.radians(central_meridian_deg) + (
        d
        - (1.0 + 2.0 * t1 + c1) * d**3 / 6.0
        + (5.0 - 2.0 * c1 + 28.0 * t1 - 3.0 * c1**2 + 8.0 * second_eccentricity_sq + 24.0 * t1**2)
        * d**5
        / 120.0
    ) / cos_lat
    return math.degrees(lon), math.degrees(lat)


def _helmert_to_wgs84_lonlat(
    lon_deg: float,
    lat_deg: float,
    *,
    semi_major: float,
    inverse_flattening: float,
    tx: float,
    ty: float,
    tz: float,
    rx_arcsec: float,
    ry_arcsec: float,
    rz_arcsec: float,
    scale_ppm: float,
) -> tuple[float, float]:
    x, y, z = _geodetic_to_cartesian(
        lon_deg,
        lat_deg,
        semi_major=semi_major,
        inverse_flattening=inverse_flattening,
    )
    rotation_scale = math.pi / (180.0 * 3600.0)
    rx = rx_arcsec * rotation_scale
    ry = ry_arcsec * rotation_scale
    rz = rz_arcsec * rotation_scale
    scale = 1.0 + scale_ppm * 1.0e-6
    wgs84_x = tx + scale * x - rz * y + ry * z
    wgs84_y = ty + rz * x + scale * y - rx * z
    wgs84_z = tz - ry * x + rx * y + scale * z
    return _cartesian_to_geodetic(
        wgs84_x,
        wgs84_y,
        wgs84_z,
        semi_major=6378137.0,
        inverse_flattening=298.257223563,
    )


def _geodetic_to_cartesian(
    lon_deg: float,
    lat_deg: float,
    *,
    semi_major: float,
    inverse_flattening: float,
) -> tuple[float, float, float]:
    lon = math.radians(lon_deg)
    lat = math.radians(lat_deg)
    flattening = 1.0 / inverse_flattening
    eccentricity_sq = flattening * (2.0 - flattening)
    prime_vertical_radius = semi_major / math.sqrt(1.0 - eccentricity_sq * math.sin(lat) ** 2)
    x = prime_vertical_radius * math.cos(lat) * math.cos(lon)
    y = prime_vertical_radius * math.cos(lat) * math.sin(lon)
    z = prime_vertical_radius * (1.0 - eccentricity_sq) * math.sin(lat)
    return x, y, z


def _cartesian_to_geodetic(
    x: float,
    y: float,
    z: float,
    *,
    semi_major: float,
    inverse_flattening: float,
) -> tuple[float, float]:
    flattening = 1.0 / inverse_flattening
    semi_minor = semi_major * (1.0 - flattening)
    eccentricity_sq = flattening * (2.0 - flattening)
    second_eccentricity_sq = (semi_major**2 - semi_minor**2) / semi_minor**2
    horizontal_radius = math.hypot(x, y)
    theta = math.atan2(z * semi_major, horizontal_radius * semi_minor)
    lon = math.atan2(y, x)
    lat = math.atan2(
        z + second_eccentricity_sq * semi_minor * math.sin(theta) ** 3,
        horizontal_radius - eccentricity_sq * semi_major * math.cos(theta) ** 3,
    )
    return math.degrees(lon), math.degrees(lat)


def _tree_shapefile_feature(
    *,
    lon: float,
    lat: float,
    config: AppConfig,
    tree_input: SupplementalShapefileConfig,
    path: Path,
    attributes: dict[str, Any],
    record_number: int,
    point_index: int,
    sequence_index: int,
) -> dict[str, Any] | None:
    geometry = {"type": "Point", "coordinates": [lon, lat]}
    centroid_distance_m = _distance_m(
        config.region.center_lat,
        config.region.center_lon,
        lat,
        lon,
    )
    roi_distance_m = _geometry_distance_to_region_center_m(geometry, config)
    roi_zone = _roi_zone(roi_distance_m, config)
    if roi_zone is None:
        return None
    tree_id = f"{path.stem}_{record_number}"
    if point_index > 1:
        tree_id = f"{tree_id}_{point_index}"
    tags = _tree_tags_from_shapefile_attributes(attributes)
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "osm_type": "supplemental",
            "osm_id": tree_id,
            "category": "trees",
            "group_tag": "tree",
            "source_tag": f"supplemental={tree_input.name}",
            "source_type": "supplemental",
            "supplemental_input_id": tree_input.name,
            "source": str(path),
            "source_crs": tree_input.crs,
            "source_attributes": attributes,
            "record_number": record_number,
            "sequence_index": sequence_index,
            "contributes_to_geometry": False,
            "geometry_role": _geometry_role(geometry),
            "roi_zone": roi_zone,
            "reconstruction_scope": _reconstruction_scope(roi_zone),
            "include_in_building_lod22_reconstruction": False,
            "centroid_distance_m": round(centroid_distance_m, 3),
            "roi_distance_m": round(roi_distance_m, 3),
            "tags": tags,
        },
    }


def _remove_overpass_trees_overlapping_supplemental_trees(
    features: list[dict[str, Any]],
    supplemental_tree_features: list[dict[str, Any]],
    tolerance_m: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    diagnostics = {
        "enabled": bool(supplemental_tree_features) and tolerance_m > 0.0,
        "tolerance_m": tolerance_m,
        "supplemental_tree_count": len(supplemental_tree_features),
        "overpass_tree_count": sum(1 for feature in features if _is_overpass_tree_feature(feature)),
        "removed_overpass_tree_count": 0,
        "removed_overpass_tree_ids": [],
        "removed_overpass_tree_markers": [],
    }
    if not diagnostics["enabled"]:
        return features, diagnostics

    supplemental_points = [
        (
            float(coordinates[1]),
            float(coordinates[0]),
            feature.get("properties", {}).get("osm_id"),
        )
        for feature in supplemental_tree_features
        if (coordinates := _point_coordinates(feature)) is not None
    ]
    if not supplemental_points:
        diagnostics["enabled"] = False
        return features, diagnostics

    filtered_features: list[dict[str, Any]] = []
    removed_ids: list[Any] = []
    removed_markers: list[dict[str, Any]] = []
    for feature in features:
        if not _is_overpass_tree_feature(feature):
            filtered_features.append(feature)
            continue
        coordinates = _point_coordinates(feature)
        if coordinates is None:
            filtered_features.append(feature)
            continue
        lon = float(coordinates[0])
        lat = float(coordinates[1])
        duplicate_match = _nearest_supplemental_tree_within_tolerance(
            lat, lon, supplemental_points, tolerance_m
        )
        if duplicate_match is not None:
            nearest_distance, nearest_supplemental_tree_id = duplicate_match
            osm_id = feature.get("properties", {}).get("osm_id")
            removed_ids.append(osm_id)
            if len(removed_markers) < 200:
                removed_markers.append(
                    {
                        "osm_id": osm_id,
                        "coordinates": [lon, lat],
                        "nearest_supplemental_tree_distance_m": round(nearest_distance, 3),
                        "nearest_supplemental_tree_id": nearest_supplemental_tree_id,
                    }
                )
            continue
        filtered_features.append(feature)

    diagnostics["removed_overpass_tree_count"] = len(removed_ids)
    diagnostics["removed_overpass_tree_ids"] = removed_ids[:200]
    diagnostics["removed_overpass_tree_markers"] = removed_markers
    return filtered_features, diagnostics


def _difference_surface_feature(
    feature: dict[str, Any],
    mask: Any,
    config: AppConfig,
) -> tuple[dict[str, Any] | None, float]:
    source_polygons = _feature_to_shapely_polygons(feature, config)
    if not source_polygons:
        return feature, 0.0
    source_geometry = unary_union(source_polygons)
    result = source_geometry if mask.is_empty else make_valid(source_geometry.difference(mask))
    polygons = [polygon for polygon in _extract_polygons(result) if polygon.area > 0.01]
    remaining_area = sum(polygon.area for polygon in polygons)
    removed_area = max(0.0, source_geometry.area - remaining_area)
    if not polygons:
        return None, removed_area
    updated = dict(feature)
    updated["geometry"] = _local_polygons_geojson_geometry(polygons, config)
    properties = dict(feature.get("properties", {}))
    properties["area_m2"] = round(remaining_area, 3)
    if removed_area > 0.01:
        properties["overlap_clipped"] = True
        properties["overlap_removed_area_m2"] = round(
            float(properties.get("overlap_removed_area_m2", 0.0)) + removed_area,
            3,
        )
    updated["properties"] = properties
    return updated, removed_area


def _local_polygons_geojson_geometry(polygons: list[Polygon], config: AppConfig) -> dict[str, Any]:
    coordinates = [_polygon_m_to_lonlat_coordinates(polygon, config) for polygon in polygons]
    if len(coordinates) == 1:
        return {"type": "Polygon", "coordinates": coordinates[0]}
    return {"type": "MultiPolygon", "coordinates": coordinates}


def _feature_union_m(features: list[dict[str, Any]], config: AppConfig) -> Any:
    polygons = [
        polygon
        for feature in features
        for polygon in _feature_to_shapely_polygons(feature, config)
    ]
    return unary_union(polygons) if polygons else Polygon()


def _resolve_surface_overlaps(
    features: list[dict[str, Any]],
    config: AppConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates = [
        (index, feature)
        for index, feature in enumerate(features)
        if feature.get("properties", {}).get("contributes_to_geometry")
    ]
    candidates.sort(key=lambda item: (_surface_precedence_rank(item[1], config), item[0]))

    occupied: Any = Polygon()
    resolved_by_index: dict[int, dict[str, Any]] = {}
    by_category: dict[str, dict[str, float | int]] = {}
    by_supplemental: dict[str, dict[str, float | int]] = {}
    clipped_count = 0
    removed_count = 0
    removed_area = 0.0
    for index, feature in candidates:
        category = str(feature.get("properties", {}).get("category"))
        stats = by_category.setdefault(
            category,
            {
                "input_features": 0,
                "accepted_features": 0,
                "clipped_features": 0,
                "removed_features": 0,
                "removed_area_m2": 0.0,
            },
        )
        surface_id = feature.get("properties", {}).get("supplemental_input_id")
        surface_stats = None
        if isinstance(surface_id, str):
            surface_stats = by_supplemental.setdefault(
                surface_id,
                {
                    "input_features": 0,
                    "accepted_features": 0,
                    "clipped_features": 0,
                    "removed_features": 0,
                    "removed_area_m2": 0.0,
                },
            )
            surface_stats["input_features"] = int(surface_stats["input_features"]) + 1
        stats["input_features"] = int(stats["input_features"]) + 1
        clipped, feature_removed_area = _difference_surface_feature(feature, occupied, config)
        removed_area += feature_removed_area
        stats["removed_area_m2"] = float(stats["removed_area_m2"]) + feature_removed_area
        if surface_stats is not None:
            surface_stats["removed_area_m2"] = float(surface_stats["removed_area_m2"]) + feature_removed_area
        if clipped is None:
            removed_count += 1
            stats["removed_features"] = int(stats["removed_features"]) + 1
            if surface_stats is not None:
                surface_stats["removed_features"] = int(surface_stats["removed_features"]) + 1
            continue
        resolved_by_index[index] = clipped
        stats["accepted_features"] = int(stats["accepted_features"]) + 1
        if surface_stats is not None:
            surface_stats["accepted_features"] = int(surface_stats["accepted_features"]) + 1
        if feature_removed_area > 0.01:
            clipped_count += 1
            stats["clipped_features"] = int(stats["clipped_features"]) + 1
            if surface_stats is not None:
                surface_stats["clipped_features"] = int(surface_stats["clipped_features"]) + 1
        clipped_geometry = _feature_union_m([clipped], config)
        occupied = clipped_geometry if occupied.is_empty else make_valid(occupied.union(clipped_geometry))

    resolved = [
        resolved_by_index[index] if index in resolved_by_index else feature
        for index, feature in enumerate(features)
        if not feature.get("properties", {}).get("contributes_to_geometry") or index in resolved_by_index
    ]
    for stats in by_category.values():
        stats["removed_area_m2"] = round(float(stats["removed_area_m2"]), 3)
    for stats in by_supplemental.values():
        stats["removed_area_m2"] = round(float(stats["removed_area_m2"]), 3)
    return resolved, {
        "precedence": list(config.shapefiles.surface_precedence),
        "input_polygon_features": len(candidates),
        "accepted_polygon_features": len(resolved_by_index),
        "clipped_polygon_features": clipped_count,
        "removed_polygon_features": removed_count,
        "removed_overlap_area_m2": round(removed_area, 3),
        "by_category": dict(sorted(by_category.items())),
        "by_supplemental": dict(sorted(by_supplemental.items())),
        "policy": "Contributing polygons are processed in configured precedence order. Each polygon is clipped against all previously accepted higher- or equal-priority surface coverage, producing mutually disjoint Stage 1 surfaces.",
    }


def _surface_precedence_rank(feature: dict[str, Any], config: AppConfig) -> int:
    properties = feature.get("properties", {})
    category = str(properties.get("category", ""))
    group_tag = str(properties.get("group_tag", ""))
    surface_id = str(properties.get("supplemental_input_id", ""))
    selectors = (
        f"supplemental:{surface_id}" if surface_id else "",
        f"{category}:{group_tag}" if group_tag else "",
        category,
    )
    for selector in selectors:
        if selector in config.shapefiles.surface_precedence:
            return config.shapefiles.surface_precedence.index(selector)
    raise ConfigError(f"no shapefiles.surface_precedence entry matches {category}:{group_tag}")


def _nearest_supplemental_tree_within_tolerance(
    overpass_lat: float,
    overpass_lon: float,
    supplemental_points: list[tuple[float, float, Any]],
    tolerance_m: float,
) -> tuple[float, Any] | None:
    nearest_match: tuple[float, Any] | None = None
    for supplemental_lat, supplemental_lon, supplemental_tree_id in supplemental_points:
        distance_m = _distance_m(overpass_lat, overpass_lon, supplemental_lat, supplemental_lon)
        if distance_m <= tolerance_m and (nearest_match is None or distance_m < nearest_match[0]):
            nearest_match = (distance_m, supplemental_tree_id)
    return nearest_match


def _is_overpass_tree_feature(feature: dict[str, Any]) -> bool:
    properties = feature.get("properties", {})
    return (
        properties.get("category") == "trees"
        and properties.get("source_tag") == "natural=tree"
        and properties.get("source_type") != "supplemental"
    )


def _point_coordinates(feature: dict[str, Any]) -> list[Any] | tuple[Any, ...] | None:
    geometry = feature.get("geometry", {})
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list | tuple) or len(coordinates) < 2:
        return None
    return coordinates


def _tree_tags_from_shapefile_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    tags: dict[str, Any] = {"natural": "tree"}
    normalized = {_normalize_attribute_key(key): value for key, value in attributes.items()}
    species = _first_attribute_value(normalized, TREE_SPECIES_ATTRIBUTE_KEYS)
    if species is not None:
        tags["species"] = species
    genus = _first_attribute_value(normalized, ("genus",))
    if genus is not None:
        tags["genus"] = genus
    dbh = _first_attribute_value(normalized, TREE_DBH_ATTRIBUTE_KEYS)
    if dbh is not None:
        tags["dbh"] = dbh
        diameter_m = _dbh_to_diameter_m(dbh)
        if diameter_m is not None:
            tags["diameter"] = round(diameter_m, 4)
    circumference = _first_attribute_value(normalized, TREE_CIRCUMFERENCE_ATTRIBUTE_KEYS)
    if circumference is not None:
        tags["source_circumference"] = circumference
        circumference_m = _circumference_to_m(circumference)
        if circumference_m is not None:
            tags["circumference"] = round(circumference_m, 4)
    return tags


def _normalize_attribute_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_").replace("-", "_")


def _first_attribute_value(attributes: dict[str, Any], keys: tuple[str, ...]) -> Any | None:
    for key in keys:
        value = attributes.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or stripped in {"-", "--"} or stripped.lower() in {"unknown", "sconosciuto", "non noto", "n/a"}:
                continue
        return value
    return None


def _dbh_to_diameter_m(value: Any) -> float | None:
    numeric = _numeric_attribute(value)
    if numeric is None or numeric <= 0.0:
        return None
    if numeric > 2.0:
        return numeric / 100.0
    return numeric


def _circumference_to_m(value: Any) -> float | None:
    numeric = _numeric_attribute(value)
    if numeric is None or numeric <= 0.0:
        return None
    if numeric > 10.0:
        return numeric / 100.0
    return numeric


def _numeric_attribute(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower().replace("cm", "").replace("centimetri", "").replace("centimeters", "")
    cleaned = cleaned.replace("metres", "").replace("meters", "").replace("meter", "").replace("m", "")
    cleaned = cleaned.replace(",", ".").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


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
    path.write_text(json.dumps(collection, indent=2, sort_keys=True), encoding="utf-8")


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


def _circle_polygon_m(radius: float) -> Polygon:
    points = [
        (
            math.cos(2.0 * math.pi * index / ROI_FILL_SEGMENTS) * radius,
            math.sin(2.0 * math.pi * index / ROI_FILL_SEGMENTS) * radius,
        )
        for index in range(ROI_FILL_SEGMENTS)
    ]
    return Polygon(points)


def _feature_to_shapely_polygons(feature: dict[str, Any], config: AppConfig) -> list[Polygon]:
    geometry = feature["geometry"]
    if geometry["type"] == "Polygon":
        polygon = _coordinates_to_polygon_m(geometry["coordinates"], config)
        return _extract_polygons(make_valid(polygon))
    if geometry["type"] == "MultiPolygon":
        polygons = []
        for coordinates in geometry["coordinates"]:
            polygon = _coordinates_to_polygon_m(coordinates, config)
            polygons.extend(_extract_polygons(make_valid(polygon)))
        return polygons
    return []


def _coordinates_to_polygon_m(coordinates: list[Any], config: AppConfig) -> Polygon:
    shell = [_project_coordinate_m(point, config) for point in coordinates[0]]
    holes = [
        [_project_coordinate_m(point, config) for point in ring]
        for ring in coordinates[1:]
        if len(ring) >= 4
    ]
    return Polygon(shell, holes)


def _extract_polygons(geometry: Any) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if hasattr(geometry, "geoms"):
        return [
            polygon
            for item in geometry.geoms
            for polygon in _extract_polygons(item)
        ]
    return []


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


def _polygon_m_to_lonlat_coordinates(polygon: Polygon, config: AppConfig) -> list[list[list[float]]]:
    rings = [
        [_local_m_to_lonlat(x, y, config) for x, y in polygon.exterior.coords],
    ]
    rings.extend(
        [_local_m_to_lonlat(x, y, config) for x, y in interior.coords]
        for interior in polygon.interiors
    )
    return rings


def _local_m_to_lonlat(x_m: float, y_m: float, config: AppConfig) -> list[float]:
    lon = config.region.center_lon + math.degrees(x_m / (EARTH_RADIUS_M * math.cos(math.radians(config.region.center_lat))))
    lat = config.region.center_lat + math.degrees(y_m / EARTH_RADIUS_M)
    return [lon, lat]


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
