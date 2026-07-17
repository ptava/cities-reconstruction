"""Place catalogued air-purifier towers in the City4CFD local frame."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
import math
from pathlib import Path
import re
from typing import Any

from cities_reconstruction import __version__
from cities_reconstruction.artifacts import atomic_write_json, atomic_write_text, stage_output_lock
from cities_reconstruction.config import AppConfig, ConfigError
from cities_reconstruction.geometry.stl_regions import (
    REGION_NAMES,
    RegionMesh,
    mesh_bounds,
    read_region_stl,
    transform_region_mesh,
    write_region_stl,
)
from cities_reconstruction.geometry.terrain import (
    TerrainSampler,
    load_terrain_sampler,
    validate_completed_city_models_terrain,
)
from cities_reconstruction.stage_result import StageResult


PURIFIER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
TERRAIN_CLEARANCE_M = 0.05


@dataclass(frozen=True)
class AirPurifierModel:
    name: str
    kind: str
    source_path: Path
    native_width_m: float
    native_depth_m: float
    native_height_m: float
    linear_tolerance_m: float
    mesh: RegionMesh


@dataclass(frozen=True)
class AirPurifierInstance:
    purifier_id: str
    model_name: str
    source_lon: float
    source_lat: float
    projected_x: float
    projected_y: float
    local_x: float
    local_y: float
    base_z: float
    target_width_m: float
    target_depth_m: float
    target_height_m: float
    native_width_m: float
    native_depth_m: float
    native_height_m: float
    scale_x: float
    scale_y: float
    scale_z: float
    rotation_deg: float
    width_source: str
    depth_source: str
    height_source: str
    rotation_source: str
    terrain_source: str
    input_id: str
    source: str
    source_crs: str
    source_feature_index: int
    roi_zone: str
    source_properties: dict[str, Any]


@dataclass(frozen=True)
class AirPurifiersStageOutput:
    output_directory: Path
    placement_geojson_path: Path
    catalog_path: Path
    manifest_path: Path
    surfaces_directory: Path
    combined_stl_path: Path
    instance_stl_paths: dict[str, Path]
    preview_path: Path
    report_path: Path
    purifier_count: int
    model_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "output_directory": str(self.output_directory),
            "placement_geojson_path": str(self.placement_geojson_path),
            "catalog_path": str(self.catalog_path),
            "manifest_path": str(self.manifest_path),
            "surfaces_directory": str(self.surfaces_directory),
            "combined_stl_path": str(self.combined_stl_path),
            "instance_stl_paths": {key: str(value) for key, value in sorted(self.instance_stl_paths.items())},
            "preview_path": str(self.preview_path),
            "report_path": str(self.report_path),
            "purifier_count": self.purifier_count,
            "model_counts": dict(sorted(self.model_counts.items())),
        }


def plan(config: AppConfig) -> StageResult:
    output = config.output.root_directory / "05_air_purifiers"
    catalog = config.air_purifiers.model_library_path
    terrain = config.air_purifiers.terrain_geometry_path
    return StageResult(
        stage="air-purifiers",
        summary="Place normalized air-purifier models and publish CFD-ready three-region STL surfaces.",
        planned_actions=(
            "Read normalized purifier points from 01_shapefiles/air_purifiers.geojson.",
            f"Resolve model library: {catalog if catalog is not None else 'unresolved (required at execution)' }.",
            f"Resolve terrain geometry: {terrain if terrain is not None else 'unresolved (use z=0)' }.",
            "Scale, rotate, terrain-project, and write aggregate/per-unit inlet, outlet, and tower surfaces.",
            "Publish placements, offline preview, report, and completion manifest.",
        ),
        expected_outputs=(output,),
    )


def run(
    config: AppConfig,
    *,
    model_library_path: Path | str | None = None,
    terrain_geometry_path: Path | str | None = None,
) -> AirPurifiersStageOutput:
    if config.region.crs != "EPSG:25832":
        raise ConfigError("air-purifier generation currently supports EPSG:25832 output coordinates")
    output_dir = config.output.root_directory / "05_air_purifiers"
    with stage_output_lock(output_dir, "air-purifiers"):
        return _run_locked(
            config,
            model_library_path=_effective_path(config, model_library_path, config.air_purifiers.model_library_path),
            terrain_geometry_path=_effective_path(config, terrain_geometry_path, config.air_purifiers.terrain_geometry_path),
        )


def _run_locked(
    config: AppConfig,
    *,
    model_library_path: Path | None,
    terrain_geometry_path: Path | None,
) -> AirPurifiersStageOutput:
    if model_library_path is None:
        raise ConfigError("air-purifier model library is unresolved; configure model_library_path or provide an override")
    source_geojson = config.output.root_directory / "01_shapefiles" / "air_purifiers.geojson"
    if not source_geojson.exists():
        raise ConfigError("missing air-purifier GeoJSON. Run `shapefiles` before `air-purifiers`.")

    output_dir = config.output.root_directory / "05_air_purifiers"
    surfaces_dir = output_dir / "surfaces"
    instances_dir = surfaces_dir / "instances"
    placement_path = output_dir / "air_purifier_placements.geojson"
    manifest_path = output_dir / "air_purifier_models_manifest.json"
    preview_path = output_dir / "air_purifier_models_preview.html"
    report_path = output_dir / "air_purifier_models_report.md"
    combined_path = surfaces_dir / "air_purifiers_combined.stl"
    prior_instance_paths = _prior_instance_allowlist(manifest_path, instances_dir)
    manifest_path.unlink(missing_ok=True)

    models = _load_model_library(model_library_path)
    features = _load_features(source_geojson)
    origin_x, origin_y = _lonlat_to_epsg25832(config.region.center_lon, config.region.center_lat)
    terrain_sampler: TerrainSampler | None = None
    if terrain_geometry_path is not None:
        validate_completed_city_models_terrain(config, terrain_geometry_path, context="air-purifier")
        terrain_sampler = load_terrain_sampler(
            terrain_geometry_path,
            footprint_label="air-purifier footprint",
        )
    instances = _resolve_instances(
        features,
        models,
        origin_x=origin_x,
        origin_y=origin_y,
        terrain_path=terrain_geometry_path,
        terrain_sampler=terrain_sampler,
    )

    aggregate: RegionMesh = {region: [] for region in REGION_NAMES}
    instance_paths: dict[str, Path] = {}
    instance_meshes: dict[str, RegionMesh] = {}
    for instance in instances:
        transformed = transform_region_mesh(
            models[instance.model_name].mesh,
            scale=(instance.scale_x, instance.scale_y, instance.scale_z),
            rotation_deg=instance.rotation_deg,
            translation=(instance.local_x, instance.local_y, instance.base_z),
        )
        instance_path = instances_dir / f"{instance.purifier_id}.stl"
        write_region_stl(instance_path, transformed)
        instance_paths[instance.purifier_id] = instance_path
        instance_meshes[instance.purifier_id] = transformed
        for region in REGION_NAMES:
            aggregate[region].extend(transformed[region])
    write_region_stl(combined_path, aggregate)

    current_paths = {path.resolve() for path in instance_paths.values()}
    for stale_path in prior_instance_paths - current_paths:
        stale_path.unlink(missing_ok=True)

    model_counts = _counts(instance.model_name for instance in instances)
    input_counts = _counts(instance.input_id for instance in instances)
    parameter_source_counts = {
        field: _counts(getattr(instance, field) for instance in instances)
        for field in ("height_source", "width_source", "depth_source", "rotation_source")
    }
    atomic_write_json(placement_path, _placement_payload(instances))
    atomic_write_text(preview_path, _render_preview(instances, instance_meshes, origin_x, origin_y))
    atomic_write_text(
        report_path,
        _render_report(
            source_geojson, model_library_path, terrain_geometry_path, origin_x, origin_y,
            instances, model_counts, input_counts, parameter_source_counts,
            placement_path, combined_path, instance_paths, preview_path, manifest_path,
        ),
    )
    manifest = {
        "manifest_schema_version": 1,
        "application_version": __version__,
        "stage": "air-purifiers",
        "stage_status": "completed",
        "source_geojson": str(source_geojson),
        "model_library": str(model_library_path),
        "model_files": {name: str(model.source_path) for name, model in sorted(models.items())},
        "resolved_overrides": {
            "model_library_path": str(model_library_path),
            "terrain_geometry_path": str(terrain_geometry_path) if terrain_geometry_path else None,
        },
        "local_origin": {"crs": "EPSG:25832", "easting": origin_x, "northing": origin_y},
        "terrain": {
            "path": str(terrain_geometry_path) if terrain_geometry_path else None,
            "status": "projected" if terrain_geometry_path else "z=0 fallback",
            "base_clearance_m": TERRAIN_CLEARANCE_M if terrain_geometry_path else 0.0,
            "footprint_validation": "all four rotated bounding-box corners",
        },
        "counts": {
            "purifiers": len(instances), "models": model_counts,
            "inputs": input_counts,
        },
        "parameter_source_counts": parameter_source_counts,
        "outputs": {
            "placements": str(placement_path), "combined_surface": str(combined_path),
            "instances": {key: str(value) for key, value in sorted(instance_paths.items())},
            "preview": str(preview_path), "report": str(report_path),
            "openfoam_handoff": {
                "aggregate_surface": str(combined_path),
                "regions": list(REGION_NAMES),
            },
        },
    }
    # Completion marker: no artifact is written after this manifest.
    atomic_write_json(manifest_path, manifest)
    return AirPurifiersStageOutput(
        output_directory=output_dir, placement_geojson_path=placement_path,
        catalog_path=model_library_path, manifest_path=manifest_path,
        surfaces_directory=surfaces_dir, combined_stl_path=combined_path,
        instance_stl_paths=instance_paths, preview_path=preview_path,
        report_path=report_path, purifier_count=len(instances), model_counts=model_counts,
    )


def _effective_path(config: AppConfig, override: Path | str | None, configured: Path | None) -> Path | None:
    if override is None:
        return configured
    path = Path(override)
    return path if path.is_absolute() else (config.path.parent / path).resolve()


def _load_model_library(path: Path) -> dict[str, AirPurifierModel]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid air-purifier model catalog: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ConfigError(f"air-purifier model catalog schema_version must be 1: {path}")
    raw_models = payload.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ConfigError(f"air-purifier model catalog must contain non-empty models: {path}")
    models: dict[str, AirPurifierModel] = {}
    for index, raw in enumerate(raw_models, start=1):
        if not isinstance(raw, dict):
            raise ConfigError(f"invalid model entry {index} in {path}")
        name = _required_text(raw, "name", f"model entry {index}")
        if name in models:
            raise ConfigError(f"duplicate air-purifier model name {name!r} in {path}")
        kind = _required_text(raw, "kind", f"model {name!r}")
        height = _positive_number(raw.get("height_m"), f"model {name!r} height_m")
        tolerance = _positive_number(raw.get("linear_tolerance_m"), f"model {name!r} linear_tolerance_m")
        if kind == "octagonal":
            width = depth = _positive_number(raw.get("base_width_m"), f"model {name!r} base_width_m")
        elif kind == "four_side":
            width = _positive_number(raw.get("width_m"), f"model {name!r} width_m")
            depth = _positive_number(raw.get("depth_m"), f"model {name!r} depth_m")
        else:
            raise ConfigError(f"unknown air-purifier catalog kind {kind!r} for model {name!r}")
        output_path = Path(_required_text(raw, "output_path", f"model {name!r}"))
        if output_path.is_absolute():
            raise ConfigError(
                f"air-purifier model {name!r} requires a relative output_path in catalog {path}"
            )
        source_path = (path.parent / output_path).resolve()
        mesh = read_region_stl(source_path)
        bounds = mesh_bounds(mesh)
        actual = (bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4])
        expected = (width, depth, height)
        if abs(bounds[4]) > tolerance or any(abs(left - right) > tolerance for left, right in zip(actual, expected)):
            raise ConfigError(
                f"air-purifier model {name!r} bounds {actual!r} and base z={bounds[4]} "
                f"do not match catalog dimensions {expected!r} within {tolerance} m: {source_path}"
            )
        models[name] = AirPurifierModel(name, kind, source_path, width, depth, height, tolerance, mesh)
    return models


def _load_features(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid air-purifier GeoJSON: {path}") from exc
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise ConfigError(f"air-purifier GeoJSON must be a FeatureCollection: {path}")
    if not payload["features"]:
        raise ConfigError("no air-purifier features to generate")
    return payload["features"]


def _resolve_instances(
    features: list[Any], models: dict[str, AirPurifierModel], *, origin_x: float, origin_y: float,
    terrain_path: Path | None, terrain_sampler: TerrainSampler | None,
) -> list[AirPurifierInstance]:
    instances: list[AirPurifierInstance] = []
    seen: set[str] = set()
    for index, feature in enumerate(features, start=1):
        if not isinstance(feature, dict):
            raise ConfigError(f"air-purifier feature {index} must be an object")
        geometry = feature.get("geometry")
        properties = feature.get("properties")
        if not isinstance(geometry, dict) or geometry.get("type") != "Point":
            raise ConfigError(f"air-purifier feature {index} must have Point geometry")
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            raise ConfigError(f"air-purifier feature {index} has invalid coordinates")
        lon = _finite_number(coordinates[0], f"air-purifier feature {index} longitude")
        lat = _finite_number(coordinates[1], f"air-purifier feature {index} latitude")
        if not isinstance(properties, dict):
            raise ConfigError(f"air-purifier feature {index} properties must be an object")
        purifier_id = _required_text(properties, "purifier_id", f"air-purifier feature {index}")
        if not PURIFIER_ID_PATTERN.fullmatch(purifier_id):
            raise ConfigError(f"unsafe air-purifier ID {purifier_id!r} in feature {index}")
        if purifier_id in seen:
            raise ConfigError(f"duplicate air-purifier ID {purifier_id!r}")
        seen.add(purifier_id)
        model_name = _required_text(properties, "model", f"air-purifier feature {purifier_id!r}")
        if model_name not in models:
            raise ConfigError(f"unknown air-purifier model {model_name!r} for {purifier_id}")
        model = models[model_name]
        height, height_source = _target_dimension(properties, "height_m", "HEIGHT_M", model.native_height_m, model.name)
        width, width_source = _target_dimension(properties, "width_m", "WIDTH_M", model.native_width_m, model.name)
        depth, depth_source = _target_dimension(properties, "depth_m", "DEPTH_M", model.native_depth_m, model.name)
        raw_rotation = properties.get("rotation_deg")
        if raw_rotation is None:
            rotation, rotation_source = 0.0, f"default:{model.name}"
        else:
            rotation, rotation_source = _finite_number(raw_rotation, f"rotation_deg for {purifier_id}") % 360.0, "attribute:ROTATION_D"
        projected_x, projected_y = _lonlat_to_epsg25832(lon, lat)
        local_x, local_y = projected_x - origin_x, projected_y - origin_y
        metadata_context = f"air-purifier feature {purifier_id!r}"
        input_id = _required_text(properties, "urban_planning_input_id", metadata_context)
        roi_zone = _required_choice(
            properties,
            "roi_zone",
            ("inner", "annular", "full"),
            metadata_context,
        )
        source = _required_text(properties, "source", metadata_context)
        source_crs = _required_choice(
            properties,
            "source_crs",
            ("EPSG:4326", "EPSG:3857"),
            metadata_context,
        )
        source_feature_index = _non_negative_integer(
            properties.get("source_feature_index"),
            f"{metadata_context} source_feature_index",
        )
        source_properties = properties.get("source_properties")
        if not isinstance(source_properties, dict):
            raise ConfigError(f"{metadata_context} source_properties must be an object")
        base_z = 0.0
        terrain_source = "z=0 fallback"
        if terrain_sampler is not None and terrain_path is not None:
            radians = math.radians(rotation)
            cosine, sine = math.cos(radians), math.sin(radians)
            for x_offset, y_offset in ((-width / 2, -depth / 2), (-width / 2, depth / 2), (width / 2, -depth / 2), (width / 2, depth / 2)):
                corner_x = local_x + x_offset * cosine - y_offset * sine
                corner_y = local_y + x_offset * sine + y_offset * cosine
                try:
                    terrain_sampler(corner_x, corner_y)
                except ConfigError as exc:
                    raise ConfigError(
                        f"air-purifier footprint for {purifier_id!r} could not be projected onto terrain: {exc}"
                    ) from exc
            base_z = terrain_sampler(local_x, local_y) - TERRAIN_CLEARANCE_M
            terrain_source = str(terrain_path)
        instances.append(AirPurifierInstance(
            purifier_id=purifier_id, model_name=model_name, source_lon=lon, source_lat=lat,
            projected_x=projected_x, projected_y=projected_y,
            local_x=local_x, local_y=local_y, base_z=base_z,
            target_width_m=width, target_depth_m=depth, target_height_m=height,
            native_width_m=model.native_width_m, native_depth_m=model.native_depth_m, native_height_m=model.native_height_m,
            scale_x=width / model.native_width_m, scale_y=depth / model.native_depth_m, scale_z=height / model.native_height_m,
            rotation_deg=rotation, width_source=width_source, depth_source=depth_source, height_source=height_source,
            rotation_source=rotation_source, terrain_source=terrain_source,
            input_id=input_id, source=source, source_crs=source_crs,
            source_feature_index=source_feature_index, roi_zone=roi_zone,
            source_properties=dict(source_properties),
        ))
    return instances


def _placement_payload(instances: list[AirPurifierInstance]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "name": "air_purifier_placements",
        "crs": {"type": "name", "properties": {"name": "EPSG:25832"}},
        "features": [
            {
                "type": "Feature", "geometry": {"type": "Point", "coordinates": [item.projected_x, item.projected_y]},
                "properties": {
                    "purifier_id": item.purifier_id, "model": item.model_name,
                    "source_coordinates": [item.source_lon, item.source_lat],
                    "source_coordinate_crs": "EPSG:4326",
                    "local_x": item.local_x, "local_y": item.local_y, "base_z": item.base_z,
                    "height_m": item.target_height_m, "width_m": item.target_width_m, "depth_m": item.target_depth_m,
                    "native_height_m": item.native_height_m, "native_width_m": item.native_width_m, "native_depth_m": item.native_depth_m,
                    "scale_x": item.scale_x, "scale_y": item.scale_y, "scale_z": item.scale_z,
                    "rotation_deg": item.rotation_deg, "height_source": item.height_source,
                    "width_source": item.width_source, "depth_source": item.depth_source,
                    "rotation_source": item.rotation_source, "terrain_source": item.terrain_source,
                    "urban_planning_input_id": item.input_id,
                    "source": item.source, "source_crs": item.source_crs,
                    "source_feature_index": item.source_feature_index,
                    "roi_zone": item.roi_zone, "source_properties": item.source_properties,
                },
            }
            for item in instances
        ],
    }


def _render_preview(
    instances: list[AirPurifierInstance],
    instance_meshes: dict[str, RegionMesh],
    origin_x: float,
    origin_y: float,
) -> str:
    points = [
        point
        for item in instances
        for region in REGION_NAMES
        for triangle in instance_meshes[item.purifier_id][region]
        for point in triangle
    ]
    bounds = (
        min(point[0] for point in points), max(point[0] for point in points),
        min(point[1] for point in points), max(point[1] for point in points),
        min(point[2] for point in points), max(point[2] for point in points),
    )
    centre = [
        (bounds[0] + bounds[1]) / 2.0,
        (bounds[2] + bounds[3]) / 2.0,
        (bounds[4] + bounds[5]) / 2.0,
    ]
    radius = max(
        math.sqrt(sum((point[axis] - centre[axis]) ** 2 for axis in range(3)))
        for point in points
    )
    radius = max(radius, 1e-6)
    preview_payload = {
        "schema_version": 1,
        "patch_colours": {"inlet": "#2f80ed", "outlet": "#eb5757", "tower": "#b9c1c9"},
        "scene": {
            "bounds": list(bounds),
            "centre": centre,
            "radius": radius,
            "default_scale": 620.0 * 0.44 / radius,
            "default_yaw": 0.65,
            "default_pitch": 0.55,
        },
        "instances": [
            {
                "id": item.purifier_id,
                "model": item.model_name,
                "height": item.target_height_m,
                "rotation": item.rotation_deg,
                "label_anchor": [item.local_x, item.local_y, item.base_z + item.target_height_m],
                "regions": {
                    region: instance_meshes[item.purifier_id][region]
                    for region in REGION_NAMES
                },
            }
            for item in instances
        ],
    }
    data = (
        json.dumps(preview_payload, sort_keys=True, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    model_controls = "".join(
        f'<label><input type="checkbox" checked data-model="{escape(name)}"> {escape(name)}</label>'
        for name in sorted({item.model_name for item in instances})
    )
    instance_controls = "".join(
        f'<label><input type="checkbox" checked data-instance="{escape(item.purifier_id)}"> {escape(item.purifier_id)}</label>'
        for item in instances
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Air-purifier models preview</title>
<style>body{{font-family:system-ui;margin:1.5rem;color:#243447}}canvas{{border:1px solid #b9c1c9;width:min(100%,1000px);height:620px;background:#f8fafc}}label{{margin-right:1rem}}.swatch{{display:inline-block;width:.9rem;height:.9rem}}.controls{{display:flex;flex-wrap:wrap;gap:.35rem 1rem}}</style></head>
<body><h1>Air-purifier models preview</h1>
<p>Offline local-coordinate preview. Local origin EPSG:25832: easting {origin_x:.3f}, northing {origin_y:.3f}.</p>
<p><span class="swatch" style="background:#2f80ed"></span> inlet &nbsp; <span class="swatch" style="background:#eb5757"></span> outlet &nbsp; <span class="swatch" style="background:#b9c1c9"></span> tower</p>
<div><button id="orbit">Orbit</button> <button id="zoomIn">Zoom +</button> <button id="zoomOut">Zoom -</button> <button id="reset">Reset</button></div>
<h2>Models</h2><div class="controls">{model_controls}</div><h2>Instances</h2><div class="controls">{instance_controls}</div>
<canvas id="scene" width="1000" height="620"></canvas>
<script id="preview-data" type="application/json">{data}</script>
<script>
const preview=JSON.parse(document.getElementById('preview-data').textContent);
const canvas=document.getElementById('scene'),ctx=canvas.getContext('2d');
const camera={{yaw:0,pitch:0,scale:1}};
function resetView(){{camera.yaw=preview.scene.default_yaw;camera.pitch=preview.scene.default_pitch;camera.scale=preview.scene.default_scale;draw()}}
function visible(i){{return document.querySelector(`[data-model="${{i.model}}"]`).checked&&document.querySelector(`[data-instance="${{i.id}}"]`).checked}}
function project(point){{
  const dx=point[0]-preview.scene.centre[0],dy=point[1]-preview.scene.centre[1],dz=point[2]-preview.scene.centre[2];
  const rx=dx*Math.cos(camera.yaw)-dy*Math.sin(camera.yaw),ry=dx*Math.sin(camera.yaw)+dy*Math.cos(camera.yaw);
  return [canvas.width/2+rx*camera.scale,canvas.height/2-(dz*Math.cos(camera.pitch)-ry*Math.sin(camera.pitch))*camera.scale,ry*Math.cos(camera.pitch)+dz*Math.sin(camera.pitch)];
}}
function draw(){{
  ctx.clearRect(0,0,canvas.width,canvas.height);const faces=[];
  for(const instance of preview.instances.filter(visible)){{for(const region of ['inlet','outlet','tower']){{for(const triangle of instance.regions[region]){{const projected=triangle.map(project);faces.push({{region,points:projected,depth:projected.reduce((sum,p)=>sum+p[2],0)/3}})}}}}}}
  faces.sort((a,b)=>a.depth-b.depth);
  for(const face of faces){{ctx.beginPath();ctx.moveTo(face.points[0][0],face.points[0][1]);ctx.lineTo(face.points[1][0],face.points[1][1]);ctx.lineTo(face.points[2][0],face.points[2][1]);ctx.closePath();ctx.fillStyle=preview.patch_colours[face.region];ctx.fill();ctx.strokeStyle='rgba(36,52,71,.18)';ctx.stroke()}}
  ctx.fillStyle='#17202a';ctx.font='12px system-ui';for(const instance of preview.instances.filter(visible)){{const label=project(instance.label_anchor);ctx.fillText(instance.id,label[0]+5,label[1]-5)}}
}}
document.querySelectorAll('input').forEach(x=>x.addEventListener('change',draw));
orbit.onclick=()=>{{camera.yaw+=Math.PI/8;draw()}};zoomIn.onclick=()=>{{camera.scale*=1.2;draw()}};zoomOut.onclick=()=>{{camera.scale/=1.2;draw()}};reset.onclick=resetView;
canvas.addEventListener('wheel',e=>{{e.preventDefault();camera.scale*=e.deltaY<0?1.1:.9;draw()}},{{passive:false}});
let dragging=false,lastX=0,lastY=0;canvas.addEventListener('pointerdown',e=>{{dragging=true;lastX=e.clientX;lastY=e.clientY;canvas.setPointerCapture(e.pointerId)}});canvas.addEventListener('pointermove',e=>{{if(!dragging)return;camera.yaw+=(e.clientX-lastX)*.008;camera.pitch=Math.max(-1.3,Math.min(1.3,camera.pitch+(e.clientY-lastY)*.008));lastX=e.clientX;lastY=e.clientY;draw()}});canvas.addEventListener('pointerup',()=>{{dragging=false}});
resetView();
</script></body></html>"""


def _render_report(
    source: Path, catalog: Path, terrain: Path | None, origin_x: float, origin_y: float,
    instances: list[AirPurifierInstance], model_counts: dict[str, int],
    input_counts: dict[str, int], parameter_source_counts: dict[str, dict[str, int]], placement: Path,
    combined: Path, instance_paths: dict[str, Path], preview: Path, manifest: Path,
) -> str:
    model_lines = _report_counts(model_counts)
    input_lines = _report_counts(input_counts)
    parameter_lines = "\n\n".join(
        f"### {field.removesuffix('_source').replace('_', ' ').title()}\n\n{_report_counts(counts)}"
        for field, counts in parameter_source_counts.items()
    )
    instance_lines = "\n".join(f"- `{key}`: `{value}`" for key, value in sorted(instance_paths.items()))
    terrain_text = f"`{terrain}` with {TERRAIN_CLEARANCE_M:.2f} m base clearance" if terrain else "unresolved; bases use z=0"
    return f"""# Air-purifier models report

## Inputs

- Normalized features: `{source}`
- Model catalog: `{catalog}`
- Terrain: {terrain_text}
- Local origin: EPSG:25832 ({origin_x:.3f}, {origin_y:.3f})

## Transformations and validation

Generated {len(instances)} purifier units. Target height, width, and depth are resolved independently from normalized attributes or catalog defaults. Source meshes are base-centred, anisotropically scaled, rotated counter-clockwise around +Z, and translated into the City4CFD local frame. When terrain is configured, all four rotated footprint corners are checked before the centre elevation is sampled.

### Counts by model

{model_lines}

### Counts by input

{input_lines}

## Parameter provenance

{parameter_lines}

## Outputs

- Placements: `{placement}`
- Aggregate surface: `{combined}`
- Offline preview: `{preview}`
- Completion manifest: `{manifest}`

{instance_lines}

## Limitations

The surfaces preserve the exact `inlet`, `outlet`, and `tower` exterior patch regions. They do not model internal ducts, fans, filters, or purifier performance.
"""


def _prior_instance_allowlist(manifest_path: Path, instances_dir: Path) -> set[Path]:
    if not manifest_path.exists():
        return set()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw = payload["outputs"]["instances"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return set()
    if not isinstance(raw, dict):
        return set()
    parent = instances_dir.resolve()
    allowed: set[Path] = set()
    for purifier_id, value in raw.items():
        if not isinstance(purifier_id, str) or not PURIFIER_ID_PATTERN.fullmatch(purifier_id) or not isinstance(value, str):
            continue
        path = Path(value).resolve()
        if path.parent == parent and path.name == f"{purifier_id}.stl":
            allowed.add(path)
    return allowed


def _target_dimension(properties: dict[str, Any], key: str, field: str, default: float, model_name: str) -> tuple[float, str]:
    value = properties.get(key)
    if value is None:
        return default, f"default:{model_name}"
    return _positive_number(value, key), f"attribute:{field}"


def _required_text(payload: dict[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} requires non-empty {key}")
    return value.strip()


def _required_choice(
    payload: dict[str, Any],
    key: str,
    allowed: tuple[str, ...],
    context: str,
) -> str:
    value = _required_text(payload, key, context)
    if value not in allowed:
        choices = ", ".join(allowed)
        raise ConfigError(f"{context} {key} must be one of {choices}; got {value!r}")
    return value


def _non_negative_integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{context} must be a non-negative integer")
    return value


def _finite_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{context} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{context} must be a finite number")
    return result


def _positive_number(value: Any, context: str) -> float:
    result = _finite_number(value, context)
    if result <= 0.0:
        raise ConfigError(f"{context} must be positive")
    return result


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items()))


def _report_counts(counts: dict[str, int]) -> str:
    return "\n".join(f"- `{name}`: {count}" for name, count in sorted(counts.items()))


def _lonlat_to_epsg25832(lon: float, lat: float) -> tuple[float, float]:
    semi_major = 6378137.0
    flattening = 1 / 298.257223563
    eccentricity_sq = flattening * (2 - flattening)
    lat_rad, lon_rad = math.radians(lat), math.radians(lon)
    lon0, k0, false_easting = math.radians(9.0), 0.9996, 500000.0
    n = semi_major / math.sqrt(1 - eccentricity_sq * math.sin(lat_rad) ** 2)
    t = math.tan(lat_rad) ** 2
    c = (eccentricity_sq / (1 - eccentricity_sq)) * math.cos(lat_rad) ** 2
    a = (lon_rad - lon0) * math.cos(lat_rad)
    m = semi_major * (
        (1 - eccentricity_sq / 4 - 3 * eccentricity_sq**2 / 64 - 5 * eccentricity_sq**3 / 256) * lat_rad
        - (3 * eccentricity_sq / 8 + 3 * eccentricity_sq**2 / 32 + 45 * eccentricity_sq**3 / 1024) * math.sin(2 * lat_rad)
        + (15 * eccentricity_sq**2 / 256 + 45 * eccentricity_sq**3 / 1024) * math.sin(4 * lat_rad)
        - (35 * eccentricity_sq**3 / 3072) * math.sin(6 * lat_rad)
    )
    easting = false_easting + k0 * n * (a + (1 - t + c) * a**3 / 6 + (5 - 18*t + t*t + 72*c - 58*eccentricity_sq) * a**5 / 120)
    northing = k0 * (m + n * math.tan(lat_rad) * (a*a/2 + (5 - t + 9*c + 4*c*c) * a**4/24 + (61 - 58*t + t*t + 600*c - 330*eccentricity_sq) * a**6/720))
    return easting, northing
