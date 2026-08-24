"""Parametric tree model generation for the fourth geometry module."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from cities_reconstruction.config import AppConfig, ConfigError
from cities_reconstruction.geometry.terrain import (
    load_terrain_sampler,
    validate_completed_city_models_terrain,
)
from cities_reconstruction.stage_contract import (
    ArtifactKind,
    ArtifactReference,
    JsonValue,
    StageManifest,
    StageStatus,
    invalidate_stage_manifests,
    require_completed_manifest,
    require_manifest_artifact,
)
from cities_reconstruction.stage_layout import StageId, stage_output_directory
from cities_reconstruction.stage_result import StageResult
from cities_reconstruction.stages.trees.diagnostics import species_counts as _species_counts
from cities_reconstruction.stages.trees.geometry import (
    Point3,
    Triangle,
)
from cities_reconstruction.stages.trees.geometry import (
    build_tree_instances as _build_tree_instances,
)
from cities_reconstruction.stages.trees.geometry import (
    crown_triangles as _crown_triangles,
)
from cities_reconstruction.stages.trees.geometry import (
    lonlat_to_epsg25832 as _lonlat_to_epsg25832,
)
from cities_reconstruction.stages.trees.geometry import (
    translate_triangles as _translate_triangles,
)
from cities_reconstruction.stages.trees.geometry import (
    trunk_triangles as _trunk_triangles,
)
from cities_reconstruction.stages.trees.inputs import read_feature_collection as _read_feature_collection
from cities_reconstruction.stages.trees.models import TreeInstance
from cities_reconstruction.stages.trees.publication import (
    TreesPublicationInput,
    publish_trees_manifest,
)
from cities_reconstruction.stages.trees.publication import (
    library_payload as _library_payload,
)
from cities_reconstruction.stages.trees.publication import (
    placement_geojson as _placement_geojson,
)
from cities_reconstruction.stages.trees.rendering import (
    render_preview as _render_preview,
)
from cities_reconstruction.stages.trees.reporting import render_report as _render_report


@dataclass(frozen=True)
class TreesStageOutput:
    manifest: StageManifest
    placement_geojson_path: Path
    library_path: Path
    surfaces_directory: Path
    trunks_stl_path: Path
    crowns_stl_path: Path
    combined_stl_path: Path
    tree_count: int
    species_counts: dict[str, int]

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


STAGE_ID = StageId.TREES


def plan(config: AppConfig) -> StageResult:
    output = stage_output_directory(config.output.root_directory, STAGE_ID)
    return StageResult(
        stage=STAGE_ID.value,
        summary="Generate parametric tree STL models from retrieved tree features.",
        planned_actions=(
            f"Use {config.trees.default} as the configured fallback species for tree features without species tags.",
            "Read retrieved OSM tree features from module 1.",
            "Project tree placements to the configured EPSG:25832 metric CRS.",
            "Optionally project tree bases onto a supplied city-models terrain geometry file so trunk bases sit just below the local terrain surface.",
            "Resolve species through the configured species/category mapping and scale category models with available tree height/diameter tags.",
            "Write trunk, crown, and combined STL surfaces plus an interactive HTML QA preview.",
        ),
        expected_outputs=(output,),
    )


def run(config: AppConfig) -> TreesStageOutput:
    """Generate deterministic parametric tree meshes from stage-1 tree features."""

    output_dir = stage_output_directory(config.output.root_directory, STAGE_ID)
    output_dir.mkdir(parents=True, exist_ok=True)
    invalidate_stage_manifests(
        output_dir,
        legacy_names=("tree_models_manifest.json",),
    )
    if config.region.crs != "EPSG:25832":
        raise ConfigError("tree model generation currently supports EPSG:25832 output coordinates")

    stage1_manifest = require_completed_manifest(
        stage_output_directory(config.output.root_directory, StageId.SHAPEFILES) / "manifest.json",
        expected_stage=StageId.SHAPEFILES.value,
    )
    tree_features_path = require_manifest_artifact(
        stage1_manifest,
        name="category-trees",
        kind=ArtifactKind.HANDOFF,
    ).path

    features = _read_feature_collection(tree_features_path)
    surfaces_dir = output_dir / "surfaces"
    surfaces_dir.mkdir(parents=True, exist_ok=True)

    placement_path = output_dir / "tree_placements.geojson"
    library_path = output_dir / "tree_species_library.json"
    manifest_path = output_dir / "manifest.json"
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
    manifest = publish_trees_manifest(
        TreesPublicationInput(
            config=config,
            output_directory=output_dir,
            tree_features_path=tree_features_path,
            placement_path=placement_path,
            library_path=library_path,
            report_path=report_path,
            preview_path=preview_path,
            trunks_stl_path=trunks_stl_path,
            crowns_stl_path=crowns_stl_path,
            combined_stl_path=combined_stl_path,
            species_crown_paths=species_crown_paths,
            instances=instances,
            species_counts=species_counts,
            surface_origin_x=surface_origin_x,
            surface_origin_y=surface_origin_y,
            terrain_geometry_path=terrain_geometry_path,
        )
    )

    return TreesStageOutput(
        manifest=manifest,
        placement_geojson_path=placement_path,
        library_path=library_path,
        surfaces_directory=surfaces_dir,
        trunks_stl_path=trunks_stl_path,
        crowns_stl_path=crowns_stl_path,
        combined_stl_path=combined_stl_path,
        tree_count=len(instances),
        species_counts=species_counts,
    )


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
    slugs = _unique_species_slugs(grouped)
    paths: dict[str, Path] = {}
    for species, triangles in sorted(grouped.items()):
        slug = slugs[species]
        path = species_crowns_dir / f"{slug}_crowns.stl"
        local_triangles = _translate_triangles(triangles, -surface_origin_x, -surface_origin_y, 0.0)
        _write_stl(path, slug, local_triangles)
        paths[species] = path
    return paths


def _unique_species_slugs(species_names: Iterable[str]) -> dict[str, str]:
    names = sorted(set(species_names))
    grouped: dict[str, list[str]] = {}
    for name in names:
        grouped.setdefault(_slug(name), []).append(name)

    reserved = set(grouped)
    used: set[str] = set()
    result: dict[str, str] = {}
    for base_slug, colliding_names in sorted(grouped.items()):
        for index, name in enumerate(colliding_names):
            if index == 0:
                candidate = base_slug
            else:
                digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
                candidate = ""
                for length in range(8, len(digest) + 1):
                    proposed = f"{base_slug}_{digest[:length]}"
                    if proposed not in reserved and proposed not in used:
                        candidate = proposed
                        break
                if not candidate:
                    suffix = 2
                    candidate = f"{base_slug}_{digest}_{suffix}"
                    while candidate in reserved or candidate in used:
                        suffix += 1
                        candidate = f"{base_slug}_{digest}_{suffix}"
            used.add(candidate)
            result[name] = candidate
    return result


def _slug(value: str) -> str:
    normalized = []
    for character in value.lower():
        if character.isalnum():
            normalized.append(character)
        elif normalized and normalized[-1] != "_":
            normalized.append("_")
    return "".join(normalized).strip("_") or "unknown_species"


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
