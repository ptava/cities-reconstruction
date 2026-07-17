"""Segmentation-assisted visual enrichment stage.

The executable path ingests segmentation polygons from an external backend and
writes reviewable candidate layers. It does not run a neural segmentation model
itself yet, and it does not overwrite stage-1 reconstruction inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
import math
from pathlib import Path
from typing import Any

from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.validation import make_valid

from cities_reconstruction.config import AppConfig
from cities_reconstruction.stage_result import StageResult


EARTH_RADIUS_M = 6_371_000.0
DEFAULT_SEGMENTATION_INPUT_NAME = "segmentation_input.geojson"
DEFAULT_SAT2LOD2_POLYGONS_NAME = "sat2lod2_building_polygons.geojson"

BUILDING_CLASSES = frozenset({"building", "buildings", "building_footprint", "roof", "rooftop"})
ROAD_CLASSES = frozenset({"road", "roads", "street", "highway", "asphalt", "carriageway"})
PAVED_CONCRETE_CLASSES = frozenset(
    {"paved", "pavement", "paving", "paving_stones", "concrete", "parking", "sidewalk", "square", "impervious"}
)
GREEN_CLASSES = frozenset({"vegetation", "green", "grass", "tree_canopy", "park", "garden"})
WATER_CLASSES = frozenset({"water", "river", "canal", "pond"})
OTHER_TERRAIN_CLASSES = frozenset({"bare_soil", "soil", "ground", "terrain", "other_terrain", "unknown_terrain"})

CANDIDATE_STYLES = {
    "buildings": {"label": "Candidate building footprints", "color": "#ef4444"},
    "terrain": {"label": "Candidate terrain tags", "color": "#22c55e"},
    "roads_paved_concrete": {"label": "Candidate roads / paved / concrete", "color": "#0ea5e9"},
}
BASE_STYLES = {
    "buildings": "#92400e",
    "roads": "#374151",
    "green_areas": "#166534",
    "concrete": "#64748b",
    "water": "#0369a1",
    "trees": "#15803d",
    "other_terrain": "#a16207",
    "gap_fill": "#f59e0b",
}


@dataclass(frozen=True)
class VisualEnrichmentStageOutput:
    output_directory: Path
    candidate_building_footprints_path: Path
    candidate_terrain_surfaces_path: Path
    candidate_roads_paved_concrete_path: Path
    visual_enrichment_delta_path: Path
    enriched_all_features_path: Path
    segmentation_diagnostics_path: Path
    segmentation_input_template_path: Path
    sat2lod2_handoff_manifest_path: Path
    segmentation_overlay_path: Path
    report_path: Path
    source_feature_count: int
    segmentation_feature_count: int
    sat2lod2_feature_count: int
    candidate_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "output_directory": str(self.output_directory),
            "candidate_building_footprints_path": str(self.candidate_building_footprints_path),
            "candidate_terrain_surfaces_path": str(self.candidate_terrain_surfaces_path),
            "candidate_roads_paved_concrete_path": str(self.candidate_roads_paved_concrete_path),
            "visual_enrichment_delta_path": str(self.visual_enrichment_delta_path),
            "enriched_all_features_path": str(self.enriched_all_features_path),
            "segmentation_diagnostics_path": str(self.segmentation_diagnostics_path),
            "segmentation_input_template_path": str(self.segmentation_input_template_path),
            "sat2lod2_handoff_manifest_path": str(self.sat2lod2_handoff_manifest_path),
            "segmentation_overlay_path": str(self.segmentation_overlay_path),
            "report_path": str(self.report_path),
            "source_feature_count": self.source_feature_count,
            "segmentation_feature_count": self.segmentation_feature_count,
            "sat2lod2_feature_count": self.sat2lod2_feature_count,
            "candidate_count": self.candidate_count,
        }


def plan(config: AppConfig) -> StageResult:
    output = config.output.root_directory / "02_visual_enrichment"
    return StageResult(
        stage="visual-enrichment",
        summary="Run review-gated segmentation-assisted refinement of OSM features from aerial/orthophoto imagery.",
        planned_actions=(
            "Read stage-1 all_features.geojson plus imagery_diagnostics.json from 01_shapefiles.",
            "Ingest external segmentation polygons from 02_visual_enrichment/segmentation_input.geojson or a CLI-provided GeoJSON path.",
            "Prepare a SAT2LoD2/LOD2BuildingModel external handoff manifest, and import user-provided SAT2LoD2 2D building polygons when available.",
            "Use building segmentation polygons to propose refined or missing building footprints for future LOD 2.2 reconstruction.",
            "Use terrain segmentation polygons to propose improved tags for weakly classified terrain and gap-fill surfaces.",
            "Use segmentation polygons to propose roads, asphalt, paved surfaces, and concrete surfaces.",
            "Keep review gating explicit: do not promote segmented geometry into reconstruction inputs automatically.",
            "Write candidate layers, an enriched review copy, diagnostics, and graphical feedback; do not promote segmented geometry into reconstruction inputs automatically.",
        ),
        expected_outputs=(output,),
    )


def run(
    config: AppConfig,
    segmentation_geojson_path: Path | None = None,
    sat2lod2_geojson_path: Path | None = None,
) -> VisualEnrichmentStageOutput:
    """Execute visual enrichment using reviewable external segmentation polygons."""

    stage1_dir = config.output.root_directory / "01_shapefiles"
    source_features_path = stage1_dir / "all_features.geojson"
    imagery_diagnostics_path = stage1_dir / "imagery_diagnostics.json"
    if not source_features_path.exists():
        raise FileNotFoundError(
            f"visual-enrichment requires stage-1 features at {source_features_path}; run the shapefiles stage first"
        )

    output_dir = config.output.root_directory / "02_visual_enrichment"
    output_dir.mkdir(parents=True, exist_ok=True)
    default_segmentation_path = output_dir / DEFAULT_SEGMENTATION_INPUT_NAME
    segmentation_path = segmentation_geojson_path or (default_segmentation_path if default_segmentation_path.exists() else None)
    default_sat2lod2_path = output_dir / DEFAULT_SAT2LOD2_POLYGONS_NAME
    sat2lod2_path = sat2lod2_geojson_path or (default_sat2lod2_path if default_sat2lod2_path.exists() else None)

    source_features = _load_feature_collection(source_features_path)
    imagery_diagnostics = _load_optional_json(imagery_diagnostics_path, default=_empty_imagery_diagnostics(config))
    imagery_sources = _available_imagery_sources(imagery_diagnostics)
    segmentation_features = _load_feature_collection(segmentation_path) if segmentation_path is not None else []
    sat2lod2_features = _sat2lod2_features(sat2lod2_path) if sat2lod2_path is not None else []

    candidate_groups = _build_candidates(source_features, segmentation_features, imagery_sources, config)
    sat2lod2_candidate_groups = _build_candidates(
        source_features,
        sat2lod2_features,
        imagery_sources,
        config,
        source_label="sat2lod2",
        backend_label="GDAOSU/LOD2BuildingModel SAT2LoD2 external adapter",
    )
    candidate_groups["buildings"].extend(sat2lod2_candidate_groups["buildings"])
    candidates = [
        *candidate_groups["buildings"],
        *candidate_groups["terrain"],
        *candidate_groups["roads_paved_concrete"],
    ]
    enriched_features = [*_mark_source_features(source_features), *candidates]

    candidate_buildings_path = output_dir / "candidate_building_footprints.geojson"
    candidate_terrain_path = output_dir / "candidate_terrain_surfaces.geojson"
    candidate_roads_path = output_dir / "candidate_roads_paved_concrete.geojson"
    delta_path = output_dir / "visual_enrichment_delta.geojson"
    enriched_all_features_path = output_dir / "enriched_all_features.geojson"
    diagnostics_path = output_dir / "segmentation_diagnostics.json"
    template_path = output_dir / "segmentation_input_template.geojson"
    sat2lod2_manifest_path = output_dir / "sat2lod2_handoff_manifest.json"
    overlay_path = output_dir / "segmentation_overlay.html"
    report_path = output_dir / "visual_enrichment_report.md"

    _write_geojson(candidate_buildings_path, candidate_groups["buildings"])
    _write_geojson(candidate_terrain_path, candidate_groups["terrain"])
    _write_geojson(candidate_roads_path, candidate_groups["roads_paved_concrete"])
    _write_geojson(delta_path, candidates)
    _write_geojson(enriched_all_features_path, enriched_features)
    template_path.write_text(json.dumps(_segmentation_input_template(), indent=2, sort_keys=True), encoding="utf-8")
    sat2lod2_manifest_path.write_text(
        json.dumps(
            _sat2lod2_handoff_manifest(
                config=config,
                imagery_diagnostics=imagery_diagnostics,
                source_features_path=source_features_path,
                sat2lod2_path=sat2lod2_path,
            ),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    diagnostics = _build_diagnostics(
        config=config,
        source_features=source_features,
        segmentation_features=segmentation_features,
        sat2lod2_features=sat2lod2_features,
        candidate_groups=candidate_groups,
        segmentation_path=segmentation_path,
        sat2lod2_path=sat2lod2_path,
        imagery_sources=imagery_sources,
    )
    diagnostics_path.write_text(json.dumps(diagnostics, indent=2, sort_keys=True), encoding="utf-8")
    overlay_path.write_text(
        _render_overlay_html(config, source_features, candidate_groups, imagery_diagnostics),
        encoding="utf-8",
    )
    report_path.write_text(
        _render_report(
            config=config,
            diagnostics=diagnostics,
            candidate_buildings_path=candidate_buildings_path,
            candidate_terrain_path=candidate_terrain_path,
            candidate_roads_path=candidate_roads_path,
            delta_path=delta_path,
            enriched_all_features_path=enriched_all_features_path,
            diagnostics_path=diagnostics_path,
            template_path=template_path,
            sat2lod2_manifest_path=sat2lod2_manifest_path,
            overlay_path=overlay_path,
        ),
        encoding="utf-8",
    )

    return VisualEnrichmentStageOutput(
        output_directory=output_dir,
        candidate_building_footprints_path=candidate_buildings_path,
        candidate_terrain_surfaces_path=candidate_terrain_path,
        candidate_roads_paved_concrete_path=candidate_roads_path,
        visual_enrichment_delta_path=delta_path,
        enriched_all_features_path=enriched_all_features_path,
        segmentation_diagnostics_path=diagnostics_path,
        segmentation_input_template_path=template_path,
        sat2lod2_handoff_manifest_path=sat2lod2_manifest_path,
        segmentation_overlay_path=overlay_path,
        report_path=report_path,
        source_feature_count=len(source_features),
        segmentation_feature_count=len(segmentation_features),
        sat2lod2_feature_count=len(sat2lod2_features),
        candidate_count=len(candidates),
    )


def _build_candidates(
    source_features: list[dict[str, Any]],
    segmentation_features: list[dict[str, Any]],
    imagery_sources: list[dict[str, Any]],
    config: AppConfig,
    source_label: str = "segmentation",
    backend_label: str = "external_geojson_segmentation",
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "buildings": [],
        "terrain": [],
        "roads_paved_concrete": [],
    }
    for feature in segmentation_features:
        geometry = _candidate_geometry(feature)
        if geometry is None:
            continue
        semantic_class = _semantic_class(feature.get("properties", {}))
        target_group = _target_group(semantic_class)
        if target_group is None:
            continue
        candidate = _candidate_from_segmentation(
            feature=feature,
            geometry=geometry,
            semantic_class=semantic_class,
            target_group=target_group,
            index=len(groups[target_group]) + 1,
            source_features=source_features,
            imagery_sources=imagery_sources,
            config=config,
            source_label=source_label,
            backend_label=backend_label,
        )
        groups[target_group].append(candidate)
    return groups


def _candidate_from_segmentation(
    feature: dict[str, Any],
    geometry: dict[str, Any],
    semantic_class: str,
    target_group: str,
    index: int,
    source_features: list[dict[str, Any]],
    imagery_sources: list[dict[str, Any]],
    config: AppConfig,
    source_label: str,
    backend_label: str,
) -> dict[str, Any]:
    properties = feature.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    polygon = _feature_shape({"type": "Feature", "geometry": geometry, "properties": properties})
    matched = _best_overlap_match(polygon, source_features, _match_categories(target_group))
    suggested_category = _suggested_category(semantic_class, target_group)
    target_tag = _suggested_tag(semantic_class, suggested_category)
    complexity = _complexity_delta(polygon, matched["feature"]) if matched else None
    roi_zone = _candidate_roi_zone(polygon, config)

    candidate_properties: dict[str, Any] = {
        "candidate_id": f"{target_group}_{index}",
        "category": suggested_category,
        "candidate_group": target_group,
        "source": source_label,
        "source_tag": target_tag,
        "segmentation_backend": str(properties.get("backend") or properties.get("model") or backend_label),
        "segmentation_class": semantic_class,
        "source_image": _source_image(properties, imagery_sources),
        "confidence": _confidence(properties),
        "review_status": "needs_review",
        "contributes_to_geometry": False,
        "geometry_role": "segmentation_candidate_review_only",
        "roi_zone": roi_zone,
        "reconstruction_scope": "primary_roi" if roi_zone in {"inner", "full"} else "annular_context",
        "suggested_target_category": suggested_category,
        "suggested_source_tag": target_tag,
        "proposed_action": _proposed_action(target_group, matched, suggested_category),
        "matched_source_feature_id": _matched_feature_id(matched["feature"]) if matched else None,
        "matched_source_category": matched["feature"]["properties"].get("category") if matched else None,
        "matched_overlap_ratio": round(matched["overlap_ratio"], 4) if matched else 0.0,
        "area_m2": round(_area_m2(polygon, config), 3),
        "tags": {"visual_enrichment": f"{source_label}_candidate", "suggested": target_tag},
    }
    if target_group == "buildings":
        candidate_properties.update(
            {
                "lod_target": "LOD2.2",
                "include_in_building_lod22_reconstruction": False,
                "footprint_vertex_count": _vertex_count(polygon),
                "matched_footprint_vertex_count": complexity["matched_vertex_count"] if complexity else None,
                "footprint_vertex_count_delta": complexity["vertex_count_delta"] if complexity else None,
            }
        )
        if source_label == "sat2lod2":
            candidate_properties["proposed_action"] = (
                "import_sat2lod2_refined_footprint_for_lod22" if matched else "import_sat2lod2_missing_building_candidate"
            )
    else:
        candidate_properties["include_in_building_lod22_reconstruction"] = False

    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": candidate_properties,
    }


def _candidate_geometry(feature: dict[str, Any]) -> dict[str, Any] | None:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        return None
    polygon = _feature_shape(feature)
    if polygon.is_empty or polygon.area <= 0.0:
        return None
    return mapping(make_valid(polygon))


def _feature_shape(feature: dict[str, Any]) -> Polygon | MultiPolygon:
    return make_valid(shape(feature["geometry"]))


def _target_group(semantic_class: str) -> str | None:
    if semantic_class in BUILDING_CLASSES:
        return "buildings"
    if semantic_class in ROAD_CLASSES or semantic_class in PAVED_CONCRETE_CLASSES:
        return "roads_paved_concrete"
    if semantic_class in GREEN_CLASSES or semantic_class in WATER_CLASSES or semantic_class in OTHER_TERRAIN_CLASSES:
        return "terrain"
    return None


def _match_categories(target_group: str) -> set[str]:
    if target_group == "buildings":
        return {"buildings"}
    if target_group == "roads_paved_concrete":
        return {"roads", "concrete", "gap_fill", "other_terrain"}
    return {"green_areas", "water", "concrete", "other_terrain", "gap_fill"}


def _suggested_category(semantic_class: str, target_group: str) -> str:
    if target_group == "buildings":
        return "buildings"
    if semantic_class in ROAD_CLASSES:
        return "roads"
    if semantic_class in PAVED_CONCRETE_CLASSES:
        return "concrete"
    if semantic_class in GREEN_CLASSES:
        return "green_areas"
    if semantic_class in WATER_CLASSES:
        return "water"
    return "other_terrain"


def _suggested_tag(semantic_class: str, suggested_category: str) -> str:
    if suggested_category == "buildings":
        return "building=segmentation_candidate"
    if semantic_class == "asphalt":
        return "surface=asphalt"
    if suggested_category == "roads":
        return "highway=segmentation_candidate"
    if semantic_class == "concrete":
        return "surface=concrete"
    if suggested_category == "concrete":
        return "surface=paved"
    if suggested_category == "green_areas":
        return "landcover=vegetation"
    if suggested_category == "water":
        return "natural=water"
    return f"landcover={semantic_class}"


def _proposed_action(target_group: str, matched: dict[str, Any] | None, suggested_category: str) -> str:
    if target_group == "buildings":
        return "refine_existing_footprint_for_lod22" if matched else "add_missing_building_candidate"
    if matched and matched["feature"]["properties"].get("category") == "gap_fill":
        return f"replace_gap_fill_with_{suggested_category}"
    if matched and matched["feature"]["properties"].get("category") == "other_terrain":
        return f"retag_weak_terrain_as_{suggested_category}"
    return f"add_or_refine_{suggested_category}_surface"


def _best_overlap_match(
    candidate: Polygon | MultiPolygon,
    source_features: list[dict[str, Any]],
    categories: set[str],
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    candidate_area = max(candidate.area, 1e-18)
    for source_feature in source_features:
        properties = source_feature.get("properties", {})
        if not isinstance(properties, dict) or properties.get("category") not in categories:
            continue
        geometry = source_feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        source_polygon = _feature_shape(source_feature)
        if source_polygon.is_empty:
            continue
        overlap_area = candidate.intersection(source_polygon).area
        if overlap_area <= 0.0:
            continue
        overlap_ratio = overlap_area / candidate_area
        if best is None or overlap_ratio > best["overlap_ratio"]:
            best = {"feature": source_feature, "overlap_ratio": overlap_ratio}
    return best


def _mark_source_features(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    marked = []
    for feature in features:
        copied = json.loads(json.dumps(feature))
        copied.setdefault("properties", {})["visual_enrichment_role"] = "stage_1_source_feature"
        marked.append(copied)
    return marked


def _load_feature_collection(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        raise ValueError(f"GeoJSON file must be a FeatureCollection: {path}")
    return [feature for feature in data["features"] if isinstance(feature, dict)]


def _sat2lod2_features(path: Path) -> list[dict[str, Any]]:
    features = []
    for feature in _load_feature_collection(path):
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        properties = feature.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        mapped_properties = dict(properties)
        mapped_properties.setdefault("segmentation_class", "building_footprint")
        mapped_properties.setdefault("backend", "GDAOSU/LOD2BuildingModel SAT2LoD2 external adapter")
        mapped_properties.setdefault("source_image", "SAT2LoD2 orthophoto input")
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": mapped_properties,
            }
        )
    return features


def _load_optional_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return default
    return data


def _sat2lod2_handoff_manifest(
    config: AppConfig,
    imagery_diagnostics: dict[str, Any],
    source_features_path: Path,
    sat2lod2_path: Path | None,
) -> dict[str, Any]:
    return {
        "adapter": "GDAOSU/LOD2BuildingModel SAT2LoD2 external adapter",
        "status": "sat2lod2_output_available" if sat2lod2_path else "waiting_for_external_sat2lod2_output",
        "license_policy": (
            "Do not vendor or redistribute SAT2LoD2/LOD2BuildingModel code or weights in this repository. "
            "Use a user-installed copy externally, then import neutral GeoJSON polygon outputs."
        ),
        "source_stage1_features": str(source_features_path),
        "expected_import_path": str(sat2lod2_path or (config.output.root_directory / "02_visual_enrichment" / DEFAULT_SAT2LOD2_POLYGONS_NAME)),
        "expected_import_format": (
            "GeoJSON FeatureCollection of SAT2LoD2-derived 2D building footprint polygons in the same lon/lat "
            "frame as 01_shapefiles/all_features.geojson. Optional properties: confidence, score, source_image."
        ),
        "orthophoto_sources": [
            {
                "name": source.get("name"),
                "image_path": source.get("image_path"),
                "crs": source.get("crs"),
                "width": source.get("width"),
                "height": source.get("height"),
                "status": source.get("status"),
            }
            for source in _available_imagery_sources(imagery_diagnostics)
        ],
        "bbox_lon_lat": imagery_diagnostics.get("bbox_lon_lat"),
        "region": {
            "name": config.region.name,
            "crs": config.region.crs,
            "center_lat": config.region.center_lat,
            "center_lon": config.region.center_lon,
            "inner_diameter_m": config.region.inner_diameter_m,
            "outer_diameter_m": config.region.outer_diameter_m,
        },
        "notes": [
            "SAT2LoD2 normally works from orthophoto/DSM evidence; this manifest records this project's available image and ROI context.",
            "Imported polygons are review candidates only and are not promoted into stage-1 authoritative features.",
            "If SAT2LoD2 outputs are in a projected CRS, transform them to lon/lat before import or add an explicit CRS conversion adapter.",
        ],
    }


def _empty_imagery_diagnostics(config: AppConfig) -> dict[str, Any]:
    return {
        "bbox_lon_lat": _roi_bbox_lon_lat(config),
        "sources": [],
        "assumptions": ["No stage-1 imagery diagnostics were found."],
    }


def _available_imagery_sources(imagery_diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    sources = imagery_diagnostics.get("sources", [])
    if not isinstance(sources, list):
        return []
    return [
        source
        for source in sources
        if isinstance(source, dict) and source.get("status") == "fetched" and isinstance(source.get("image_path"), str)
    ]


def _source_image(properties: dict[str, Any], imagery_sources: list[dict[str, Any]]) -> str:
    for key in ("source_image", "image_path", "imagery_source"):
        value = properties.get(key)
        if isinstance(value, str) and value:
            return value
    if imagery_sources:
        return str(imagery_sources[0]["image_path"])
    return "unknown"


def _semantic_class(properties: dict[str, Any]) -> str:
    for key in ("segmentation_class", "semantic_class", "class", "label", "category"):
        value = properties.get(key)
        if isinstance(value, str) and value:
            return value.lower().replace(" ", "_").replace("-", "_")
    return "unknown"


def _confidence(properties: dict[str, Any]) -> float | None:
    value = properties.get("confidence", properties.get("score"))
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(max(0.0, min(1.0, float(value))), 4)


def _candidate_roi_zone(candidate: Polygon | MultiPolygon, config: AppConfig) -> str:
    if config.region.inner_diameter_m is None:
        return "full"
    centroid = candidate.centroid
    lon, lat = centroid.x, centroid.y
    distance = _distance_m(config.region.center_lat, config.region.center_lon, lat, lon)
    if distance <= config.region.inner_diameter_m / 2.0:
        return "inner"
    return "annular"


def _matched_feature_id(feature: dict[str, Any]) -> str:
    properties = feature.get("properties", {})
    osm_type = properties.get("osm_type", "feature")
    osm_id = properties.get("osm_id", properties.get("candidate_id", "unknown"))
    return f"{osm_type}:{osm_id}"


def _complexity_delta(candidate: Polygon | MultiPolygon, matched_feature: dict[str, Any]) -> dict[str, int]:
    candidate_vertices = _vertex_count(candidate)
    matched_vertices = _vertex_count(_feature_shape(matched_feature))
    return {
        "candidate_vertex_count": candidate_vertices,
        "matched_vertex_count": matched_vertices,
        "vertex_count_delta": candidate_vertices - matched_vertices,
    }


def _vertex_count(geometry: Polygon | MultiPolygon) -> int:
    polygons = [geometry] if isinstance(geometry, Polygon) else list(geometry.geoms)
    return sum(len(polygon.exterior.coords) for polygon in polygons)


def _area_m2(geometry: Polygon | MultiPolygon, config: AppConfig) -> float:
    polygons = [geometry] if isinstance(geometry, Polygon) else list(geometry.geoms)
    return sum(_project_polygon(polygon, config).area for polygon in polygons)


def _project_polygon(polygon: Polygon, config: AppConfig) -> Polygon:
    shell = [_project_coordinate_m([x, y], config) for x, y in polygon.exterior.coords]
    holes = [
        [_project_coordinate_m([x, y], config) for x, y in interior.coords]
        for interior in polygon.interiors
    ]
    return Polygon(shell, holes)


def _project_coordinate_m(coordinate: list[float], config: AppConfig) -> tuple[float, float]:
    lon, lat = coordinate
    x_m = math.radians(lon - config.region.center_lon) * EARTH_RADIUS_M * math.cos(math.radians(config.region.center_lat))
    y_m = math.radians(lat - config.region.center_lat) * EARTH_RADIUS_M
    return x_m, y_m


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


def _roi_bbox_lon_lat(config: AppConfig) -> dict[str, float]:
    radius_m = config.region.outer_diameter_m / 2.0
    lat_delta = math.degrees(radius_m / EARTH_RADIUS_M)
    lon_delta = math.degrees(radius_m / (EARTH_RADIUS_M * math.cos(math.radians(config.region.center_lat))))
    return {
        "min_lon": config.region.center_lon - lon_delta,
        "min_lat": config.region.center_lat - lat_delta,
        "max_lon": config.region.center_lon + lon_delta,
        "max_lat": config.region.center_lat + lat_delta,
    }


def _write_geojson(path: Path, features: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _build_diagnostics(
    config: AppConfig,
    source_features: list[dict[str, Any]],
    segmentation_features: list[dict[str, Any]],
    sat2lod2_features: list[dict[str, Any]],
    candidate_groups: dict[str, list[dict[str, Any]]],
    segmentation_path: Path | None,
    sat2lod2_path: Path | None,
    imagery_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_counts = {group: len(features) for group, features in candidate_groups.items()}
    confidence_values = [
        feature["properties"]["confidence"]
        for features in candidate_groups.values()
        for feature in features
        if feature["properties"]["confidence"] is not None
    ]
    return {
        "region": {
            "name": config.region.name,
            "crs": config.region.crs,
            "inner_diameter_m": config.region.inner_diameter_m,
            "outer_diameter_m": config.region.outer_diameter_m,
        },
        "status": "processed_segmentation_input" if segmentation_path else "waiting_for_segmentation_input",
        "segmentation_input_path": str(segmentation_path) if segmentation_path else None,
        "segmentation_input_format": "GeoJSON FeatureCollection with Polygon or MultiPolygon masks",
        "sat2lod2_input_path": str(sat2lod2_path) if sat2lod2_path else None,
        "sat2lod2_input_format": "GeoJSON FeatureCollection of external SAT2LoD2 2D building polygons",
        "source_feature_count": len(source_features),
        "segmentation_feature_count": len(segmentation_features),
        "sat2lod2_feature_count": len(sat2lod2_features),
        "candidate_counts": candidate_counts,
        "candidate_count": sum(candidate_counts.values()),
        "imagery_sources_used_for_provenance": imagery_sources,
        "confidence_statistics": _confidence_statistics(confidence_values),
        "review_policy": (
            "All segmentation-derived features are written as candidates with review_status=needs_review, "
            "contributes_to_geometry=false, and include_in_building_lod22_reconstruction=false. "
            "They are not promoted into authoritative reconstruction inputs until a later acceptance workflow."
        ),
        "assumptions": [
            "Segmentation is supplied by an external backend through GeoJSON masks; this package currently normalizes and compares those masks.",
            "SAT2LoD2/LOD2BuildingModel is treated as an optional user-installed external adapter because its license is not suitable for vendoring here.",
            "Overlap matching uses small-ROI lon/lat geometry for feature association and local meter projection for diagnostic area estimates.",
            "The enriched_all_features.geojson file is a review artifact in 02_visual_enrichment, not a replacement for 01_shapefiles/all_features.geojson.",
        ],
    }


def _confidence_statistics(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "mean": round(sum(values) / len(values), 4),
    }


def _segmentation_input_template() -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [],
        "properties_schema": {
            "required_geometry": "Polygon or MultiPolygon in the same lon/lat coordinate frame as stage-1 GeoJSON",
            "class_keys": ["segmentation_class", "semantic_class", "class", "label", "category"],
            "supported_classes": {
                "buildings": sorted(BUILDING_CLASSES),
                "roads_paved_concrete": sorted(ROAD_CLASSES | PAVED_CONCRETE_CLASSES),
                "terrain": sorted(GREEN_CLASSES | WATER_CLASSES | OTHER_TERRAIN_CLASSES),
            },
            "optional_properties": ["confidence", "backend", "model", "source_image"],
            "sat2lod2_import": {
                "default_path": DEFAULT_SAT2LOD2_POLYGONS_NAME,
                "description": "Optional SAT2LoD2/LOD2BuildingModel 2D building polygon output imported as review-gated building footprint candidates.",
                "required_geometry": "Polygon or MultiPolygon in stage-1 lon/lat coordinates",
            },
        },
    }


def _render_report(
    config: AppConfig,
    diagnostics: dict[str, Any],
    candidate_buildings_path: Path,
    candidate_terrain_path: Path,
    candidate_roads_path: Path,
    delta_path: Path,
    enriched_all_features_path: Path,
    diagnostics_path: Path,
    template_path: Path,
    sat2lod2_manifest_path: Path,
    overlay_path: Path,
) -> str:
    counts = diagnostics["candidate_counts"]
    outputs = "\n".join(
        [
            f"- Candidate building footprints: `{candidate_buildings_path}`",
            f"- Candidate terrain surfaces: `{candidate_terrain_path}`",
            f"- Candidate roads / paved / concrete surfaces: `{candidate_roads_path}`",
            f"- Combined visual-enrichment delta: `{delta_path}`",
            f"- Enriched review copy of all features: `{enriched_all_features_path}`",
            f"- Segmentation diagnostics: `{diagnostics_path}`",
            f"- Segmentation input template: `{template_path}`",
            f"- SAT2LoD2 external handoff manifest: `{sat2lod2_manifest_path}`",
            f"- Segmentation overlay preview: `{overlay_path}`",
        ]
    )
    assumptions = "\n".join(f"- {item}" for item in diagnostics["assumptions"])
    inner_diameter_line = (
        f"- Inner diameter: {config.region.inner_diameter_m:g} m"
        if config.region.inner_diameter_m is not None
        else "- Inner diameter: not set (uniform treatment across the outer ROI)"
    )
    return f"""# Visual Enrichment Report

## Region

- Name: {config.region.name}
- CRS: {config.region.crs}
{inner_diameter_line}
- Outer diameter: {config.region.outer_diameter_m:g} m

## Result

- Status: {diagnostics["status"]}
- Source stage-1 features: {diagnostics["source_feature_count"]}
- Segmentation features read: {diagnostics["segmentation_feature_count"]}
- SAT2LoD2 building polygons read: {diagnostics["sat2lod2_feature_count"]}
- Candidate building footprints: {counts["buildings"]}
- Candidate terrain surfaces: {counts["terrain"]}
- Candidate roads / paved / concrete surfaces: {counts["roads_paved_concrete"]}
- Total candidates: {diagnostics["candidate_count"]}

## Review Policy

{diagnostics["review_policy"]}

## Outputs

{outputs}

## Assumptions

{assumptions}
"""


def _render_overlay_html(
    config: AppConfig,
    source_features: list[dict[str, Any]],
    candidate_groups: dict[str, list[dict[str, Any]]],
    imagery_diagnostics: dict[str, Any],
) -> str:
    bbox = _bbox_tuple(imagery_diagnostics.get("bbox_lon_lat") or _roi_bbox_lon_lat(config))
    successful_sources = _available_imagery_sources(imagery_diagnostics)
    candidate_rows = "\n".join(
        _candidate_row(group, len(features))
        for group, features in candidate_groups.items()
    )
    if successful_sources:
        sections = "\n".join(_imagery_section(source, source_features, candidate_groups, bbox) for source in successful_sources)
    else:
        sections = _local_svg_section(config, source_features, candidate_groups)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{escape(config.region.name)} visual enrichment overlay</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; color: #1f2933; }}
    main {{ display: grid; grid-template-columns: minmax(0, 920px) 300px; gap: 1.5rem; align-items: start; }}
    figure {{ margin: 0 0 1.5rem 0; }}
    figcaption {{ margin-top: 0.45rem; color: #52606d; font-size: 0.9rem; }}
    .overlay {{ position: relative; width: 100%; border: 1px solid #c8d1dc; background: #f8fafc; }}
    .overlay img {{ display: block; width: 100%; height: auto; }}
    .overlay svg {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
    svg.local {{ width: 100%; height: auto; border: 1px solid #c8d1dc; background: #f8fafc; }}
    .zoom-frame {{ overflow: hidden; }}
    .zoom-frame svg.local {{ border: 0; display: block; }}
    .zoom-content {{ position: relative; transform-origin: center center; transition: transform 120ms ease-out; }}
    .zoom-controls {{ display: flex; gap: 0.45rem; flex-wrap: wrap; margin: 0 0 0.7rem 0; }}
    .zoom-controls button {{ border: 1px solid #94a3b8; background: #ffffff; color: #1f2933; padding: 0.35rem 0.65rem; border-radius: 0.35rem; cursor: pointer; }}
    .zoom-controls button:hover {{ background: #e2e8f0; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #d9e2ec; padding: 0.4rem; text-align: left; }}
    th {{ font-weight: 600; }}
    .swatch {{ display: inline-block; width: 0.85rem; height: 0.85rem; margin-right: 0.45rem; border: 1px solid #475569; vertical-align: -0.1rem; }}
    .note {{ color: #52606d; font-size: 0.9rem; line-height: 1.35; }}
  </style>
</head>
<body>
  <h1>{escape(config.region.name)} segmentation overlay</h1>
  <main>
    <section>{sections}</section>
    <section>
      <h2>Candidate counts</h2>
      <table>{candidate_rows}</table>
      <p class="note">Muted fills are stage-1 features. Bright outlines are segmentation-derived candidates requiring review.</p>
      <p class="note">Candidates are written to 02_visual_enrichment and do not overwrite the stage-1 GeoJSON files.</p>
      <p class="note">Use the mouse wheel or zoom buttons to zoom every feedback plot.</p>
    </section>
  </main>
  {_zoomable_preview_script()}
</body>
</html>
"""


def _imagery_section(
    source: dict[str, Any],
    source_features: list[dict[str, Any]],
    candidate_groups: dict[str, list[dict[str, Any]]],
    bbox: tuple[float, float, float, float],
) -> str:
    width = int(source.get("width", 1200))
    height = int(source.get("height", 1200))
    image_path = Path(str(source["image_path"]))
    image_src = f"../01_shapefiles/imagery/{image_path.name}"
    source_svg = "\n".join(_feature_to_bbox_svg(feature, bbox, width, height, candidate=False) for feature in source_features)
    candidate_svg = "\n".join(
        _feature_to_bbox_svg(feature, bbox, width, height, candidate=True, group=group)
        for group, features in candidate_groups.items()
        for feature in features
    )
    return f"""
      <figure>
        {_zoom_controls_html()}
        <div class="overlay zoom-frame" data-zoomable>
          <div class="zoom-content">
            <img src="{escape(image_src)}" alt="{escape(str(source.get('name', 'imagery')))}">
            <svg viewBox="0 0 {width} {height}" role="img" aria-label="Segmentation candidates over imagery">
              {source_svg}
              {candidate_svg}
            </svg>
          </div>
        </div>
        <figcaption>{escape(str(source.get('name', 'imagery')))}</figcaption>
      </figure>
"""


def _local_svg_section(
    config: AppConfig,
    source_features: list[dict[str, Any]],
    candidate_groups: dict[str, list[dict[str, Any]]],
) -> str:
    width = 900
    height = 700
    margin = 70
    scale = min((width - 2 * margin) / config.region.outer_diameter_m, (height - 2 * margin) / config.region.outer_diameter_m)
    center_x = width / 2.0
    center_y = height / 2.0
    outer_radius_px = config.region.outer_diameter_m * scale / 2.0
    inner_boundary_svg = ""
    if config.region.inner_diameter_m is not None:
        inner_radius_px = config.region.inner_diameter_m * scale / 2.0
        inner_boundary_svg = f'<circle cx="{center_x:.3f}" cy="{center_y:.3f}" r="{inner_radius_px:.3f}" fill="none" stroke="#102a43" stroke-width="2"/>'
    source_svg = "\n".join(_feature_to_local_svg(feature, config, center_x, center_y, scale, candidate=False) for feature in source_features)
    candidate_svg = "\n".join(
        _feature_to_local_svg(feature, config, center_x, center_y, scale, candidate=True, group=group)
        for group, features in candidate_groups.items()
        for feature in features
    )
    return f"""
      <figure>
        {_zoom_controls_html()}
        <div class="zoom-frame" data-zoomable>
          <div class="zoom-content">
            <svg class="local" viewBox="0 0 {width} {height}" role="img" aria-label="Segmentation candidates over stage-1 features">
              <circle cx="{center_x:.3f}" cy="{center_y:.3f}" r="{outer_radius_px:.3f}" fill="none" stroke="#334e68" stroke-width="2" stroke-dasharray="8 6"/>
              {inner_boundary_svg}
              {source_svg}
              {candidate_svg}
            </svg>
          </div>
        </div>
        <figcaption>No fetched imagery was available, so this overlay uses the local stage preview plane.</figcaption>
      </figure>
"""


def _zoom_controls_html() -> str:
    return """
        <div class="zoom-controls" aria-label="Feedback plot zoom controls">
          <button type="button" data-zoom-in>Zoom in</button>
          <button type="button" data-zoom-out>Zoom out</button>
          <button type="button" data-zoom-reset>Reset zoom</button>
        </div>
"""


def _zoomable_preview_script() -> str:
    return """
  <script>
    for (const frame of document.querySelectorAll("[data-zoomable]")) {
      const content = frame.querySelector(".zoom-content");
      const controls = frame.previousElementSibling && frame.previousElementSibling.classList.contains("zoom-controls")
        ? frame.previousElementSibling
        : null;
      let zoom = 1.0;
      function setZoom(nextZoom) {
        zoom = Math.max(0.35, Math.min(6.0, nextZoom));
        content.style.transform = `scale(${zoom})`;
      }
      frame.addEventListener("wheel", (event) => {
        event.preventDefault();
        setZoom(zoom * (event.deltaY < 0 ? 1.12 : 0.88));
      }, { passive: false });
      if (controls) {
        controls.querySelector("[data-zoom-in]").addEventListener("click", () => setZoom(zoom * 1.2));
        controls.querySelector("[data-zoom-out]").addEventListener("click", () => setZoom(zoom / 1.2));
        controls.querySelector("[data-zoom-reset]").addEventListener("click", () => setZoom(1.0));
      }
      setZoom(1.0);
    }
  </script>
"""


def _feature_to_bbox_svg(
    feature: dict[str, Any],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    candidate: bool,
    group: str | None = None,
) -> str:
    geometry = feature["geometry"]
    if geometry["type"] not in {"Polygon", "MultiPolygon"}:
        return ""
    color, opacity, stroke_width, dash = _style(feature, candidate, group)
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    return "\n".join(
        f'<path d="{_bbox_svg_path(polygon, bbox, width, height)}" fill="{color}" stroke="{color}" stroke-width="{stroke_width}" opacity="{opacity}" fill-rule="evenodd"{dash}/>'
        for polygon in polygons
    )


def _feature_to_local_svg(
    feature: dict[str, Any],
    config: AppConfig,
    center_x: float,
    center_y: float,
    scale: float,
    candidate: bool,
    group: str | None = None,
) -> str:
    geometry = feature["geometry"]
    if geometry["type"] not in {"Polygon", "MultiPolygon"}:
        return ""
    color, opacity, stroke_width, dash = _style(feature, candidate, group)
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    return "\n".join(
        f'<path d="{_local_svg_path(polygon, config, center_x, center_y, scale)}" fill="{color}" stroke="{color}" stroke-width="{stroke_width}" opacity="{opacity}" fill-rule="evenodd"{dash}/>'
        for polygon in polygons
    )


def _style(feature: dict[str, Any], candidate: bool, group: str | None) -> tuple[str, str, str, str]:
    if candidate:
        color = CANDIDATE_STYLES[group or "terrain"]["color"]
        return color, "0.88", "3.0", ' stroke-dasharray="8 5"'
    category = feature.get("properties", {}).get("category", "other_terrain")
    return BASE_STYLES.get(category, "#94a3b8"), "0.24", "1.0", ""


def _bbox_svg_path(
    rings: list[list[list[float]]],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> str:
    return " ".join(_bbox_ring_path(ring, bbox, width, height) for ring in rings if ring)


def _bbox_ring_path(
    coordinates: list[list[float]],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> str:
    points = [
        f"{x:.3f} {y:.3f}"
        for x, y in (_project_to_bbox(point, bbox, width, height) for point in coordinates)
    ]
    return f"M {' L '.join(points)} Z" if points else ""


def _local_svg_path(
    rings: list[list[list[float]]],
    config: AppConfig,
    center_x: float,
    center_y: float,
    scale: float,
) -> str:
    return " ".join(_local_ring_path(ring, config, center_x, center_y, scale) for ring in rings if ring)


def _local_ring_path(
    coordinates: list[list[float]],
    config: AppConfig,
    center_x: float,
    center_y: float,
    scale: float,
) -> str:
    points = [
        f"{x:.3f} {y:.3f}"
        for x, y in (_project_to_local_svg(point, config, center_x, center_y, scale) for point in coordinates)
    ]
    return f"M {' L '.join(points)} Z" if points else ""


def _project_to_bbox(
    coordinate: list[float] | tuple[float, float],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[float, float]:
    min_lon, min_lat, max_lon, max_lat = bbox
    lon, lat = coordinate
    x = (lon - min_lon) / (max_lon - min_lon) * width
    y = (max_lat - lat) / (max_lat - min_lat) * height
    return x, y


def _project_to_local_svg(
    coordinate: list[float],
    config: AppConfig,
    center_x: float,
    center_y: float,
    scale: float,
) -> tuple[float, float]:
    x_m, y_m = _project_coordinate_m(coordinate, config)
    return center_x + x_m * scale, center_y - y_m * scale


def _bbox_tuple(value: Any) -> tuple[float, float, float, float]:
    if isinstance(value, dict):
        return (
            float(value["min_lon"]),
            float(value["min_lat"]),
            float(value["max_lon"]),
            float(value["max_lat"]),
        )
    if isinstance(value, (list, tuple)) and len(value) == 4:
        min_lon, min_lat, max_lon, max_lat = value
        return float(min_lon), float(min_lat), float(max_lon), float(max_lat)
    raise ValueError("imagery bbox must be a dict or 4-value sequence")


def _candidate_row(group: str, count: int) -> str:
    style = CANDIDATE_STYLES[group]
    swatch = f'<span class="swatch" style="background:{style["color"]};"></span>'
    return f"<tr><th>{swatch}{escape(style['label'])}</th><td>{count}</td></tr>"
