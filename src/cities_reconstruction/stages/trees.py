"""Parametric tree model generation for the fourth geometry module."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
import math
from pathlib import Path
from typing import Any, Callable

from shapely.geometry import MultiPoint, Point, shape

from cities_reconstruction.config import AppConfig, ConfigError
from cities_reconstruction.geometry.terrain import (
    load_terrain_sampler,
    validate_completed_city_models_terrain,
)
from cities_reconstruction.stage_result import StageResult


@dataclass(frozen=True)
class TreeSpeciesModel:
    name: str
    aliases: tuple[str, ...]
    default_height_m: float
    default_crown_radius_m: float
    default_trunk_radius_m: float
    crown_base_fraction: float
    crown_shape: str


@dataclass(frozen=True)
class TreeInstance:
    tree_id: str
    species: str
    source_species: str | None
    model_category: str
    crown_shape: str
    x: float
    y: float
    z: float
    height_m: float
    crown_radius_m: float
    trunk_radius_m: float
    trunk_height_m: float
    roi_zone: str
    osm_id: object | None
    model_source: str
    height_source: str
    crown_radius_source: str
    trunk_radius_source: str
    used_tags: tuple[str, ...]
    defaulted_fields: tuple[str, ...]


@dataclass(frozen=True)
class TreesStageOutput:
    output_directory: Path
    placement_geojson_path: Path
    library_path: Path
    manifest_path: Path
    surfaces_directory: Path
    trunks_stl_path: Path
    crowns_stl_path: Path
    combined_stl_path: Path
    preview_path: Path
    report_path: Path
    tree_count: int
    species_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "output_directory": str(self.output_directory),
            "placement_geojson_path": str(self.placement_geojson_path),
            "library_path": str(self.library_path),
            "manifest_path": str(self.manifest_path),
            "surfaces_directory": str(self.surfaces_directory),
            "trunks_stl_path": str(self.trunks_stl_path),
            "crowns_stl_path": str(self.crowns_stl_path),
            "combined_stl_path": str(self.combined_stl_path),
            "preview_path": str(self.preview_path),
            "report_path": str(self.report_path),
            "tree_count": self.tree_count,
            "species_counts": dict(self.species_counts),
        }


TREE_TERRAIN_CLEARANCE_M = 0.05

CROWN_SEGMENTS = 16
CROWN_RINGS = 8
TRUNK_SEGMENTS = 14
Point3 = tuple[float, float, float]
Triangle = tuple[str, Point3, Point3, Point3]


def plan(config: AppConfig) -> StageResult:
    output = config.output.root_directory / "04_trees"
    return StageResult(
        stage="trees",
        summary="Generate parametric tree STL models from retrieved tree features.",
        planned_actions=(
            f"Use {config.trees.default} as the configured fallback species for tree features without species tags.",
            "Read retrieved OSM tree features from module 1.",
            "Project tree placements to the configured EPSG:25832 metric CRS.",
            "Optionally project tree bases onto a supplied terrain geometry file from stage 3 so trunk bases sit just below the local terrain surface.",
            "Resolve species through the configured species/category mapping and scale category models with available tree height/diameter tags.",
            "Write trunk, crown, and combined STL surfaces plus an interactive HTML QA preview.",
        ),
        expected_outputs=(output,),
    )


def run(config: AppConfig) -> TreesStageOutput:
    """Generate deterministic parametric tree meshes from stage-1 tree features."""

    if config.region.crs != "EPSG:25832":
        raise ConfigError("tree model generation currently supports EPSG:25832 output coordinates")

    tree_features_path = config.output.root_directory / "01_shapefiles" / "trees.geojson"
    if not tree_features_path.exists():
        raise ConfigError("missing tree GeoJSON. Run `shapefiles` before `trees`.")

    features = _read_feature_collection(tree_features_path)
    output_dir = config.output.root_directory / "04_trees"
    surfaces_dir = output_dir / "surfaces"
    output_dir.mkdir(parents=True, exist_ok=True)
    surfaces_dir.mkdir(parents=True, exist_ok=True)

    placement_path = output_dir / "tree_placements.geojson"
    library_path = output_dir / "tree_species_library.json"
    manifest_path = output_dir / "tree_models_manifest.json"
    report_path = output_dir / "tree_models_report.md"
    preview_path = output_dir / "tree_models_preview.html"
    trunks_stl_path = surfaces_dir / "tree_trunks.stl"
    crowns_stl_path = surfaces_dir / "tree_crowns.stl"
    combined_stl_path = surfaces_dir / "trees_combined.stl"
    species_crowns_dir = surfaces_dir / "species_crowns"
    surface_origin_x, surface_origin_y = _lonlat_to_epsg25832(config.region.center_lon, config.region.center_lat)
    terrain_geometry_path = config.inputs.tree_terrain_geometry_path
    terrain_sampler = None
    if terrain_geometry_path is not None:
        validate_completed_city_models_terrain(config, terrain_geometry_path)
        terrain_sampler = load_terrain_sampler(terrain_geometry_path)

    instances = _build_tree_instances(features, config, surface_origin_x, surface_origin_y, terrain_sampler)
    trunk_triangles: list[Triangle] = []
    crown_triangles: list[Triangle] = []
    for instance in instances:
        trunk_triangles.extend(_trunk_triangles(instance))
        crown_triangles.extend(_crown_triangles(instance))

    local_trunk_triangles = _translate_triangles(trunk_triangles, -surface_origin_x, -surface_origin_y, 0.0)
    local_crown_triangles = _translate_triangles(crown_triangles, -surface_origin_x, -surface_origin_y, 0.0)
    _write_stl(trunks_stl_path, "tree_trunks", local_trunk_triangles)
    _write_stl(crowns_stl_path, "tree_crowns", local_crown_triangles)
    _write_stl(combined_stl_path, "trees_combined", [*local_trunk_triangles, *local_crown_triangles])
    species_crown_paths = _write_species_crown_stls(species_crowns_dir, instances, surface_origin_x, surface_origin_y)

    species_counts = _species_counts(instances)
    placement_path.write_text(json.dumps(_placement_geojson(instances), indent=2, sort_keys=True), encoding="utf-8")
    library_path.write_text(json.dumps(_library_payload(config), indent=2, sort_keys=True), encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            _manifest_payload(
                config,
                tree_features_path,
                placement_path,
                library_path,
                trunks_stl_path,
                crowns_stl_path,
                combined_stl_path,
                species_crown_paths,
                preview_path,
                report_path,
                instances,
                species_counts,
                surface_origin_x,
                surface_origin_y,
                terrain_geometry_path,
            ),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    preview_path.write_text(_render_preview(config, instances, surface_origin_x, surface_origin_y), encoding="utf-8")
    report_path.write_text(
        _render_report(
            config,
            tree_features_path,
            placement_path,
            library_path,
            manifest_path,
            trunks_stl_path,
            crowns_stl_path,
            combined_stl_path,
            species_crown_paths,
            preview_path,
            instances,
            species_counts,
            surface_origin_x,
            surface_origin_y,
            terrain_geometry_path,
        ),
        encoding="utf-8",
    )

    return TreesStageOutput(
        output_directory=output_dir,
        placement_geojson_path=placement_path,
        library_path=library_path,
        manifest_path=manifest_path,
        surfaces_directory=surfaces_dir,
        trunks_stl_path=trunks_stl_path,
        crowns_stl_path=crowns_stl_path,
        combined_stl_path=combined_stl_path,
        preview_path=preview_path,
        report_path=report_path,
        tree_count=len(instances),
        species_counts=species_counts,
    )


def _read_feature_collection(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    features = data.get("features")
    if not isinstance(features, list):
        raise ConfigError(f"GeoJSON feature collection missing features list: {path}")
    return [feature for feature in features if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)]


def _build_tree_instances(
    features: list[dict[str, Any]],
    config: AppConfig,
    surface_origin_x: float,
    surface_origin_y: float,
    terrain_sampler: Callable[[float, float], float] | None,
) -> list[TreeInstance]:
    configured_models = _configured_species_models(config)
    category_mapping = _species_category_mapping(config)
    fallback = (
        _fallback_species_model(config, category_mapping, configured_models)
        if any(
            isinstance(feature.get("properties"), dict)
            and feature["properties"].get("direct_model_category") is None
            for feature in features
        )
        else None
    )
    instances: list[TreeInstance] = []
    for index, feature in enumerate(features, start=1):
        point = _feature_reference_point(feature)
        if point is None:
            continue
        properties = feature.get("properties", {})
        tags = properties.get("tags", {})
        if not isinstance(tags, dict):
            tags = {}
        direct_model = properties.get("direct_model_category")
        source_species_key: str | None = None
        source_species: str | None = None
        if direct_model is not None:
            model = _required_category_model(str(direct_model), configured_models)
            model_source = "urban_planning:model"
            species_name = str(direct_model)
        else:
            source_species_key, source_species = _source_species(tags)
        if direct_model is None and source_species:
            mapped_category = category_mapping.get(_normalize_species_name(source_species))
            if mapped_category is None:
                raise ConfigError(
                    f"tree species {source_species!r} is not present in configured species_category_mapping.json"
                )
            model = _match_category(mapped_category, configured_models)
            if model is None:
                raise ConfigError(f"tree species {source_species!r} maps to unavailable category {mapped_category!r}")
            model_source = f"tag:{source_species_key}:species_category_mapping"
            species_name = source_species
        elif direct_model is None:
            if fallback is None:
                fallback = _fallback_species_model(config, category_mapping, configured_models)
            default_species, fallback_model = fallback
            model = fallback_model
            model_source = f"default:{default_species}:species_category_mapping"
            species_name = default_species
        x, y = _lonlat_to_epsg25832(float(point.x), float(point.y))
        terrain_z = 0.0
        if direct_model is not None:
            height, height_source = _planned_dimension_or(
                properties,
                "height_m",
                lambda: _tree_height(tags, model),
            )
            crown_radius, crown_radius_source = _planned_radius_or(
                properties,
                "crown_diameter_m",
                lambda: _crown_radius(tags, model, height),
            )
            trunk_radius, trunk_radius_source = _planned_radius_or(
                properties,
                "trunk_diameter_m",
                lambda: _trunk_radius(tags, model, height),
            )
        else:
            height, height_source = _tree_height(tags, model)
            crown_radius, crown_radius_source = _crown_radius(tags, model, height)
            trunk_radius, trunk_radius_source = _trunk_radius(tags, model, height)
        trunk_height = max(1.8, min(height * model.crown_base_fraction, height - 1.0))
        if terrain_sampler is not None:
            terrain_local_x = x - surface_origin_x
            terrain_local_y = y - surface_origin_y
            terrain_z = terrain_sampler(terrain_local_x, terrain_local_y) - TREE_TERRAIN_CLEARANCE_M
        used_tags = tuple(source for source in (model_source, height_source, crown_radius_source, trunk_radius_source) if source.startswith("tag:"))
        defaulted_fields = tuple(
            field
            for field, source in (
                ("species_model", model_source),
                ("height_m", height_source),
                ("crown_radius_m", crown_radius_source),
                ("trunk_radius_m", trunk_radius_source),
            )
            if source.startswith("default")
        )
        instances.append(
            TreeInstance(
                tree_id=_planned_tree_id(properties) if direct_model is not None else f"tree_{index:04d}",
                species=species_name,
                source_species=source_species,
                model_category=model.name,
                crown_shape=model.crown_shape,
                x=x,
                y=y,
                z=terrain_z,
                height_m=height,
                crown_radius_m=crown_radius,
                trunk_radius_m=trunk_radius,
                trunk_height_m=trunk_height,
                roi_zone=str(properties.get("roi_zone", "unknown")),
                osm_id=properties.get("osm_id"),
                model_source=model_source,
                height_source=height_source,
                crown_radius_source=crown_radius_source,
                trunk_radius_source=trunk_radius_source,
                used_tags=used_tags,
                defaulted_fields=defaulted_fields,
            )
        )
    return instances


def _configured_species_models(config: AppConfig) -> list[TreeSpeciesModel]:
    models = _load_species_model_library(config.trees.model_library_path)
    if not models:
        raise ConfigError(f"tree model library must contain at least one model: {config.trees.model_library_path}")
    return models


def _fallback_species_model(
    config: AppConfig,
    category_mapping: dict[str, str],
    models: list[TreeSpeciesModel],
) -> tuple[str, TreeSpeciesModel]:
    default_species = config.trees.default.strip()
    mapped_category = category_mapping.get(_normalize_species_name(default_species))
    if mapped_category is None:
        raise ConfigError(
            f"trees.default {default_species!r} is not present in configured species_category_mapping.json"
        )
    model = _match_category(mapped_category, models)
    if model is None:
        raise ConfigError(f"trees.default {default_species!r} maps to unavailable category {mapped_category!r}")
    return default_species, model


def _load_species_model_library(path: Path) -> list[TreeSpeciesModel]:
    if not path.exists():
        raise ConfigError(f"tree model library does not exist: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise ConfigError(f"tree model library must contain a models list: {path}")
    models: list[TreeSpeciesModel] = []
    for index, raw_model in enumerate(raw_models, start=1):
        if not isinstance(raw_model, dict):
            raise ConfigError(f"tree model library entry {index} must be an object: {path}")
        name = raw_model.get("name")
        aliases = raw_model.get("aliases", [])
        if not isinstance(name, str) or not name:
            raise ConfigError(f"tree model library entry {index} has no name: {path}")
        if not isinstance(aliases, list) or not all(isinstance(alias, str) and alias for alias in aliases):
            raise ConfigError(f"tree model library entry {name} aliases must be non-empty strings")
        models.append(
            TreeSpeciesModel(
                name=name,
                aliases=tuple(dict.fromkeys([name.lower(), *(alias.lower() for alias in aliases)])),
                default_height_m=_positive_float(raw_model, "default_height_m", name, path),
                default_crown_radius_m=_positive_float(raw_model, "default_crown_radius_m", name, path),
                default_trunk_radius_m=_positive_float(raw_model, "default_trunk_radius_m", name, path),
                crown_base_fraction=_fraction(raw_model, "crown_base_fraction", name, path),
                crown_shape=_optional_model_str(raw_model, "crown_shape", "ellipsoid"),
            )
        )
    return models


def _optional_model_str(raw_model: dict[str, Any], key: str, default: str) -> str:
    value = raw_model.get(key, default)
    if not isinstance(value, str) or not value:
        return default
    return value


def _positive_float(raw_model: dict[str, Any], key: str, name: str, path: Path) -> float:
    value = raw_model.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0.0:
        raise ConfigError(f"tree model {name} has invalid {key} in {path}")
    return float(value)


def _fraction(raw_model: dict[str, Any], key: str, name: str, path: Path) -> float:
    value = _positive_float(raw_model, key, name, path)
    if value >= 1.0:
        raise ConfigError(f"tree model {name} {key} must be less than 1.0 in {path}")
    return value


def _feature_reference_point(feature: dict[str, Any]) -> Point | None:
    try:
        geometry = shape(feature["geometry"])
    except (KeyError, TypeError, ValueError):
        return None
    if geometry.is_empty:
        return None
    if isinstance(geometry, Point):
        return geometry
    if isinstance(geometry, MultiPoint):
        return geometry.centroid
    return geometry.representative_point()


def _source_species(tags: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in ("species", "genus", "taxon", "name"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()
    return None, None


def _match_category(category: str | None, models: list[TreeSpeciesModel]) -> TreeSpeciesModel | None:
    if category is None:
        return None
    normalized = _normalize_species_name(category)
    for model in models:
        if _normalize_species_name(model.name) == normalized:
            return model
        if any(_normalize_species_name(alias) == normalized for alias in model.aliases):
            return model
    return None


def _required_category_model(category: str, models: list[TreeSpeciesModel]) -> TreeSpeciesModel:
    model = _match_category(category, models)
    if model is None:
        raise ConfigError(f"planned tree model category {category!r} is unavailable")
    return model


def _planned_tree_id(properties: dict[str, Any]) -> str:
    value = properties.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("planned tree requires a non-empty id")
    return value.strip()


def _planned_dimension_or(
    properties: dict[str, Any],
    key: str,
    fallback: Callable[[], tuple[float, str]],
) -> tuple[float, str]:
    value = properties.get(key)
    if value is None:
        return fallback()
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)) or value <= 0:
        raise ConfigError(f"planned tree {key} must be a finite positive number")
    return float(value), f"urban_planning:{key}"


def _planned_radius_or(
    properties: dict[str, Any],
    key: str,
    fallback: Callable[[], tuple[float, str]],
) -> tuple[float, str]:
    diameter, source = _planned_dimension_or(properties, key, fallback)
    if source == f"urban_planning:{key}":
        return diameter / 2.0, source
    return diameter, source


def _species_category_mapping(config: AppConfig) -> dict[str, str]:
    path = config.trees.category_mapping_path
    if path is None:
        return {}
    if not path.exists():
        raise ConfigError(f"tree species category mapping does not exist: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_mapping = payload.get("species_to_category")
    if not isinstance(raw_mapping, dict):
        raise ConfigError(f"tree species category mapping must contain species_to_category: {path}")
    mapping: dict[str | None, str] = {}
    for species, category in raw_mapping.items():
        if not isinstance(species, str) or not isinstance(category, str) or not category:
            raise ConfigError(f"invalid species/category entry in {path}")
        normalized_species = _normalize_species_name(species)
        if normalized_species and normalized_species not in {"-", "--", "unknown", "sconosciuto", "non noto", "n/a", "da riconoscere"}:
            mapping[normalized_species] = category
    return mapping


def _normalize_species_name(value: str) -> str:
    return " ".join(value.lower().replace('"', " ").replace("'", " ").split())


def _tree_height(tags: dict[str, Any], model: TreeSpeciesModel) -> tuple[float, str]:
    tag_key, tagged_height = _first_float_tag(tags, ("height", "tree:height"))
    if tagged_height is not None and tagged_height > 1.5:
        return min(tagged_height, 35.0), f"tag:{tag_key}"
    _circumference_key, circumference = _first_float_tag(tags, ("circumference",))
    if circumference is not None and circumference > 0.0:
        return max(4.0, min(model.default_height_m, 6.0 + 2.2 * math.sqrt(circumference))), "allometry:circumference"
    return model.default_height_m, f"default:{model.name}.height_m"


def _crown_radius(tags: dict[str, Any], model: TreeSpeciesModel, height: float) -> tuple[float, str]:
    tag_key, crown_diameter = _first_float_tag(tags, ("crown:diameter", "diameter_crown"))
    if crown_diameter is not None and crown_diameter > 0.5:
        return min(crown_diameter / 2.0, height * 0.48), f"tag:{tag_key}"
    return min(model.default_crown_radius_m, height * 0.42), f"default:{model.name}.crown_radius_m"


def _trunk_radius(tags: dict[str, Any], model: TreeSpeciesModel, height: float) -> tuple[float, str]:
    tag_key, diameter = _first_float_tag(tags, ("diameter", "trunk:diameter"))
    if diameter is not None and diameter > 0.05:
        return diameter / 2.0, f"tag:{tag_key}"
    _circumference_key, circumference = _first_float_tag(tags, ("circumference",))
    if circumference is not None and circumference > 0.1:
        return circumference / (2.0 * math.pi), "allometry:circumference"
    return min(model.default_trunk_radius_m, height * 0.035), f"default:{model.name}.trunk_radius_m"


def _first_float_tag(tags: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, float | None]:
    for key in keys:
        value = _float_tag(tags.get(key))
        if value is not None:
            return key, value
    return None, None


def _float_tag(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = value.lower().replace("metres", "").replace("meters", "").replace("meter", "")
    cleaned = cleaned.replace("m", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _trunk_triangles(instance: TreeInstance) -> list[Triangle]:
    bottom = _circle_points(instance.x, instance.y, instance.z, instance.trunk_radius_m, TRUNK_SEGMENTS)
    top = _circle_points(
        instance.x,
        instance.y,
        instance.z + instance.trunk_height_m,
        instance.trunk_radius_m * 0.72,
        TRUNK_SEGMENTS,
    )
    triangles: list[Triangle] = []
    center_bottom = (instance.x, instance.y, instance.z)
    center_top = (instance.x, instance.y, instance.z + instance.trunk_height_m)
    for index in range(TRUNK_SEGMENTS):
        next_index = (index + 1) % TRUNK_SEGMENTS
        triangles.extend(
            _quad_triangles(f"{instance.tree_id}_trunk", bottom[index], bottom[next_index], top[next_index], top[index])
        )
        triangles.append((f"{instance.tree_id}_trunk_cap", center_bottom, bottom[next_index], bottom[index]))
        triangles.append((f"{instance.tree_id}_trunk_cap", center_top, top[index], top[next_index]))
    return triangles


def _crown_triangles(instance: TreeInstance) -> list[Triangle]:
    if instance.crown_shape == "conical":
        return _conical_crown_triangles(instance)
    center_z = instance.z + (instance.trunk_height_m + instance.height_m) / 2.0
    radius_z = max(0.8, (instance.height_m - instance.trunk_height_m) / 2.0)
    crown_radius = instance.crown_radius_m
    if instance.crown_shape == "umbrella":
        crown_radius *= 1.08
    elif instance.crown_shape == "columnar":
        crown_radius *= 0.72
    elif instance.crown_shape == "tuft":
        crown_radius *= 0.78
    rings: list[list[Point3]] = []
    for ring_index in range(CROWN_RINGS + 1):
        phi = -math.pi / 2.0 + math.pi * ring_index / CROWN_RINGS
        rings.append(
            _circle_points(
                instance.x,
                instance.y,
                center_z + radius_z * math.sin(phi),
                crown_radius * math.cos(phi),
                CROWN_SEGMENTS,
            )
        )

    triangles: list[Triangle] = []
    for ring_index in range(CROWN_RINGS):
        lower = rings[ring_index]
        upper = rings[ring_index + 1]
        for segment in range(CROWN_SEGMENTS):
            next_segment = (segment + 1) % CROWN_SEGMENTS
            triangles.extend(
                _quad_triangles(
                    f"{instance.tree_id}_crown",
                    lower[segment],
                    lower[next_segment],
                    upper[next_segment],
                    upper[segment],
                )
            )
    return triangles


def _conical_crown_triangles(instance: TreeInstance) -> list[Triangle]:
    base_z = instance.z + instance.trunk_height_m * 0.82
    apex = (instance.x, instance.y, instance.z + instance.height_m)
    base_points = _circle_points(instance.x, instance.y, base_z, instance.crown_radius_m, CROWN_SEGMENTS)
    base_center = (instance.x, instance.y, base_z)
    triangles: list[Triangle] = []
    for segment in range(CROWN_SEGMENTS):
        next_segment = (segment + 1) % CROWN_SEGMENTS
        triangles.append((f"{instance.tree_id}_crown", base_points[segment], base_points[next_segment], apex))
        triangles.append((f"{instance.tree_id}_crown", base_center, base_points[next_segment], base_points[segment]))
    return triangles


def _write_species_crown_stls(
    species_crowns_dir: Path,
    instances: list[TreeInstance],
    surface_origin_x: float,
    surface_origin_y: float,
) -> dict[str, Path]:
    species_crowns_dir.mkdir(parents=True, exist_ok=True)
    for stale_path in species_crowns_dir.glob("*.stl"):
        stale_path.unlink()
    grouped: dict[str, list[Triangle]] = {}
    for instance in instances:
        grouped.setdefault(instance.species, []).extend(_crown_triangles(instance))
    paths: dict[str, Path] = {}
    for species, triangles in sorted(grouped.items()):
        slug = _slug(species)
        path = species_crowns_dir / f"{slug}_crowns.stl"
        local_triangles = _translate_triangles(triangles, -surface_origin_x, -surface_origin_y, 0.0)
        _write_stl(path, slug, local_triangles)
        paths[species] = path
    return paths


def _slug(value: str) -> str:
    normalized = []
    for character in value.lower():
        if character.isalnum():
            normalized.append(character)
        elif normalized and normalized[-1] != "_":
            normalized.append("_")
    return "".join(normalized).strip("_") or "unknown_species"


def _circle_points(x: float, y: float, z: float, radius: float, segments: int) -> list[Point3]:
    return [
        (
            x + radius * math.cos(2.0 * math.pi * index / segments),
            y + radius * math.sin(2.0 * math.pi * index / segments),
            z,
        )
        for index in range(segments)
    ]


def _quad_triangles(label: str, a: Point3, b: Point3, c: Point3, d: Point3) -> list[Triangle]:
    return [(label, a, b, c), (label, a, c, d)]


def _write_stl(path: Path, name: str, triangles: list[Triangle]) -> None:
    lines = [f"solid {name}"]
    for _label, a, b, c in triangles:
        normal = _normal(a, b, c)
        lines.extend(
            [
                f"  facet normal {normal[0]:.6g} {normal[1]:.6g} {normal[2]:.6g}",
                "    outer loop",
                f"      vertex {a[0]:.3f} {a[1]:.3f} {a[2]:.3f}",
                f"      vertex {b[0]:.3f} {b[1]:.3f} {b[2]:.3f}",
                f"      vertex {c[0]:.3f} {c[1]:.3f} {c[2]:.3f}",
                "    endloop",
                "  endfacet",
            ]
        )
    lines.append(f"endsolid {name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _normal(a: Point3, b: Point3, c: Point3) -> Point3:
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length == 0.0:
        return 0.0, 0.0, 0.0
    return nx / length, ny / length, nz / length


def _lonlat_to_epsg25832(lon: float, lat: float) -> tuple[float, float]:
    semi_major = 6378137.0
    flattening = 1 / 298.257223563
    eccentricity_sq = flattening * (2 - flattening)
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    lon0 = math.radians(9.0)
    k0 = 0.9996
    false_easting = 500000.0
    n = semi_major / math.sqrt(1 - eccentricity_sq * math.sin(lat_rad) ** 2)
    t = math.tan(lat_rad) ** 2
    c = (eccentricity_sq / (1 - eccentricity_sq)) * math.cos(lat_rad) ** 2
    a = (lon_rad - lon0) * math.cos(lat_rad)
    m = semi_major * (
        (1 - eccentricity_sq / 4 - 3 * eccentricity_sq**2 / 64 - 5 * eccentricity_sq**3 / 256) * lat_rad
        - (3 * eccentricity_sq / 8 + 3 * eccentricity_sq**2 / 32 + 45 * eccentricity_sq**3 / 1024)
        * math.sin(2 * lat_rad)
        + (15 * eccentricity_sq**2 / 256 + 45 * eccentricity_sq**3 / 1024) * math.sin(4 * lat_rad)
        - (35 * eccentricity_sq**3 / 3072) * math.sin(6 * lat_rad)
    )
    easting = false_easting + k0 * n * (
        a + (1 - t + c) * a**3 / 6 + (5 - 18 * t + t**2 + 72 * c - 58 * eccentricity_sq) * a**5 / 120
    )
    northing = k0 * (
        m
        + n
        * math.tan(lat_rad)
        * (
            a**2 / 2
            + (5 - t + 9 * c + 4 * c**2) * a**4 / 24
            + (61 - 58 * t + t**2 + 600 * c - 330 * eccentricity_sq) * a**6 / 720
        )
    )
    return easting, northing


def _species_counts(instances: list[TreeInstance]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for instance in instances:
        counts[instance.species] = counts.get(instance.species, 0) + 1
    return counts


def _category_counts(instances: list[TreeInstance]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for instance in instances:
        counts[instance.model_category] = counts.get(instance.model_category, 0) + 1
    return counts


def _information_summary(instances: list[TreeInstance]) -> dict[str, Any]:
    species_tag_model_count = sum(1 for instance in instances if instance.model_source.startswith("tag:"))
    fallback_model_count = sum(1 for instance in instances if instance.model_source.startswith("default:"))
    planning_model_count = sum(
        1 for instance in instances if instance.model_source.startswith("urban_planning:")
    )
    tag_counts = {
        "species_model": species_tag_model_count,
        "height_m": sum(1 for instance in instances if instance.height_source.startswith("tag:")),
        "crown_radius_m": sum(1 for instance in instances if instance.crown_radius_source.startswith("tag:")),
        "trunk_radius_m": sum(1 for instance in instances if instance.trunk_radius_source.startswith("tag:")),
    }
    allometry_counts = {
        "height_m": sum(1 for instance in instances if instance.height_source.startswith("allometry:")),
        "trunk_radius_m": sum(1 for instance in instances if instance.trunk_radius_source.startswith("allometry:")),
    }
    default_counts = {
        "species_model": fallback_model_count,
        "height_m": sum(1 for instance in instances if instance.height_source.startswith("default:")),
        "crown_radius_m": sum(1 for instance in instances if instance.crown_radius_source.startswith("default:")),
        "trunk_radius_m": sum(1 for instance in instances if instance.trunk_radius_source.startswith("default:")),
    }
    planning_counts = {
        "species_model": planning_model_count,
        "height_m": sum(1 for instance in instances if instance.height_source.startswith("urban_planning:")),
        "crown_radius_m": sum(
            1 for instance in instances if instance.crown_radius_source.startswith("urban_planning:")
        ),
        "trunk_radius_m": sum(
            1 for instance in instances if instance.trunk_radius_source.startswith("urban_planning:")
        ),
    }
    any_information = sum(
        1
        for instance in instances
        if instance.used_tags
        or instance.model_source.startswith("urban_planning:")
        or instance.height_source.startswith(("allometry:", "urban_planning:"))
        or instance.crown_radius_source.startswith("urban_planning:")
        or instance.trunk_radius_source.startswith(("allometry:", "urban_planning:"))
    )
    full_tag_information = sum(
        1
        for instance in instances
        if instance.model_source.startswith("tag:")
        and instance.height_source.startswith("tag:")
        and instance.crown_radius_source.startswith("tag:")
        and instance.trunk_radius_source.startswith("tag:")
    )
    return {
        "tree_count": len(instances),
        "trees_with_any_model_input_tags_or_allometry": any_information,
        "trees_with_species_tag_model": species_tag_model_count,
        "trees_with_direct_planning_model": planning_model_count,
        "trees_with_fallback_species_model": fallback_model_count,
        "trees_with_all_primary_values_from_tags": full_tag_information,
        "tag_value_counts": tag_counts,
        "allometry_value_counts": allometry_counts,
        "default_value_counts": default_counts,
        "planning_value_counts": planning_counts,
        "default_model_count": fallback_model_count,
        "fallback_model_count": fallback_model_count,
    }


def _placement_geojson(instances: list[TreeInstance]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(instance.x, 3), round(instance.y, 3), round(instance.z, 3)]},
                "properties": {
                    "tree_id": instance.tree_id,
                    "species": instance.species,
                    "species_model": instance.model_category,
                    "source_species": instance.source_species,
                    "model_category": instance.model_category,
                    "crown_shape": instance.crown_shape,
                    "height_m": round(instance.height_m, 3),
                    "crown_radius_m": round(instance.crown_radius_m, 3),
                    "trunk_radius_m": round(instance.trunk_radius_m, 3),
                    "trunk_height_m": round(instance.trunk_height_m, 3),
                    "roi_zone": instance.roi_zone,
                    "osm_id": instance.osm_id,
                    "projected_crs": "EPSG:25832",
                    "model_source": instance.model_source,
                    "height_source": instance.height_source,
                    "crown_radius_source": instance.crown_radius_source,
                    "trunk_radius_source": instance.trunk_radius_source,
                    "used_tags": list(instance.used_tags),
                    "defaulted_fields": list(instance.defaulted_fields),
                },
            }
            for instance in instances
        ],
    }


def _library_payload(config: AppConfig) -> dict[str, Any]:
    models = _configured_species_models(config)
    category_mapping = _species_category_mapping(config)
    default_species = config.trees.default.strip()
    default_model = _match_category(
        category_mapping.get(_normalize_species_name(default_species)),
        models,
    )
    assumptions = [
        "Species are represented by low-poly parametric models for CFD geometry preparation.",
        "Planning features with a direct model category bypass species mapping.",
        "Tree features with species tags must resolve through the configured species/category mapping.",
        "Available OSM height, crown diameter, trunk diameter, and circumference tags override defaults when parseable; missing fields keep species defaults.",
    ]
    if default_model is not None:
        assumptions.insert(
            3,
            f"Tree features without species tags use the configured default species {default_species!r}, mapped to category {default_model.name!r}.",
        )
    return {
        "configured_default_species": default_species,
        "configured_default_category": default_model.name if default_model is not None else None,
        "model_library_path": str(config.trees.model_library_path) if config.trees.model_library_path is not None else None,
        "category_mapping_path": str(config.trees.category_mapping_path) if config.trees.category_mapping_path is not None else None,
        "supported_species": {
            name: {
                "aliases": list(model.aliases),
                "default_height_m": model.default_height_m,
                "default_crown_radius_m": model.default_crown_radius_m,
                "default_trunk_radius_m": model.default_trunk_radius_m,
                "crown_base_fraction": model.crown_base_fraction,
                "crown_shape": model.crown_shape,
            }
            for name, model in sorted((model.name, model) for model in models)
        },
        "assumptions": assumptions,
    }


def _manifest_payload(
    config: AppConfig,
    tree_features_path: Path,
    placement_path: Path,
    library_path: Path,
    trunks_stl_path: Path,
    crowns_stl_path: Path,
    combined_stl_path: Path,
    species_crown_paths: dict[str, Path],
    preview_path: Path,
    report_path: Path,
    instances: list[TreeInstance],
    species_counts: dict[str, int],
    surface_origin_x: float,
    surface_origin_y: float,
    terrain_geometry_path: Path | None,
) -> dict[str, Any]:
    return {
        "stage": "trees",
        "region": config.region.name,
        "crs": config.region.crs,
        "source_tree_features": str(tree_features_path),
        "placement_geojson": str(placement_path),
        "species_library": str(library_path),
        "surfaces": {
            "trunks": str(trunks_stl_path),
            "crowns": str(crowns_stl_path),
            "combined": str(combined_stl_path),
            "species_crowns": {species: str(path) for species, path in species_crown_paths.items()},
        },
        "preview": str(preview_path),
        "report": str(report_path),
        "tree_count": len(instances),
        "species_counts": species_counts,
        "category_counts": _category_counts(instances),
        "information_summary": _information_summary(instances),
        "fallback": {
            "default_species": config.trees.default,
            "model_source": f"default:{config.trees.default}:species_category_mapping",
            "tree_count": _information_summary(instances)["fallback_model_count"],
        },
        "surface_frame": {
            "name": "city4cfd_local_origin",
            "origin_x": round(surface_origin_x, 3),
            "origin_y": round(surface_origin_y, 3),
            "description": "Tree STL surfaces are translated to the same local projected origin used by the City4CFD handoff.",
        },
        "terrain_geometry_path": str(terrain_geometry_path) if terrain_geometry_path is not None else None,
        "tree_information": [_tree_information_payload(instance) for instance in instances],
        "status": "parametric_tree_models_generated",
    }


def _tree_information_payload(instance: TreeInstance) -> dict[str, Any]:
    return {
        "tree_id": instance.tree_id,
        "osm_id": instance.osm_id,
        "species": instance.species,
        "species_model": instance.model_category,
        "source_species": instance.source_species,
        "model_category": instance.model_category,
        "crown_shape": instance.crown_shape,
        "model_source": instance.model_source,
        "height_source": instance.height_source,
        "crown_radius_source": instance.crown_radius_source,
        "trunk_radius_source": instance.trunk_radius_source,
        "used_tags": list(instance.used_tags),
        "defaulted_fields": list(instance.defaulted_fields),
    }


def _render_preview(config: AppConfig, instances: list[TreeInstance], surface_origin_x: float, surface_origin_y: float) -> str:
    scene_json = json.dumps(_scene_data(instances, surface_origin_x, surface_origin_y), separators=(",", ":"))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{escape(config.region.name)} tree model preview</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; color: #1f2933; background: #f8fafc; }}
    canvas {{ display: block; width: min(1080px, 100%); height: min(68vh, 720px); border: 1px solid #c8d1dc; background: #ffffff; margin-bottom: 1.2rem; }}
    .note {{ max-width: 1080px; color: #52606d; line-height: 1.35; }}
    .legend {{ display: flex; gap: 1rem; flex-wrap: wrap; margin: 0.8rem 0; color: #334155; }}
    .swatch {{ display: inline-block; width: 0.9rem; height: 0.9rem; margin-right: 0.35rem; vertical-align: -0.12rem; }}
    .species-list {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.35rem 1rem; max-width: 1080px; margin: 0.8rem 0 1rem; color: #334155; }}
    .species-list div {{ border-bottom: 1px solid #e2e8f0; padding: 0.22rem 0; }}
    .zoom-controls {{ display: flex; gap: 0.45rem; flex-wrap: wrap; margin: 0.7rem 0; }}
    .zoom-controls button {{ border: 1px solid #94a3b8; background: #ffffff; color: #1f2933; padding: 0.35rem 0.65rem; border-radius: 0.35rem; cursor: pointer; }}
    .zoom-controls button:hover {{ background: #e2e8f0; }}
  </style>
</head>
<body>
  <h1>{escape(config.region.name)} parametric tree models</h1>
  <div class="zoom-controls" aria-label="Tree preview zoom controls">
    <button type="button" id="zoomIn">Zoom in</button>
    <button type="button" id="zoomOut">Zoom out</button>
    <button type="button" id="zoomReset">Reset zoom</button>
  </div>
  <canvas id="treeScene" width="1400" height="900" aria-label="3D parametric tree model preview"></canvas>
  <div class="legend">
    <span><span class="swatch" style="background:#7c4a21"></span>trunks</span>
    <span><span class="swatch" style="background:#15803d"></span>species crowns</span>
  </div>
  <p class="note">Species-tag models: <strong><span id="tagInfo"></span></strong>. Direct planning models: <strong><span id="planningInfo"></span></strong>. Fallback species models: <strong><span id="defaultInfo"></span></strong>.</p>
  <h2>Named Trees</h2>
  <div class="species-list" id="speciesList"></div>
  <p class="note">Drag to rotate the 3D tree preview. Use the mouse wheel or zoom buttons to zoom in and out. The placement GeoJSON stays in projected EPSG:25832 coordinates, while the STL surfaces are translated to the same local origin used by the City4CFD handoff so they line up with module 3 output.</p>
  <script>
    const scene = {scene_json};
    const view = {{ canvas: document.getElementById("treeScene"), yaw: -0.7, pitch: 0.82, zoom: 1.0, dragging: false, last: null }};
    function resize() {{
      const ratio = window.devicePixelRatio || 1;
      const rect = view.canvas.getBoundingClientRect();
      view.canvas.width = Math.max(640, Math.round(rect.width * ratio));
      view.canvas.height = Math.max(420, Math.round(rect.height * ratio));
      draw();
    }}
    function rotate(point) {{
      const [x, y, z] = point;
      const cy = Math.cos(view.yaw), sy = Math.sin(view.yaw), cp = Math.cos(view.pitch), sp = Math.sin(view.pitch);
      const rx = x * cy - y * sy, ry = x * sy + y * cy;
      return [rx, ry * cp + z * sp, ry * sp - z * cp];
    }}
    function project(point) {{
      const [x, y, z] = rotate(point);
      const scale = Math.min(view.canvas.width, view.canvas.height) * 0.42 / scene.extent * view.zoom;
      return [view.canvas.width / 2 + x * scale, view.canvas.height * 0.64 - y * scale, z];
    }}
    function setZoom(nextZoom) {{ view.zoom = Math.max(0.35, Math.min(5.0, nextZoom)); draw(); }}
    function line(a, b, color, width) {{
      const ctx = view.canvas.getContext("2d"), pa = project(a), pb = project(b);
      ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath(); ctx.moveTo(pa[0], pa[1]); ctx.lineTo(pb[0], pb[1]); ctx.stroke();
    }}
    function ellipse(tree) {{
      const ctx = view.canvas.getContext("2d"), center = project([tree.x, tree.y, tree.crownCenterZ]), top = project([tree.x, tree.y, tree.height]), side = project([tree.x + tree.crownRadius, tree.y, tree.crownCenterZ]);
      ctx.fillStyle = tree.crownFill; ctx.strokeStyle = tree.crownStroke; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.ellipse(center[0], center[1], Math.max(4, Math.abs(side[0] - center[0])), Math.max(4, Math.abs(top[1] - center[1])), 0, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    }}
    function draw() {{
      const canvas = view.canvas, ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height); ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
      for (let i = -4; i <= 4; i++) {{ line([-scene.extent, i * scene.extent / 4, 0], [scene.extent, i * scene.extent / 4, 0], "#e2e8f0", 1); line([i * scene.extent / 4, -scene.extent, 0], [i * scene.extent / 4, scene.extent, 0], "#e2e8f0", 1); }}
      const trees = [...scene.trees].sort((a, b) => rotate([a.x, a.y, a.height / 2])[2] - rotate([b.x, b.y, b.height / 2])[2]);
      for (const tree of trees) {{ line([tree.x, tree.y, 0], [tree.x, tree.y, tree.trunkHeight], "#7c4a21", Math.max(2, tree.trunkRadius * 5)); ellipse(tree); }}
      ctx.fillStyle = "#334155"; ctx.font = `${{Math.max(13, canvas.width / 95)}}px Arial`; ctx.fillText(`Trees: ${{scene.trees.length}}`, 18, 28);
      ctx.fillText(`Species-tag model: ${{scene.information.trees_with_species_tag_model}}`, 18, 52);
      ctx.fillText(`Direct planning model: ${{scene.information.trees_with_direct_planning_model}}`, 18, 76);
      ctx.fillText(`Fallback model: ${{scene.information.fallback_model_count}}`, 18, 100);
    }}
    view.canvas.addEventListener("pointerdown", (event) => {{ view.dragging = true; view.last = [event.clientX, event.clientY]; view.canvas.setPointerCapture(event.pointerId); }});
    view.canvas.addEventListener("pointermove", (event) => {{ if (!view.dragging || !view.last) return; view.yaw += (event.clientX - view.last[0]) * 0.008; view.pitch = Math.max(0.15, Math.min(1.45, view.pitch + (event.clientY - view.last[1]) * 0.006)); view.last = [event.clientX, event.clientY]; draw(); }});
    view.canvas.addEventListener("pointerup", () => {{ view.dragging = false; view.last = null; }});
    view.canvas.addEventListener("wheel", (event) => {{ event.preventDefault(); setZoom(view.zoom * (event.deltaY < 0 ? 1.12 : 0.88)); }}, {{ passive: false }});
    document.getElementById("zoomIn").addEventListener("click", () => setZoom(view.zoom * 1.2));
    document.getElementById("zoomOut").addEventListener("click", () => setZoom(view.zoom / 1.2));
    document.getElementById("zoomReset").addEventListener("click", () => setZoom(1.0));
    document.getElementById("tagInfo").textContent = `${{scene.information.trees_with_species_tag_model}} / ${{scene.information.tree_count}} trees`;
    document.getElementById("planningInfo").textContent = `${{scene.information.trees_with_direct_planning_model}} / ${{scene.information.tree_count}} trees`;
    document.getElementById("defaultInfo").textContent = `${{scene.information.fallback_model_count}} / ${{scene.information.tree_count}} trees`;
    const speciesList = document.getElementById("speciesList");
    for (const item of scene.namedSpecies) {{
      const row = document.createElement("div");
      row.textContent = `${{item.species}}: ${{item.count}}`;
      speciesList.appendChild(row);
    }}
    window.addEventListener("resize", resize); resize();
  </script>
</body>
</html>
"""


def _scene_data(instances: list[TreeInstance], surface_origin_x: float, surface_origin_y: float) -> dict[str, Any]:
    if not instances:
        return {
            "extent": 10.0,
            "trees": [],
            "information": _information_summary(instances),
            "namedSpecies": [],
            "surfaceFrame": {"originX": round(surface_origin_x, 3), "originY": round(surface_origin_y, 3)},
            "viewCenter": {"x": round(surface_origin_x, 3), "y": round(surface_origin_y, 3)},
        }
    center_x = (min(instance.x for instance in instances) + max(instance.x for instance in instances)) / 2.0
    center_y = (min(instance.y for instance in instances) + max(instance.y for instance in instances)) / 2.0
    local_points = [(instance.x - center_x, instance.y - center_y) for instance in instances]
    extent = max(
        10.0,
        max(
            max(abs(x), abs(y)) + instance.crown_radius_m
            for (x, y), instance in zip(local_points, instances, strict=True)
        ),
    )
    return {
        "extent": extent,
        "information": _information_summary(instances),
        "namedSpecies": [
            {"species": species, "count": count}
            for species, count in sorted(_species_counts(instances).items(), key=lambda item: (-item[1], item[0]))
        ],
        "surfaceFrame": {"originX": round(surface_origin_x, 3), "originY": round(surface_origin_y, 3)},
        "viewCenter": {"x": round(center_x, 3), "y": round(center_y, 3)},
        "trees": [
            {
                "id": instance.tree_id,
                "species": instance.species,
                "category": instance.model_category,
                "crownFill": _preview_crown_fill(instance.species),
                "crownStroke": _preview_crown_stroke(instance.species),
                "x": instance.x - center_x,
                "y": instance.y - center_y,
                "height": instance.height_m,
                "trunkHeight": instance.trunk_height_m,
                "trunkRadius": instance.trunk_radius_m,
                "crownRadius": instance.crown_radius_m,
                "crownCenterZ": (instance.trunk_height_m + instance.height_m) / 2.0,
            }
            for instance in instances
        ],
    }


def _preview_crown_stroke(species: str) -> str:
    palette = ("#15803d", "#65a30d", "#0f766e", "#4d7c0f", "#166534", "#047857", "#3f6212")
    return palette[sum(ord(character) for character in species) % len(palette)]


def _preview_crown_fill(species: str) -> str:
    stroke = _preview_crown_stroke(species).lstrip("#")
    red = int(stroke[0:2], 16)
    green = int(stroke[2:4], 16)
    blue = int(stroke[4:6], 16)
    return f"rgba({red}, {green}, {blue}, 0.30)"


def _translate_triangles(triangles: list[Triangle], dx: float, dy: float, dz: float) -> list[Triangle]:
    return [
        (
            label,
            (a[0] + dx, a[1] + dy, a[2] + dz),
            (b[0] + dx, b[1] + dy, b[2] + dz),
            (c[0] + dx, c[1] + dy, c[2] + dz),
        )
        for label, a, b, c in triangles
    ]


def _render_report(
    config: AppConfig,
    tree_features_path: Path,
    placement_path: Path,
    library_path: Path,
    manifest_path: Path,
    trunks_stl_path: Path,
    crowns_stl_path: Path,
    combined_stl_path: Path,
    species_crown_paths: dict[str, Path],
    preview_path: Path,
    instances: list[TreeInstance],
    species_counts: dict[str, int],
    surface_origin_x: float,
    surface_origin_y: float,
    terrain_geometry_path: Path | None,
) -> str:
    counts = "\n".join(f"- {species}: {count}" for species, count in sorted(species_counts.items())) or "- none"
    category_lines = "\n".join(f"- {category}: {count}" for category, count in sorted(_category_counts(instances).items())) or "- none"
    species_crown_lines = "\n".join(
        f"- {species}: `{path}`"
        for species, path in sorted(species_crown_paths.items())
    ) or "- none"
    information = _information_summary(instances)
    tree_rows = "\n".join(
        "| {tree_id} | {osm_id} | {species} | {category} | {model_source} | {height_source} | {crown_source} | {trunk_source} | {defaulted} |".format(
            tree_id=instance.tree_id,
            osm_id=instance.osm_id if instance.osm_id is not None else "",
            species=instance.species,
            category=instance.model_category,
            model_source=instance.model_source,
            height_source=instance.height_source,
            crown_source=instance.crown_radius_source,
            trunk_source=instance.trunk_radius_source,
            defaulted=", ".join(instance.defaulted_fields) if instance.defaulted_fields else "none",
        )
        for instance in instances[:200]
    )
    if not tree_rows:
        tree_rows = "| none | | | | | | | | |"
    return f"""# Tree Model Generation Report

Region: {config.region.name}
CRS: {config.region.crs}

## Summary

- Source tree features: {tree_features_path}
- Generated tree instances: {len(instances)}
- Species counts:
{counts}
- Category counts:
{category_lines}
- Trees reconstructed from species tags: {information["trees_with_species_tag_model"]} / {information["tree_count"]}
- Trees reconstructed from direct urban-planning models: {information["trees_with_direct_planning_model"]} / {information["tree_count"]}
- Trees reconstructed with configured fallback species model ({config.trees.default}): {information["trees_with_fallback_species_model"]} / {information["tree_count"]}
- Trees with any usable source information or allometry: {information["trees_with_any_model_input_tags_or_allometry"]} / {information["tree_count"]}
- Trees with all primary values directly from tags: {information["trees_with_all_primary_values_from_tags"]} / {information["tree_count"]}
- Defaulted values: species model {information["default_value_counts"]["species_model"]}, height {information["default_value_counts"]["height_m"]}, crown radius {information["default_value_counts"]["crown_radius_m"]}, trunk radius {information["default_value_counts"]["trunk_radius_m"]}

## Per-Tree Model Inputs

| Tree | OSM ID | Species | Category model | Model source | Height source | Crown source | Trunk source | Defaulted fields |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
{tree_rows}

## Outputs

- Tree placements: {placement_path}
- Species library: {library_path}
- Manifest: {manifest_path}
- Trunk STL: {trunks_stl_path}
- Crown STL: {crowns_stl_path}
- Combined STL: {combined_stl_path}
- Species crown STL surfaces:
{species_crown_lines}
- 3D preview: {preview_path}
- STL surface frame: local City4CFD origin at ({surface_origin_x:.3f}, {surface_origin_y:.3f})
- Terrain geometry projection: {terrain_geometry_path if terrain_geometry_path is not None else "not provided"}

## Assumptions

- Tree ground elevation defaults to z=0 when no terrain geometry is provided.
- Tree STL surfaces are translated to the same local projected origin used by the City4CFD handoff, while the placement GeoJSON remains in EPSG:25832.
- When a terrain geometry file is provided, tree bases are projected onto that terrain and placed just below the local surface.
- Trees with species tags must resolve through the configured species/category mapping.
- Trees without species tags use the configured fallback species ({config.trees.default}) through the same species/category mapping.
- Missing dimensions keep default values from the selected category model; available parseable tags override only the corresponding dimension.
- The generated STL files are low-poly CFD handoff geometry and graphical QA artifacts, not botanically detailed assets.
"""
