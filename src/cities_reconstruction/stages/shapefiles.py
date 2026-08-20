"""Geospatial feature retrieval for the first pipeline stage.

The implemented output format is GeoJSON. The stage keeps the surrounding
module name because the project-level workflow calls this step "shapefiles";
true ESRI Shapefile export will be added behind the same stage boundary when a
GIS stack is introduced.
"""

from __future__ import annotations

import json
import math
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from shapely.geometry import MultiPolygon, Polygon, mapping
from shapely.ops import unary_union
from shapely.validation import make_valid

from cities_reconstruction.artifacts import lightweight_state_fingerprint
from cities_reconstruction.config import (
    AppConfig,
    ConfigError,
    ImagerySourceConfig,
    SupplementalShapefileConfig,
)
from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    JsonValue,
    StageManifest,
    StageStatus,
    invalidate_stage_manifests,
    publish_stage_manifest,
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
from cities_reconstruction.stages.shapefiles_rendering import (
    render_imagery_overlay_html,
    render_preview_html,
)
from cities_reconstruction.stages.shapefiles_reporting import render_report
from cities_reconstruction.urban_planning import load_inputs as load_urban_planning_inputs

EARTH_RADIUS_M = 6_371_000.0
ROI_FILL_SEGMENTS = 256
SHAPEFILE_HEADER_BYTES = 100
SHAPEFILE_FILE_CODE = 9994
SHAPEFILE_VERSION = 1000
SHAPEFILE_NULL = 0
SHAPEFILE_POINT_TYPES = frozenset({1, 11, 21})
SHAPEFILE_MULTIPOINT_TYPES = frozenset({8, 18, 28})
SHAPEFILE_POLYGON_TYPES = frozenset({5, 15, 25})
TRANSIENT_OVERPASS_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
DBF_FIELD_TERMINATOR = 0x0D
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

FEATURE_LIKE_INVENTORY_KEYS = frozenset(
    {
        "aeroway",
        "amenity",
        "barrier",
        "building",
        "craft",
        "emergency",
        "geological",
        "healthcare",
        "highway",
        "historic",
        "landuse",
        "leisure",
        "man_made",
        "natural",
        "office",
        "place",
        "power",
        "public_transport",
        "railway",
        "shop",
        "sport",
        "surface",
        "tourism",
        "water",
        "waterway",
    }
)


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
        tag_inventory_raw_data, tag_inventory_source = _load_or_fetch_overpass(
            config,
            tag_inventory_query,
            overpass_json_path,
            cached_source_label="cached file used for tag inventory",
        )
    tag_inventory_raw_path.write_text(json.dumps(tag_inventory_raw_data, indent=2, sort_keys=True), encoding="utf-8")
    tag_inventory = _build_tag_inventory(tag_inventory_raw_data, source=tag_inventory_source, config=config)
    tag_inventory_path = output_dir / "tag_inventory.json"
    tag_inventory_path.write_text(json.dumps(tag_inventory, indent=2, sort_keys=True), encoding="utf-8")

    query = build_overpass_query(config)
    query_path = output_dir / "overpass_query.txt"
    query_path.write_text(query, encoding="utf-8")

    raw_data, source = _load_or_fetch_geometry_batches(
        config,
        output_dir,
        overpass_json_path,
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
    imagery_diagnostics = _fetch_imagery_diagnostics(config, output_dir)
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
    artifacts = (
        ArtifactReference("all-features", all_features_path, ArtifactKind.HANDOFF),
        ArtifactReference("urban-planning", urban_planning_path, ArtifactKind.HANDOFF),
        ArtifactReference("air-purifiers", air_purifiers_path, ArtifactKind.HANDOFF),
        *(ArtifactReference(f"category-{category.replace('_', '-')}", path, ArtifactKind.HANDOFF) for category, path in sorted(category_paths.items())),
        *(ArtifactReference(f"region-{region.replace('_', '-')}", path, ArtifactKind.HANDOFF) for region, path in sorted(region_paths.items())),
        ArtifactReference("tag-inventory-query", tag_inventory_query_path, ArtifactKind.DIAGNOSTIC),
        ArtifactReference("tag-inventory-raw", tag_inventory_raw_path, ArtifactKind.DIAGNOSTIC),
        ArtifactReference("tag-inventory", tag_inventory_path, ArtifactKind.SUPPORTING),
        ArtifactReference("overpass-query", query_path, ArtifactKind.DIAGNOSTIC),
        ArtifactReference("overpass-raw", raw_path, ArtifactKind.DIAGNOSTIC),
        ArtifactReference("geometry-diagnostics", diagnostics_path, ArtifactKind.DIAGNOSTIC),
        ArtifactReference("non-contributing-features", diagnostics_geojson_path, ArtifactKind.DIAGNOSTIC),
        ArtifactReference("imagery-diagnostics", imagery_diagnostics_path, ArtifactKind.DIAGNOSTIC),
        ArtifactReference("imagery-overlay", imagery_overlay_path, ArtifactKind.DIAGNOSTIC),
        *_imagery_evidence_artifacts(imagery_diagnostics),
        ArtifactReference("summary", summary_path, ArtifactKind.SUPPORTING),
        ArtifactReference("report", report_path, ArtifactKind.REPORT),
        ArtifactReference("preview", preview_path, ArtifactKind.PREVIEW),
    )
    manifest = publish_stage_manifest(
        stage=STAGE_ID.value,
        status=StageStatus.COMPLETED,
        output_directory=output_dir,
        report_path=report_path,
        preview_path=preview_path,
        input_state_fingerprint=_shapefiles_input_fingerprint(config, overpass_json_path),
        artifacts=artifacts,
        metrics={
            "raw_element_count": raw_element_count,
            "accepted_feature_count": len(reference_features),
            "skipped_feature_count": skipped_count,
        },
        details={
            "source": feature_source,
            "categories": list[JsonValue](sorted(category_paths)),
            "regions": list[JsonValue](sorted(region_paths)),
        },
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


def _imagery_evidence_artifacts(imagery_diagnostics: dict[str, Any]) -> tuple[ArtifactReference, ...]:
    """List only imagery evidence files that this run actually generated."""

    records = imagery_diagnostics.get("sources")
    if not isinstance(records, list):
        return ()
    artifacts: list[ArtifactReference] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        source_name = _slug(str(record.get("name", f"source-{index}")))
        candidates: list[tuple[str, str, ArtifactKind]] = [
            ("request", "request_url_path", ArtifactKind.DIAGNOSTIC),
        ]
        if record.get("status") == "fetched":
            candidates.append(("image", "image_path", ArtifactKind.SUPPORTING))
        elif record.get("status") == "error":
            candidates.append(("error", "error_path", ArtifactKind.DIAGNOSTIC))
        for role, field, kind in candidates:
            raw_path = record.get(field)
            if not isinstance(raw_path, str):
                continue
            path = Path(raw_path)
            if path.is_file():
                artifacts.append(
                    ArtifactReference(
                        f"imagery-{source_name}-{index}-{role}",
                        path,
                        kind,
                    )
                )
    return tuple(artifacts)


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


def overpass_to_features(raw_data: dict[str, Any], config: AppConfig) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    elements = raw_data.get("elements")
    if not isinstance(elements, list):
        raise ValueError("Overpass JSON must contain an 'elements' list")

    nodes = _node_lookup(elements)
    features: list[dict[str, Any]] = []
    skipped_by_reason: dict[str, int] = {}
    for element in elements:
        feature, skipped_reason = _element_to_feature(element, nodes, config)
        if feature is None:
            _increment(skipped_by_reason, skipped_reason or "unknown")
            continue
        features.append(feature)

    features.sort(
        key=lambda item: (
            item["properties"]["category"],
            item["properties"]["group_tag"],
            item["properties"]["osm_type"],
            item["properties"]["osm_id"],
        )
    )
    return features, sum(skipped_by_reason.values()), skipped_by_reason


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

    payload = path.read_bytes()
    _validate_tree_shapefile_header(payload, path, tree_input.name)
    attributes = _load_tree_shapefile_attributes(path.with_suffix(".dbf"))
    features: list[dict[str, Any]] = []
    record_index = 0
    for shape_index, (record_number, points, _is_null) in enumerate(_iter_shapefile_record_points(payload, path)):
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

    payload = path.read_bytes()
    _validate_surface_shapefile_header(payload, path, surface.name)
    attributes = _load_tree_shapefile_attributes(path.with_suffix(".dbf"))
    features: list[dict[str, Any]] = []
    for shape_index, (record_number, polygons) in enumerate(_iter_shapefile_record_polygons(payload, path)):
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


def _validate_surface_shapefile_header(payload: bytes, path: Path, input_name: str) -> None:
    if len(payload) < SHAPEFILE_HEADER_BYTES:
        raise ConfigError(f"supplemental input '{input_name}' shapefile is too small to contain a valid header: {path}")
    file_code = struct.unpack(">i", payload[0:4])[0]
    version = struct.unpack("<i", payload[28:32])[0]
    shape_type = struct.unpack("<i", payload[32:36])[0]
    if file_code != SHAPEFILE_FILE_CODE or version != SHAPEFILE_VERSION:
        raise ConfigError(f"invalid ESRI shapefile header for supplemental input '{input_name}': {path}")
    if shape_type not in SHAPEFILE_POLYGON_TYPES:
        raise ConfigError(
            f"supplemental input '{input_name}' shapefile must contain Polygon records; "
            f"{path} has shapefile type {shape_type}"
        )


def _iter_shapefile_record_polygons(payload: bytes, path: Path) -> list[tuple[int, list[Polygon]]]:
    records: list[tuple[int, list[Polygon]]] = []
    offset = SHAPEFILE_HEADER_BYTES
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise ConfigError(f"truncated shapefile record header in polygon input: {path}")
        record_number, content_words = struct.unpack(">2i", payload[offset:offset + 8])
        offset += 8
        content_bytes = content_words * 2
        content = payload[offset:offset + content_bytes]
        if len(content) != content_bytes:
            raise ConfigError(f"truncated shapefile record payload in polygon input: {path}")
        offset += content_bytes
        polygons = _shapefile_record_polygons(content, path, record_number)
        records.append((record_number, polygons))
    return records


def _shapefile_record_polygons(content: bytes, path: Path, record_number: int) -> list[Polygon]:
    if len(content) < 4:
        raise ConfigError(f"polygon shapefile record {record_number} has no shape type: {path}")
    shape_type = struct.unpack("<i", content[0:4])[0]
    if shape_type == SHAPEFILE_NULL:
        return []
    if shape_type not in SHAPEFILE_POLYGON_TYPES:
        raise ConfigError(
            f"polygon shapefile record {record_number} in {path} has unsupported shape type {shape_type}"
        )
    if len(content) < 44:
        raise ConfigError(f"polygon shapefile record {record_number} is truncated: {path}")
    part_count, point_count = struct.unpack("<2i", content[36:44])
    parts_end = 44 + part_count * 4
    points_end = parts_end + point_count * 16
    if part_count <= 0 or point_count < 4 or len(content) < points_end:
        raise ConfigError(f"polygon shapefile record {record_number} has invalid part data: {path}")
    part_starts = list(struct.unpack(f"<{part_count}i", content[44:parts_end]))
    if part_starts[0] != 0 or part_starts != sorted(part_starts) or part_starts[-1] >= point_count:
        raise ConfigError(f"polygon shapefile record {record_number} has invalid part offsets: {path}")
    points = [
        struct.unpack("<2d", content[index:index + 16])
        for index in range(parts_end, points_end, 16)
    ]
    rings = [
        points[start:end]
        for start, end in zip(part_starts, [*part_starts[1:], point_count], strict=True)
        if end - start >= 4
    ]
    return _polygons_from_shapefile_rings(rings)


def _polygons_from_shapefile_rings(rings: list[list[tuple[float, float]]]) -> list[Polygon]:
    ring_polygons = [make_valid(Polygon(ring)) for ring in rings]
    simple_rings = [polygon for geometry in ring_polygons for polygon in _extract_polygons(geometry) if polygon.area > 0.0]
    if not simple_rings:
        return []
    parents: list[int | None] = []
    for index, polygon in enumerate(simple_rings):
        containers = [
            (candidate.area, candidate_index)
            for candidate_index, candidate in enumerate(simple_rings)
            if candidate_index != index
            and candidate.area > polygon.area
            and candidate.covers(polygon.representative_point())
        ]
        parents.append(min(containers)[1] if containers else None)

    def depth(index: int) -> int:
        result = 0
        parent = parents[index]
        while parent is not None:
            result += 1
            parent = parents[parent]
        return result

    polygons: list[Polygon] = []
    for index, shell in enumerate(simple_rings):
        if depth(index) % 2:
            continue
        holes = [
            list(simple_rings[child].exterior.coords)
            for child, parent in enumerate(parents)
            if parent == index and depth(child) % 2 == 1
        ]
        polygons.extend(_extract_polygons(make_valid(Polygon(shell.exterior.coords, holes))))
    return polygons


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


def _validate_tree_shapefile_header(payload: bytes, path: Path, input_name: str) -> None:
    if len(payload) < SHAPEFILE_HEADER_BYTES:
        raise ConfigError(f"supplemental input '{input_name}' tree shapefile is too small to contain a valid header: {path}")
    file_code = struct.unpack(">i", payload[0:4])[0]
    version = struct.unpack("<i", payload[28:32])[0]
    shape_type = struct.unpack("<i", payload[32:36])[0]
    if file_code != SHAPEFILE_FILE_CODE or version != SHAPEFILE_VERSION:
        raise ConfigError(f"invalid ESRI shapefile header for supplemental tree input '{input_name}': {path}")
    if shape_type not in SHAPEFILE_POINT_TYPES | SHAPEFILE_MULTIPOINT_TYPES:
        raise ConfigError(
            f"supplemental tree input '{input_name}' shapefile must contain Point or MultiPoint records; "
            f"{path} has shapefile type {shape_type}"
        )


def _iter_shapefile_record_points(
    payload: bytes,
    path: Path,
) -> list[tuple[int, list[tuple[float, float]], bool]]:
    records: list[tuple[int, list[tuple[float, float]], bool]] = []
    offset = SHAPEFILE_HEADER_BYTES
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise ConfigError(f"truncated shapefile record header in tree input: {path}")
        record_number, content_words = struct.unpack(">2i", payload[offset:offset + 8])
        offset += 8
        content_bytes = content_words * 2
        content = payload[offset:offset + content_bytes]
        if len(content) != content_bytes:
            raise ConfigError(f"truncated shapefile record payload in tree input: {path}")
        offset += content_bytes
        is_null = len(content) >= 4 and struct.unpack("<i", content[0:4])[0] == SHAPEFILE_NULL
        points = _shapefile_record_points(content, path, record_number)
        records.append((record_number, points, is_null))
    return records


def _load_tree_shapefile_attributes(dbf_path: Path) -> list[dict[str, Any] | None]:
    if not dbf_path.exists():
        return []
    payload = dbf_path.read_bytes()
    if len(payload) < 33:
        raise ConfigError(f"tree shapefile DBF attribute table is too small: {dbf_path}")
    record_count = struct.unpack("<I", payload[4:8])[0]
    header_length = struct.unpack("<H", payload[8:10])[0]
    record_length = struct.unpack("<H", payload[10:12])[0]
    if header_length > len(payload) or record_length <= 1:
        raise ConfigError(f"invalid tree shapefile DBF header: {dbf_path}")

    fields = _dbf_fields(payload, dbf_path)
    records: list[dict[str, Any] | None] = []
    offset = header_length
    for _record_index in range(record_count):
        if offset + record_length > len(payload):
            raise ConfigError(f"truncated tree shapefile DBF records: {dbf_path}")
        raw_record = payload[offset:offset + record_length]
        offset += record_length
        if raw_record[:1] == b"*":
            records.append(None)
            continue
        records.append(_dbf_record_attributes(raw_record, fields))
    return records


def _dbf_fields(payload: bytes, dbf_path: Path) -> list[tuple[str, str, int, int, int]]:
    fields: list[tuple[str, str, int, int, int]] = []
    offset = 32
    field_offset = 1
    while offset + 32 <= len(payload):
        descriptor = payload[offset:offset + 32]
        if descriptor[0] == DBF_FIELD_TERMINATOR:
            return fields
        name = descriptor[0:11].split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()
        field_type = chr(descriptor[11])
        field_length = descriptor[16]
        decimal_count = descriptor[17]
        if not name or field_length <= 0:
            raise ConfigError(f"invalid DBF field descriptor in tree attributes: {dbf_path}")
        fields.append((name, field_type, field_offset, field_length, decimal_count))
        field_offset += field_length
        offset += 32
    raise ConfigError(f"tree shapefile DBF header is missing a field terminator: {dbf_path}")


def _dbf_record_attributes(raw_record: bytes, fields: list[tuple[str, str, int, int, int]]) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for name, field_type, offset, field_length, decimal_count in fields:
        raw_value = raw_record[offset:offset + field_length]
        value = _dbf_value(raw_value, field_type, decimal_count)
        if value is not None:
            attributes[name] = value
    return attributes


def _dbf_value(raw_value: bytes, field_type: str, decimal_count: int) -> Any:
    text = raw_value.decode("latin-1", errors="replace").strip()
    if not text:
        return None
    normalized_type = field_type.upper()
    if normalized_type in {"C", "D", "M"}:
        return text
    if normalized_type in {"N", "F", "B", "Y"}:
        try:
            if decimal_count == 0 and "." not in text and "," not in text:
                return int(text)
            return float(text.replace(",", "."))
        except ValueError:
            return text
    if normalized_type == "L":
        if text.upper() in {"Y", "T"}:
            return True
        if text.upper() in {"N", "F"}:
            return False
    return text


def _shapefile_record_points(content: bytes, path: Path, record_number: int) -> list[tuple[float, float]]:
    if len(content) < 4:
        raise ConfigError(f"tree shapefile record {record_number} has no shape type: {path}")
    shape_type = struct.unpack("<i", content[0:4])[0]
    if shape_type == SHAPEFILE_NULL:
        return []
    if shape_type in SHAPEFILE_POINT_TYPES:
        if len(content) < 20:
            raise ConfigError(f"tree shapefile point record {record_number} is truncated: {path}")
        return [struct.unpack("<2d", content[4:20])]
    if shape_type in SHAPEFILE_MULTIPOINT_TYPES:
        if len(content) < 40:
            raise ConfigError(f"tree shapefile multipoint record {record_number} is truncated: {path}")
        point_count = struct.unpack("<i", content[36:40])[0]
        points_end = 40 + point_count * 16
        if point_count < 0 or len(content) < points_end:
            raise ConfigError(f"tree shapefile multipoint record {record_number} has invalid point data: {path}")
        return [
            struct.unpack("<2d", content[index:index + 16])
            for index in range(40, points_end, 16)
        ]
    raise ConfigError(
        "tree shapefile supports only Point and MultiPoint records; "
        f"record {record_number} in {path} has shape type {shape_type}"
    )


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


def _load_or_fetch_geometry_batches(
    config: AppConfig,
    output_dir: Path,
    overpass_json_path: Path | None,
) -> tuple[dict[str, Any], str]:
    if overpass_json_path is not None:
        data, source = _load_or_fetch_overpass(
            config,
            build_overpass_query(config),
            overpass_json_path,
        )
        return data, source

    payloads: list[dict[str, Any]] = []
    for index, query in enumerate(build_overpass_query_batches(config), start=1):
        query_path = output_dir / f"overpass_query_batch_{index:02d}.txt"
        cache_path = output_dir / f"overpass_raw_batch_{index:02d}.json"
        query_path.write_text(query, encoding="utf-8")
        if cache_path.exists():
            with cache_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        else:
            payload, _source = _load_or_fetch_overpass(config, query, None)
            cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        payloads.append(payload)
    return _merge_overpass_payloads(payloads), (
        f"{config.inputs.overpass_url} ({len(payloads)} batched geometry requests)"
    )


def _merge_overpass_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    if not payloads:
        return {"elements": []}
    merged = dict(payloads[0])
    elements_by_id: dict[tuple[str, int], dict[str, Any]] = {}
    for payload in payloads:
        elements = payload.get("elements")
        if not isinstance(elements, list):
            raise ValueError("Overpass JSON must contain an 'elements' list")
        for element in elements:
            if not isinstance(element, dict):
                continue
            element_type = element.get("type")
            element_id = element.get("id")
            if isinstance(element_type, str) and isinstance(element_id, int):
                elements_by_id[(element_type, element_id)] = element
    merged["elements"] = list(elements_by_id.values())
    return merged


def _load_or_fetch_overpass(
    config: AppConfig,
    query: str,
    overpass_json_path: Path | None,
    cached_source_label: str = "cached file",
) -> tuple[dict[str, Any], str]:
    if overpass_json_path is not None:
        with overpass_json_path.open("r", encoding="utf-8") as handle:
            return json.load(handle), f"{cached_source_label}: {overpass_json_path}"

    payload = parse.urlencode({"data": query}).encode("utf-8")
    http_request = request.Request(
        config.inputs.overpass_url,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "cities-reconstruction/0.1.0",
        },
        method="POST",
    )
    failure: BaseException
    for attempt in range(1, config.inputs.overpass_max_attempts + 1):
        try:
            with request.urlopen(http_request, timeout=config.inputs.overpass_timeout_s) as response:
                return json.loads(response.read().decode("utf-8")), config.inputs.overpass_url
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            suffix = f": {detail[:500]}" if detail else ""
            message = (
                f"failed to fetch Overpass data from {config.inputs.overpass_url}: "
                f"HTTP {exc.code}{suffix}"
            )
            retryable = exc.code in TRANSIENT_OVERPASS_HTTP_STATUS
            failure = exc
        except TimeoutError as exc:
            message = (
                f"Overpass request timed out after {config.inputs.overpass_timeout_s:g} seconds: "
                f"{config.inputs.overpass_url}"
            )
            retryable = True
            failure = exc
        except error.URLError as exc:
            message = f"failed to fetch Overpass data from {config.inputs.overpass_url}: {exc}"
            retryable = True
            failure = exc

        if not retryable or attempt == config.inputs.overpass_max_attempts:
            raise RuntimeError(message) from failure
        delay_s = config.inputs.overpass_retry_backoff_s * (2 ** (attempt - 1))
        if delay_s > 0:
            time.sleep(delay_s)

    raise AssertionError("unreachable Overpass retry state")


def _build_tag_inventory(raw_data: dict[str, Any], source: str, config: AppConfig) -> dict[str, Any]:
    elements = raw_data.get("elements")
    if not isinstance(elements, list):
        raise ValueError("Overpass JSON must contain an 'elements' list")

    tag_key_counts: dict[str, int] = {}
    tag_value_counts: dict[str, int] = {}
    element_type_counts: dict[str, int] = {}
    classified_source_tag_counts: dict[str, int] = {}
    unclassified_tag_value_counts: dict[str, int] = {}
    unclassified_feature_like_tag_value_counts: dict[str, int] = {}
    tagged_element_count = 0

    for element in elements:
        if not isinstance(element, dict):
            continue
        element_type = str(element.get("type", "unknown"))
        element_type_counts[element_type] = element_type_counts.get(element_type, 0) + 1
        tags = element.get("tags")
        if not isinstance(tags, dict) or not tags:
            continue
        tagged_element_count += 1
        classification = _classify_tags(tags, config)
        if classification is None:
            for tag_value in _tag_values(tags):
                _increment(unclassified_tag_value_counts, tag_value)
                if _is_feature_like_tag_value(tag_value):
                    _increment(unclassified_feature_like_tag_value_counts, tag_value)
        else:
            _increment(classified_source_tag_counts, classification[2])
        for key, value in tags.items():
            _increment(tag_key_counts, str(key))
            _increment(tag_value_counts, f"{key}={value}")

    return {
        "source": source,
        "raw_elements": len(elements),
        "tagged_elements": tagged_element_count,
        "element_type_counts": dict(sorted(element_type_counts.items())),
        "tag_key_counts": dict(sorted(tag_key_counts.items())),
        "tag_value_counts": dict(sorted(tag_value_counts.items())),
        "classified_source_tag_counts": dict(sorted(classified_source_tag_counts.items())),
        "unclassified_tag_value_counts": dict(sorted(unclassified_tag_value_counts.items())),
        "unclassified_feature_like_tag_value_counts": dict(sorted(unclassified_feature_like_tag_value_counts.items())),
    }


def _node_lookup(elements: list[Any]) -> dict[int, tuple[float, float]]:
    nodes: dict[int, tuple[float, float]] = {}
    for element in elements:
        if not isinstance(element, dict) or element.get("type") != "node":
            continue
        if isinstance(element.get("id"), int) and _has_lat_lon(element):
            nodes[element["id"]] = (float(element["lon"]), float(element["lat"]))
    return nodes


def _element_to_feature(
    element: Any,
    nodes: dict[int, tuple[float, float]],
    config: AppConfig,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(element, dict):
        return None, "invalid_element"
    tags = element.get("tags")
    if not isinstance(tags, dict):
        return None, "missing_tags"
    classification = _classify_tags(tags, config)
    if classification is None:
        return None, "unsupported_tags"
    category, group_tag, source_tag = classification

    geometry = _geometry_from_element(element, nodes, category)
    if geometry is None:
        return None, "unsupported_or_incomplete_geometry"

    centroid = _centroid(geometry)
    centroid_distance_m = _distance_m(
        config.region.center_lat,
        config.region.center_lon,
        centroid[1],
        centroid[0],
    )
    roi_distance_m = _geometry_distance_to_region_center_m(geometry, config)
    roi_zone = _roi_zone(roi_distance_m, config)
    if roi_zone is None:
        return None, "outside_roi_policy"
    reconstruction_scope = _reconstruction_scope(roi_zone)
    building_properties = (
        {
            "building_base_height_m": _building_base_height_m(
                tags,
                config.city_models.building_roof_default_base_height_m,
            )
        }
        if category == "buildings"
        else {}
    )

    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "osm_type": element.get("type"),
            "osm_id": element.get("id"),
            "category": category,
            "group_tag": group_tag,
            "source_tag": source_tag,
            "contributes_to_geometry": _contributes_to_geometry(geometry),
            "geometry_role": _geometry_role(geometry),
            "roi_zone": roi_zone,
            "reconstruction_scope": reconstruction_scope,
            "include_in_building_lod22_reconstruction": _include_in_building_lod22_reconstruction(category, roi_zone),
            "centroid_distance_m": round(centroid_distance_m, 3),
            "roi_distance_m": round(roi_distance_m, 3),
            "tags": tags,
            **building_properties,
        },
    }, None


def _building_base_height_m(tags: dict[str, Any], roof_default_m: float) -> float:
    if tags.get("building") != "roof":
        return 0.0

    raw_height = tags.get("min_height")
    if raw_height is None:
        return roof_default_m
    try:
        height = float(raw_height)
    except (TypeError, ValueError):
        return roof_default_m
    return height if math.isfinite(height) and height >= 0 else roof_default_m


def _classify_tags(tags: dict[str, Any], config: AppConfig) -> tuple[str, str, str] | None:
    for rule in config.shapefiles.classification_rules:
        for expression in rule.match_any:
            key, separator, expected_value = expression.partition("=")
            if key not in tags:
                continue
            if separator and str(tags[key]) != expected_value:
                continue
            return rule.category, rule.group_tag, _source_tag(tags, key)
    return None


def _source_tag(tags: dict[str, Any], key: str) -> str:
    return f"{key}={tags[key]}"


def _tag_values(tags: dict[str, Any]) -> list[str]:
    return [f"{key}={value}" for key, value in tags.items()]


def _is_feature_like_tag_value(tag_value: str) -> bool:
    key = tag_value.split("=", 1)[0]
    return key in FEATURE_LIKE_INVENTORY_KEYS


def _geometry_from_element(
    element: dict[str, Any],
    nodes: dict[int, tuple[float, float]],
    category: str,
) -> dict[str, Any] | None:
    element_type = element.get("type")
    if element_type == "node" and _has_lat_lon(element):
        return {"type": "Point", "coordinates": [float(element["lon"]), float(element["lat"])]}
    if element_type == "way":
        coordinates = _way_coordinates(element, nodes)
        if len(coordinates) < 2:
            return None
        is_closed = coordinates[0] == coordinates[-1] and len(coordinates) >= 4
        if is_closed and category != "roads":
            return {"type": "Polygon", "coordinates": [coordinates]}
        return {"type": "LineString", "coordinates": coordinates}
    if element_type == "relation":
        return _relation_geometry(element, category)
    return None


def _relation_geometry(element: dict[str, Any], category: str) -> dict[str, Any] | None:
    members = element.get("members")
    if not isinstance(members, list):
        return None
    outer_segments: list[list[list[float]]] = []
    inner_segments: list[list[list[float]]] = []
    for member in members:
        if not isinstance(member, dict):
            continue
        segment = _member_geometry(member)
        if len(segment) < 2:
            continue
        if member.get("role") == "outer":
            outer_segments.append(segment)
        elif member.get("role") == "inner":
            inner_segments.append(segment)
    if not outer_segments:
        return None

    if category == "roads" and len(outer_segments) == 1 and not inner_segments:
        return {"type": "LineString", "coordinates": outer_segments[0]}

    outer_rings = _assemble_rings(outer_segments)
    if not outer_rings:
        return None
    polygons = _assign_inner_rings_to_outer_rings(outer_rings, _assemble_rings(inner_segments))
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def _member_geometry(member: dict[str, Any]) -> list[list[float]]:
    geometry = member.get("geometry")
    if not isinstance(geometry, list):
        return []
    coordinates = []
    for point in geometry:
        if isinstance(point, dict) and _has_lat_lon(point):
            coordinates.append([float(point["lon"]), float(point["lat"])])
    return coordinates


def _assemble_rings(segments: list[list[list[float]]]) -> list[list[list[float]]]:
    unused = [list(segment) for segment in segments]
    rings: list[list[list[float]]] = []
    while unused:
        ring = unused.pop(0)
        changed = True
        while changed and ring[0] != ring[-1]:
            changed = False
            for index, segment in enumerate(unused):
                if ring[-1] == segment[0]:
                    ring.extend(segment[1:])
                elif ring[-1] == segment[-1]:
                    ring.extend(reversed(segment[:-1]))
                elif ring[0] == segment[-1]:
                    ring = segment[:-1] + ring
                elif ring[0] == segment[0]:
                    ring = list(reversed(segment[1:])) + ring
                else:
                    continue
                unused.pop(index)
                changed = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def _assign_inner_rings_to_outer_rings(
    outer_rings: list[list[list[float]]],
    inner_rings: list[list[list[float]]],
) -> list[list[list[list[float]]]]:
    polygons = [[outer_ring] for outer_ring in outer_rings]
    for inner_ring in inner_rings:
        container_index = _outer_ring_index_for_inner_ring(outer_rings, inner_ring)
        if container_index is not None:
            polygons[container_index].append(inner_ring)
    return polygons


def _outer_ring_index_for_inner_ring(
    outer_rings: list[list[list[float]]],
    inner_ring: list[list[float]],
) -> int | None:
    if not inner_ring:
        return None
    sample = (inner_ring[0][0], inner_ring[0][1])
    for index, outer_ring in enumerate(outer_rings):
        outer_points = [(point[0], point[1]) for point in outer_ring]
        if _point_in_ring(sample, outer_points):
            return index
    return None


def _way_coordinates(element: dict[str, Any], nodes: dict[int, tuple[float, float]]) -> list[list[float]]:
    geometry = element.get("geometry")
    if isinstance(geometry, list):
        coordinates = []
        for point in geometry:
            if isinstance(point, dict) and _has_lat_lon(point):
                coordinates.append([float(point["lon"]), float(point["lat"])])
        return coordinates

    node_ids = element.get("nodes")
    if isinstance(node_ids, list):
        coordinates = []
        for node_id in node_ids:
            if isinstance(node_id, int) and node_id in nodes:
                lon, lat = nodes[node_id]
                coordinates.append([lon, lat])
        return coordinates
    return []


def _has_lat_lon(value: dict[str, Any]) -> bool:
    return isinstance(value.get("lat"), int | float) and isinstance(value.get("lon"), int | float)


def _contributes_to_geometry(geometry: dict[str, Any]) -> bool:
    return geometry["type"] in {"Polygon", "MultiPolygon"}


def _geometry_role(geometry: dict[str, Any]) -> str:
    if _contributes_to_geometry(geometry):
        return "contributing_polygon"
    return "reference_only_non_contributing"


def _roi_zone(roi_distance_m: float, config: AppConfig) -> str | None:
    outer_radius = config.region.outer_diameter_m / 2.0
    if config.region.inner_diameter_m is None:
        return "full" if roi_distance_m <= outer_radius else None
    inner_radius = config.region.inner_diameter_m / 2.0
    if roi_distance_m <= inner_radius:
        return "inner"
    if roi_distance_m <= outer_radius:
        return "annular"
    return None


def _reconstruction_scope(roi_zone: str) -> str:
    if roi_zone in {"inner", "full"}:
        return "primary_roi"
    return "annular_context"


def _include_in_building_lod22_reconstruction(category: str, roi_zone: str) -> bool:
    return category == "buildings" and roi_zone in {"inner", "full"}


def _centroid(geometry: dict[str, Any]) -> tuple[float, float]:
    coordinates = geometry["coordinates"]
    if geometry["type"] == "Point":
        return float(coordinates[0]), float(coordinates[1])
    if geometry["type"] == "LineString":
        return _average_coordinate(coordinates)
    if geometry["type"] == "Polygon":
        return _average_coordinate(coordinates[0])
    if geometry["type"] == "MultiPolygon":
        points = [point for polygon in coordinates for ring in polygon for point in ring]
        return _average_coordinate(points)
    raise ValueError(f"unsupported geometry type: {geometry['type']}")


def _average_coordinate(coordinates: list[list[float]]) -> tuple[float, float]:
    lon_sum = sum(point[0] for point in coordinates)
    lat_sum = sum(point[1] for point in coordinates)
    count = len(coordinates)
    return lon_sum / count, lat_sum / count


def _distance_m(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    phi_a = math.radians(lat_a)
    phi_b = math.radians(lat_b)
    delta_phi = math.radians(lat_b - lat_a)
    delta_lambda = math.radians(lon_b - lon_a)
    haversine = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.atan2(math.sqrt(haversine), math.sqrt(1.0 - haversine))


def _geometry_distance_to_region_center_m(geometry: dict[str, Any], config: AppConfig) -> float:
    if geometry["type"] == "Point":
        return _point_norm_m(_project_coordinate_m(geometry["coordinates"], config))
    if geometry["type"] == "LineString":
        return _line_distance_to_center_m(geometry["coordinates"], config)
    if geometry["type"] == "Polygon":
        return _polygon_distance_to_center_m(geometry["coordinates"], config)
    if geometry["type"] == "MultiPolygon":
        return min(
            _polygon_distance_to_center_m(polygon, config)
            for polygon in geometry["coordinates"]
        )
    raise ValueError(f"unsupported geometry type: {geometry['type']}")


def _polygon_distance_to_center_m(polygon: list[list[list[float]]], config: AppConfig) -> float:
    outer_ring = polygon[0] if polygon else []
    projected_outer = [_project_coordinate_m(point, config) for point in outer_ring]
    if projected_outer and _point_in_ring((0.0, 0.0), projected_outer):
        return 0.0
    distances = [_line_distance_to_center_m(ring, config) for ring in polygon if ring]
    return min(distances) if distances else math.inf


def _line_distance_to_center_m(coordinates: list[list[float]], config: AppConfig) -> float:
    projected = [_project_coordinate_m(point, config) for point in coordinates]
    if not projected:
        return math.inf
    point_distances = [_point_norm_m(point) for point in projected]
    if len(projected) == 1:
        return point_distances[0]
    segment_distances = [
        _segment_distance_to_origin_m(start, end)
        for start, end in zip(projected, projected[1:], strict=False)
    ]
    return min([*point_distances, *segment_distances])


def _project_coordinate_m(coordinate: list[float], config: AppConfig) -> tuple[float, float]:
    lon, lat = coordinate
    x_m = math.radians(lon - config.region.center_lon) * EARTH_RADIUS_M * math.cos(math.radians(config.region.center_lat))
    y_m = math.radians(lat - config.region.center_lat) * EARTH_RADIUS_M
    return x_m, y_m


def _point_norm_m(point: tuple[float, float]) -> float:
    return math.hypot(point[0], point[1])


def _segment_distance_to_origin_m(start: tuple[float, float], end: tuple[float, float]) -> float:
    start_x, start_y = start
    end_x, end_y = end
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared == 0.0:
        return _point_norm_m(start)
    t = -((start_x * delta_x) + (start_y * delta_y)) / length_squared
    t = max(0.0, min(1.0, t))
    closest = (start_x + t * delta_x, start_y + t * delta_y)
    return _point_norm_m(closest)


def _point_in_ring(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    if len(ring) < 3:
        return inside
    previous_x, previous_y = ring[-1]
    for current_x, current_y in ring:
        intersects = (current_y > y) != (previous_y > y)
        if intersects:
            x_at_y = (previous_x - current_x) * (y - current_y) / (previous_y - current_y) + current_x
            if x < x_at_y:
                inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


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


def _fetch_imagery_diagnostics(config: AppConfig, output_dir: Path) -> dict[str, Any]:
    bbox = _roi_bbox_lon_lat(config)
    diagnostics: dict[str, Any] = {
        "bbox_lon_lat": {
            "min_lon": bbox[0],
            "min_lat": bbox[1],
            "max_lon": bbox[2],
            "max_lat": bbox[3],
        },
        "assumptions": [
            "Imagery is diagnostic evidence only; it is not used to generate or fill geometry in this stage.",
            "The WMS request uses EPSG:4326 with a WMS 1.1.1 lon/lat bounding box around the outer ROI.",
            "Overlay geometry is drawn from integrated Overpass, supplemental shapefile, and urban-planning GeoJSON features; polygon gaps remain visible over the imagery.",
        ],
        "sources": [],
    }
    if not config.imagery.sources:
        return diagnostics

    imagery_dir = output_dir / "imagery"
    imagery_dir.mkdir(parents=True, exist_ok=True)
    for source in config.imagery.sources:
        source_record = _imagery_source_record(source)
        if not source.enabled:
            source_record["status"] = "disabled"
            diagnostics["sources"].append(source_record)
            continue

        url = _build_wms_getmap_url(source, bbox)
        slug = _slug(source.name)
        request_path = imagery_dir / f"{slug}_request.url"
        request_path.write_text(url, encoding="utf-8")
        image_path = imagery_dir / f"{slug}.{_image_extension(source.format)}"
        source_record.update(
            {
                "status": "requested",
                "request_url_path": str(request_path),
                "image_path": str(image_path),
            }
        )
        http_request = request.Request(
            url,
            headers={
                "Accept": source.format,
                "User-Agent": "cities-reconstruction/0.1.0",
            },
            method="GET",
        )
        try:
            with request.urlopen(http_request, timeout=max(10.0, config.inputs.overpass_timeout_s)) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            source_record.update({"status": "error", "error": f"HTTP {exc.code}: {detail[:500]}"})
        except error.URLError as exc:
            source_record.update({"status": "error", "error": str(exc)})
        else:
            if _looks_like_wms_error(payload, content_type):
                error_path = imagery_dir / f"{slug}_error.txt"
                error_path.write_text(payload.decode("utf-8", errors="replace")[:2000], encoding="utf-8")
                source_record.update(
                    {
                        "status": "error",
                        "error": "WMS returned a text/XML response instead of an image",
                        "error_path": str(error_path),
                        "content_type": content_type,
                    }
                )
            else:
                image_path.write_bytes(payload)
                source_record.update(
                    {
                        "status": "fetched",
                        "content_type": content_type,
                        "bytes": len(payload),
                        "width": source.width,
                        "height": source.height,
                    }
                )
        diagnostics["sources"].append(source_record)

    return diagnostics


def _imagery_source_record(source: ImagerySourceConfig) -> dict[str, Any]:
    return {
        "name": source.name,
        "type": source.type,
        "url": source.url,
        "layer": source.layer,
        "enabled": source.enabled,
        "crs": source.crs,
        "format": source.format,
        "width": source.width,
        "height": source.height,
    }


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


def _build_wms_getmap_url(source: ImagerySourceConfig, bbox: tuple[float, float, float, float]) -> str:
    parts = parse.urlsplit(source.url)
    query = parse.parse_qsl(parts.query, keep_blank_values=True)
    query.extend(
        [
            ("SERVICE", "WMS"),
            ("VERSION", "1.1.1"),
            ("REQUEST", "GetMap"),
            ("LAYERS", source.layer),
            ("STYLES", source.style),
            ("FORMAT", source.format),
            ("TRANSPARENT", "TRUE" if source.transparent else "FALSE"),
            ("SRS", source.crs),
            ("BBOX", ",".join(f"{value:.8f}" for value in bbox)),
            ("WIDTH", str(source.width)),
            ("HEIGHT", str(source.height)),
        ]
    )
    return parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parse.urlencode(query), parts.fragment))


def _image_extension(image_format: str) -> str:
    normalized = image_format.lower()
    if "png" in normalized:
        return "png"
    if "jpeg" in normalized or "jpg" in normalized:
        return "jpg"
    return "img"


def _looks_like_wms_error(payload: bytes, content_type: str) -> bool:
    prefix = payload.lstrip()[:80].lower()
    return (
        "text" in content_type.lower()
        or "xml" in content_type.lower()
        or prefix.startswith(b"<?xml")
        or prefix.startswith(b"<html")
        or b"serviceexception" in prefix
    )


def _slug(value: str) -> str:
    normalized = []
    for character in value.lower():
        if character.isalnum():
            normalized.append(character)
        elif normalized and normalized[-1] != "_":
            normalized.append("_")
    return "".join(normalized).strip("_") or "imagery"


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1
