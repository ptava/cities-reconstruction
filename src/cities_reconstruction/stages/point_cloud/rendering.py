"""HTML and scene-data rendering for the point-cloud preparation stage."""

from __future__ import annotations

import json
import math
from html import escape
from typing import TYPE_CHECKING, Any

from cities_reconstruction.config import AppConfig

if TYPE_CHECKING:
    from .stage import ProjectedPolygon


def render_preview_html(
    config: AppConfig,
    building_polygons: list[ProjectedPolygon],
    ground_points: list[tuple[float, float, float]],
    building_points: list[tuple[float, float, float]],
    tree_points: list[tuple[float, float, float]],
    unclassified_points: list[tuple[float, float, float]],
    diagnostics: dict[str, Any],
    *,
    projected_bbox: tuple[float, float, float, float],
    tree_building_footprint_buffer_m: float,
    tree_roof_offset_threshold_m: float,
    tree_roof_search_radius_m: float,
) -> str:
    scene = point_cloud_scene_data(
        config,
        building_polygons,
        ground_points,
        building_points,
        tree_points,
        unclassified_points,
        projected_bbox=projected_bbox,
    )
    scene_json = json.dumps(scene, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{escape(config.region.name)} point-cloud alignment</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; color: #1f2933; background: #f8fafc; }}
    canvas {{ display: block; width: min(1080px, 100%); height: min(72vh, 760px); border: 1px solid #c8d1dc; background: #ffffff; }}
    .note {{ max-width: 1080px; color: #52606d; line-height: 1.35; }}
    .legend {{ display: flex; gap: 1rem; flex-wrap: wrap; margin: 0.8rem 0; color: #334155; }}
    .swatch {{ display: inline-block; width: 0.9rem; height: 0.9rem; margin-right: 0.35rem; vertical-align: -0.12rem; }}
    .zoom-controls {{ display: flex; gap: 0.45rem; flex-wrap: wrap; margin: 0.7rem 0; }}
    .zoom-controls button {{ border: 1px solid #94a3b8; background: #ffffff; color: #1f2933; padding: 0.35rem 0.65rem; border-radius: 0.35rem; cursor: pointer; }}
    .zoom-controls button:hover {{ background: #e2e8f0; }}
    .load-control-group {{ margin: 0.7rem 0 0.2rem 0; }}
    .load-control-group strong {{ display: inline-block; min-width: 10rem; margin-right: 0.4rem; color: #334155; }}
    .point-load-controls {{ display: inline-flex; gap: 0.45rem; flex-wrap: wrap; }}
    .point-load-controls button {{ border: 1px solid #94a3b8; background: #ffffff; color: #1f2933; padding: 0.35rem 0.65rem; border-radius: 0.35rem; cursor: pointer; }}
    .point-load-controls button:hover, .point-load-controls button.active {{ background: #dbeafe; border-color: #2563eb; }}
    .cloud-visibility-toggle {{ margin-left: 0.45rem; border: 1px solid #94a3b8; background: #ffffff; color: #1f2933; padding: 0.35rem 0.65rem; border-radius: 0.35rem; cursor: pointer; }}
    .cloud-visibility-toggle:hover {{ background: #e2e8f0; }}
    .cloud-visibility-toggle:focus-visible {{ outline: 3px solid #93c5fd; outline-offset: 2px; }}
  </style>
</head>
<body>
  <h1>{escape(config.region.name)} point-cloud alignment 3D</h1>
  <h2>Terrain, Buildings, And Trees</h2>
  <div class="zoom-controls" aria-label="Point-cloud preview zoom controls">
    <button type="button" data-view-index="0" data-zoom-in>Zoom in</button>
    <button type="button" data-view-index="0" data-zoom-out>Zoom out</button>
    <button type="button" data-view-index="0" data-zoom-reset>Reset zoom</button>
  </div>
  <div class="load-control-group">
    <strong>Terrain load</strong>
    <span class="point-load-controls" id="terrainLoadControls" aria-label="Terrain sample density controls"></span>
    <button type="button" class="cloud-visibility-toggle" id="terrainCloudVisibilityToggle" aria-pressed="true">Hide terrain cloud</button>
  </div>
  <div class="load-control-group">
    <strong>Buildings cloud load</strong>
    <span class="point-load-controls" id="buildingsCloudLoadControls" aria-label="Buildings cloud sample density controls"></span>
    <button type="button" class="cloud-visibility-toggle" id="buildingsCloudVisibilityToggle" aria-pressed="true">Hide buildings cloud</button>
  </div>
  <div class="load-control-group">
    <strong>Unclassified cloud load</strong>
    <span class="point-load-controls" id="unclassifiedCloudLoadControls" aria-label="Unclassified cloud sample density controls"></span>
    <button type="button" class="cloud-visibility-toggle" id="unclassifiedCloudVisibilityToggle" aria-pressed="true">Hide unclassified cloud</button>
  </div>
  <canvas id="scene" width="1400" height="900" aria-label="3D point-cloud alignment preview"></canvas>
  <div class="legend">
    <span><span class="swatch" style="background:#16a34a"></span>sampled ground cloud</span>
    <span><span class="swatch" style="background:#2563eb"></span>sampled building cloud</span>
    <span><span class="swatch" style="background:#dc2626"></span>filtered tree DSM points</span>
    <span><span class="swatch" style="background:#7c3aed"></span>sampled unclassified DSM cloud</span>
    <span><span class="swatch" style="background:#b45309"></span>projected footprints on local terrain</span>
  </div>
  <p class="note">Drag to rotate the 3D view. Use the mouse wheel or zoom buttons to zoom in and out. Use the terrain-load buttons to control sampled DTM terrain points, the buildings-cloud buttons to control sampled building and filtered tree points, and the unclassified-cloud buttons to control unclassified DSM points independently. Green points are voxel-grid subsampled DTM ground points, blue points are voxel-grid subsampled DSM building points, red points are DSM cells filtered as trees, purple points are valid DSM points not classified as buildings or trees, and brown outlines are projected footprints placed on the nearest local ground elevation. Tree candidates come from vegetation-colored overlay pixels or nearby stage-1 natural=tree tags. If a candidate is inside a building footprint or within {tree_building_footprint_buffer_m:g} m of one, it enters the tree cloud only when the candidate DSM Z differs from estimated nearby roof Z by at least {tree_roof_offset_threshold_m:g} m inside a {tree_roof_search_radius_m:g} m XY search radius. Local DSM relief fallback is used only outside the buffered building-footprint zone. The preview uses the same meter-scale height differences as the exported PLY files and does not exaggerate vertical scale. Alignment status: {escape(str(diagnostics["alignment_status"]))}; estimated horizontal shift: {diagnostics["estimated_horizontal_shift_m"]} m.</p>
  <h2>Buildings And Footprints</h2>
  <div class="zoom-controls" aria-label="Building point-cloud preview zoom controls">
    <button type="button" data-view-index="1" data-zoom-in>Zoom in</button>
    <button type="button" data-view-index="1" data-zoom-out>Zoom out</button>
    <button type="button" data-view-index="1" data-zoom-reset>Reset zoom</button>
  </div>
  <div class="load-control-group">
    <strong>Building load</strong>
    <span class="point-load-controls" id="buildingLoadControls" aria-label="Building point-cloud sample density controls"></span>
  </div>
  <canvas id="buildingScene" width="1400" height="900" aria-label="3D building point-cloud and footprint preview"></canvas>
  <div class="legend">
    <span><span class="swatch" style="background:#2563eb"></span>sampled building cloud</span>
    <span><span class="swatch" style="background:#b45309"></span>projected footprints on local terrain</span>
  </div>
  <p class="note">This plot isolates the City4CFD building handoff: DSM points classified as buildings are shown with the projected footprint rings and no terrain or tree points.</p>
  <script>
    const scene = {scene_json};
    const views = [
      {{ canvas: document.getElementById("scene"), mode: "all", yaw: -0.7, pitch: 0.85, zoom: 1.0, activeTerrainSampleIndex: scene.defaultTerrainSampleLevelIndex, activeBuildingsCloudSampleIndex: scene.defaultBuildingsCloudSampleLevelIndex, activeUnclassifiedCloudSampleIndex: scene.defaultUnclassifiedCloudSampleLevelIndex, showTerrainCloud: true, showBuildingsCloud: true, showUnclassifiedCloud: true, dragging: false, last: null }},
      {{ canvas: document.getElementById("buildingScene"), mode: "buildings", yaw: -0.7, pitch: 0.85, zoom: 1.0, activeTerrainSampleIndex: scene.defaultTerrainSampleLevelIndex, activeBuildingsCloudSampleIndex: scene.defaultBuildingsCloudSampleLevelIndex, dragging: false, last: null }},
    ];

    function activeTerrainSamples(view) {{
      return scene.terrainSampleLevels[view.activeTerrainSampleIndex];
    }}

    function activeBuildingsCloudSamples(view) {{
      return scene.buildingsCloudSampleLevels[view.activeBuildingsCloudSampleIndex];
    }}

    function activeUnclassifiedCloudSamples(view) {{
      return scene.unclassifiedCloudSampleLevels[view.activeUnclassifiedCloudSampleIndex];
    }}

    function resize(view) {{
      const canvas = view.canvas;
      const ratio = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(640, Math.round(rect.width * ratio));
      canvas.height = Math.max(420, Math.round(rect.height * ratio));
      draw(view);
    }}

    function rotate(view, point) {{
      const [x, y, z] = point;
      const cy = Math.cos(view.yaw), sy = Math.sin(view.yaw);
      const cp = Math.cos(view.pitch), sp = Math.sin(view.pitch);
      const rx = x * cy - y * sy;
      const ry = x * sy + y * cy;
      const rz = z;
      return [rx, ry * cp + rz * sp, ry * sp - rz * cp];
    }}

    function project(view, point) {{
      const canvas = view.canvas;
      const [x, y, z] = rotate(view, point);
      const scale = Math.min(canvas.width, canvas.height) * 0.42 / scene.extent * view.zoom;
      return [canvas.width / 2 + x * scale, canvas.height * 0.58 - y * scale, z];
    }}

    function setZoom(view, nextZoom) {{
      view.zoom = Math.max(0.35, Math.min(5.0, nextZoom));
      draw(view);
    }}

    function drawLine(view, a, b, color, width) {{
      const ctx = view.canvas.getContext("2d");
      const pa = project(view, a), pb = project(view, b);
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.beginPath();
      ctx.moveTo(pa[0], pa[1]);
      ctx.lineTo(pb[0], pb[1]);
      ctx.stroke();
    }}

    function draw(view) {{
      const canvas = view.canvas;
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.strokeStyle = "#e2e8f0";
      ctx.lineWidth = 1;
      for (let i = -4; i <= 4; i++) {{
        drawLine(view, [-scene.extent, i * scene.extent / 4, 0], [scene.extent, i * scene.extent / 4, 0], "#e2e8f0", 1);
        drawLine(view, [i * scene.extent / 4, -scene.extent, 0], [i * scene.extent / 4, scene.extent, 0], "#e2e8f0", 1);
      }}
      for (const ring of scene.footprintRings) {{
        const color = ring.role === "hole" ? "#d97706" : "#92400e";
        const width = ring.role === "hole" ? 1.5 : 2;
        for (let i = 1; i < ring.points.length; i++) drawLine(view, ring.points[i - 1], ring.points[i], color, width);
      }}
      const terrainSamples = activeTerrainSamples(view);
      const buildingsCloudSamples = activeBuildingsCloudSamples(view);
      const unclassifiedCloudSamples = view.mode === "all" ? activeUnclassifiedCloudSamples(view) : null;
      const pointSources = view.mode === "buildings"
        ? buildingsCloudSamples.buildingPoints.map((point) => [point, "#2563eb", 2.1, 0.78])
        : [
            ...(view.showTerrainCloud ? terrainSamples.groundPoints.map((point) => [point, "#16a34a", 1.6, 0.42]) : []),
            ...(view.showBuildingsCloud ? buildingsCloudSamples.buildingPoints.map((point) => [point, "#2563eb", 2.0, 0.72]) : []),
            ...(view.showBuildingsCloud ? buildingsCloudSamples.treePoints.map((point) => [point, "#dc2626", 2.4, 0.82]) : []),
            ...(view.showUnclassifiedCloud ? unclassifiedCloudSamples.unclassifiedPoints.map((point) => [point, "#7c3aed", 1.8, 0.58]) : []),
          ];
      const pointLayers = pointSources.map(([point, color, radius, alpha]) => [project(view, point), color, radius, alpha, point[2]]).sort((a, b) => a[0][2] - b[0][2]);
      for (const [point, color, radius, alpha, z] of pointLayers) {{
        const shade = Math.max(0.25, Math.min(1, z / scene.maxZ));
        ctx.fillStyle = color.replace(")", `, ${{alpha + shade * 0.2}})`).replace("rgb", "rgba");
        ctx.beginPath();
        ctx.arc(point[0], point[1], radius, 0, Math.PI * 2);
        ctx.fill();
      }}
      ctx.fillStyle = "#334155";
      ctx.font = `${{Math.max(13, canvas.width / 95)}}px Arial`;
      if (view.mode === "buildings") {{
        ctx.fillText(`Building load: ${{buildingsCloudSamples.label}} (${{buildingsCloudSamples.buildingPoints.length}} loaded)`, 18, 28);
        ctx.fillText(`3D sampled building points: ${{buildingsCloudSamples.buildingPoints.length}} / ${{scene.totalBuildingPoints}}`, 18, 52);
        ctx.fillText(`Footprint rings: ${{scene.footprintRings.length}}`, 18, 76);
      }} else {{
        ctx.fillText(`Terrain load: ${{terrainSamples.label}} (${{terrainSamples.totalLoadedPoints}} loaded)`, 18, 28);
        ctx.fillText(`Buildings cloud load: ${{buildingsCloudSamples.label}} (${{buildingsCloudSamples.totalLoadedPoints}} loaded)`, 18, 52);
        ctx.fillText(`Unclassified cloud load: ${{unclassifiedCloudSamples.label}} (${{unclassifiedCloudSamples.totalLoadedPoints}} loaded)`, 18, 76);
        ctx.fillText(`3D sampled ground points: ${{terrainSamples.groundPoints.length}} / ${{scene.totalGroundPoints}}`, 18, 100);
        ctx.fillText(`3D sampled building points: ${{buildingsCloudSamples.buildingPoints.length}} / ${{scene.totalBuildingPoints}}`, 18, 124);
        ctx.fillText(`3D filtered tree points: ${{buildingsCloudSamples.treePoints.length}} / ${{scene.totalTreePoints}}`, 18, 148);
        ctx.fillText(`3D sampled unclassified points: ${{unclassifiedCloudSamples.unclassifiedPoints.length}} / ${{scene.totalUnclassifiedPoints}}`, 18, 172);
        ctx.fillText(`Footprint rings: ${{scene.footprintRings.length}}`, 18, 196);
      }}
    }}

    function renderLoadControls(containerId, levels, activeIndex, onSelect) {{
      const container = document.getElementById(containerId);
      for (const [index, level] of levels.entries()) {{
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = `${{level.label}} (${{level.totalLoadedPoints}} pts)`;
        button.addEventListener("click", () => {{
          onSelect(index);
          for (const item of container.querySelectorAll("button")) item.classList.remove("active");
          button.classList.add("active");
          for (const view of views) draw(view);
        }});
        if (index === activeIndex) button.classList.add("active");
        container.appendChild(button);
      }}
    }}

    function bindCloudVisibilityToggle(buttonId, view, stateKey, cloudLabel) {{
      const button = document.getElementById(buttonId);

      function updateButton() {{
        const visible = view[stateKey];
        button.textContent = `${{visible ? "Hide" : "Show"}} ${{cloudLabel}}`;
        button.setAttribute("aria-pressed", String(visible));
      }}

      button.addEventListener("click", () => {{
        view[stateKey] = !view[stateKey];
        updateButton();
        draw(view);
      }});
      updateButton();
    }}

    for (const view of views) {{
      view.canvas.addEventListener("pointerdown", (event) => {{ view.dragging = true; view.last = [event.clientX, event.clientY]; view.canvas.setPointerCapture(event.pointerId); }});
      view.canvas.addEventListener("pointermove", (event) => {{
        if (!view.dragging || !view.last) return;
        view.yaw += (event.clientX - view.last[0]) * 0.008;
        view.pitch = Math.max(0.15, Math.min(1.45, view.pitch + (event.clientY - view.last[1]) * 0.006));
        view.last = [event.clientX, event.clientY];
        draw(view);
      }});
      view.canvas.addEventListener("pointerup", () => {{ view.dragging = false; view.last = null; }});
      view.canvas.addEventListener("wheel", (event) => {{
        event.preventDefault();
        setZoom(view, view.zoom * (event.deltaY < 0 ? 1.12 : 0.88));
      }}, {{ passive: false }});
    }}
    for (const button of document.querySelectorAll("[data-zoom-in]")) {{
      button.addEventListener("click", () => {{
        const view = views[Number(button.dataset.viewIndex)];
        setZoom(view, view.zoom * 1.2);
      }});
    }}
    for (const button of document.querySelectorAll("[data-zoom-out]")) {{
      button.addEventListener("click", () => {{
        const view = views[Number(button.dataset.viewIndex)];
        setZoom(view, view.zoom / 1.2);
      }});
    }}
    for (const button of document.querySelectorAll("[data-zoom-reset]")) {{
      button.addEventListener("click", () => {{
        const view = views[Number(button.dataset.viewIndex)];
        setZoom(view, 1.0);
      }});
    }}
    window.addEventListener("resize", () => {{ for (const view of views) resize(view); }});
    renderLoadControls("terrainLoadControls", scene.terrainSampleLevels, views[0].activeTerrainSampleIndex, (index) => {{ views[0].activeTerrainSampleIndex = index; }});
    renderLoadControls("buildingsCloudLoadControls", scene.buildingsCloudSampleLevels, views[0].activeBuildingsCloudSampleIndex, (index) => {{ views[0].activeBuildingsCloudSampleIndex = index; }});
    renderLoadControls("unclassifiedCloudLoadControls", scene.unclassifiedCloudSampleLevels, views[0].activeUnclassifiedCloudSampleIndex, (index) => {{ views[0].activeUnclassifiedCloudSampleIndex = index; }});
    renderLoadControls("buildingLoadControls", scene.buildingsCloudSampleLevels, views[1].activeBuildingsCloudSampleIndex, (index) => {{ views[1].activeBuildingsCloudSampleIndex = index; }});
    bindCloudVisibilityToggle("terrainCloudVisibilityToggle", views[0], "showTerrainCloud", "terrain cloud");
    bindCloudVisibilityToggle("buildingsCloudVisibilityToggle", views[0], "showBuildingsCloud", "buildings cloud");
    bindCloudVisibilityToggle("unclassifiedCloudVisibilityToggle", views[0], "showUnclassifiedCloud", "unclassified cloud");
    for (const view of views) resize(view);
  </script>
</body>
</html>
"""


def point_cloud_scene_data(
    config: AppConfig,
    building_polygons: list[ProjectedPolygon],
    ground_points: list[tuple[float, float, float]],
    building_points: list[tuple[float, float, float]],
    tree_points: list[tuple[float, float, float]],
    unclassified_points: list[tuple[float, float, float]],
    *,
    projected_bbox: tuple[float, float, float, float],
) -> dict[str, Any]:
    min_x, min_y, max_x, max_y = projected_bbox
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    all_points = [*ground_points, *building_points, *tree_points, *unclassified_points]
    min_z = min((point[2] for point in all_points), default=0.0)
    max_z = max((point[2] for point in all_points), default=min_z + 1.0)
    terrain_sample_levels = _terrain_sample_levels(config, ground_points, center_x, center_y, min_z)
    buildings_cloud_sample_levels = _buildings_cloud_sample_levels(
        config,
        building_points,
        tree_points,
        center_x,
        center_y,
        min_z,
    )
    unclassified_cloud_sample_levels = _unclassified_cloud_sample_levels(
        config,
        unclassified_points,
        center_x,
        center_y,
        min_z,
    )
    ground_index = _ground_point_index(ground_points)
    default_ground_z = sum(point[2] for point in ground_points) / len(ground_points) if ground_points else min_z
    return {
        "extent": config.region.outer_diameter_m / 2.0,
        "maxZ": round(max_z - min_z, 3) or 1.0,
        "totalGroundPoints": len(ground_points),
        "totalBuildingPoints": len(building_points),
        "totalTreePoints": len(tree_points),
        "totalUnclassifiedPoints": len(unclassified_points),
        "terrainSampleLevels": terrain_sample_levels,
        "buildingsCloudSampleLevels": buildings_cloud_sample_levels,
        "unclassifiedCloudSampleLevels": unclassified_cloud_sample_levels,
        "defaultTerrainSampleLevelIndex": min(1, len(terrain_sample_levels) - 1),
        "defaultBuildingsCloudSampleLevelIndex": min(1, len(buildings_cloud_sample_levels) - 1),
        "defaultUnclassifiedCloudSampleLevelIndex": min(1, len(unclassified_cloud_sample_levels) - 1),
        "footprintRings": [
            {
                "role": role,
                "points": [
                    [
                        round(x - center_x, 3),
                        round(y - center_y, 3),
                        round(_nearest_ground_z(x, y, ground_index, default_ground_z) - min_z, 3),
                    ]
                    for x, y in ring
                ],
            }
            for polygon in building_polygons
            for role, ring in (("exterior", polygon.exterior), *(("hole", hole) for hole in polygon.holes))
        ],
    }


def _terrain_sample_levels(
    config: AppConfig,
    ground_points: list[tuple[float, float, float]],
    center_x: float,
    center_y: float,
    min_z: float,
) -> list[dict[str, Any]]:
    base_voxel_size_m = _preview_voxel_size_m(config)
    requested_levels = (
        ("Light", base_voxel_size_m * 2.0),
        ("Normal", base_voxel_size_m),
        ("Dense", max(1.0, base_voxel_size_m / 2.0)),
        ("Maximum", max(1.0, base_voxel_size_m / 4.0)),
    )
    unique_levels: list[tuple[str, float, float]] = []
    seen_voxel_sizes: set[float] = set()
    for label, voxel_size_m in requested_levels:
        rounded_voxel_size_m = round(voxel_size_m, 3)
        if rounded_voxel_size_m in seen_voxel_sizes:
            continue
        seen_voxel_sizes.add(rounded_voxel_size_m)
        unique_levels.append((label, voxel_size_m, rounded_voxel_size_m))
    sampled_levels = _voxel_grid_subsample_many(
        ground_points,
        [voxel_size_m for _label, voxel_size_m, _rounded_size in unique_levels],
    )
    levels: list[dict[str, Any]] = []
    for (label, _voxel_size_m, rounded_voxel_size_m), sample_ground_points in zip(
        unique_levels,
        sampled_levels,
        strict=True,
    ):
        levels.append(
            {
                "label": label,
                "voxelSizeM": rounded_voxel_size_m,
                "totalLoadedPoints": len(sample_ground_points),
                "groundPoints": _local_preview_points(sample_ground_points, center_x, center_y, min_z),
            }
        )
    return levels or [
        {
            "label": "Empty",
            "voxelSizeM": round(base_voxel_size_m, 3),
            "totalLoadedPoints": 0,
            "groundPoints": [],
        }
    ]


def _buildings_cloud_sample_levels(
    config: AppConfig,
    building_points: list[tuple[float, float, float]],
    tree_points: list[tuple[float, float, float]],
    center_x: float,
    center_y: float,
    min_z: float,
) -> list[dict[str, Any]]:
    base_voxel_size_m = _preview_voxel_size_m(config)
    requested_levels = (
        ("Light", base_voxel_size_m * 2.0),
        ("Normal", base_voxel_size_m),
        ("Dense", max(1.0, base_voxel_size_m / 2.0)),
        ("Maximum", max(1.0, base_voxel_size_m / 4.0)),
    )
    unique_levels: list[tuple[str, float, float]] = []
    seen_voxel_sizes: set[float] = set()
    for label, voxel_size_m in requested_levels:
        rounded_voxel_size_m = round(voxel_size_m, 3)
        if rounded_voxel_size_m in seen_voxel_sizes:
            continue
        seen_voxel_sizes.add(rounded_voxel_size_m)
        unique_levels.append((label, voxel_size_m, rounded_voxel_size_m))
    voxel_sizes = [voxel_size_m for _label, voxel_size_m, _rounded_size in unique_levels]
    sampled_building_levels = _voxel_grid_subsample_many(building_points, voxel_sizes)
    sampled_tree_levels = _voxel_grid_subsample_many(tree_points, voxel_sizes)
    levels: list[dict[str, Any]] = []
    for (
        (label, _voxel_size_m, rounded_voxel_size_m),
        sample_building_points,
        sample_tree_points,
    ) in zip(
        unique_levels,
        sampled_building_levels,
        sampled_tree_levels,
        strict=True,
    ):
        levels.append(
            {
                "label": label,
                "voxelSizeM": rounded_voxel_size_m,
                "totalLoadedPoints": len(sample_building_points) + len(sample_tree_points),
                "buildingPoints": _local_preview_points(sample_building_points, center_x, center_y, min_z),
                "treePoints": _local_preview_points(sample_tree_points, center_x, center_y, min_z),
            }
        )
    return levels or [
        {
            "label": "Empty",
            "voxelSizeM": round(base_voxel_size_m, 3),
            "totalLoadedPoints": 0,
            "buildingPoints": [],
            "treePoints": [],
        }
    ]


def _unclassified_cloud_sample_levels(
    config: AppConfig,
    unclassified_points: list[tuple[float, float, float]],
    center_x: float,
    center_y: float,
    min_z: float,
) -> list[dict[str, Any]]:
    base_voxel_size_m = _preview_voxel_size_m(config)
    requested_levels = (
        ("Light", base_voxel_size_m * 2.0),
        ("Normal", base_voxel_size_m),
        ("Dense", max(1.0, base_voxel_size_m / 2.0)),
        ("Maximum", max(1.0, base_voxel_size_m / 4.0)),
    )
    unique_levels: list[tuple[str, float, float]] = []
    seen_voxel_sizes: set[float] = set()
    for label, voxel_size_m in requested_levels:
        rounded_voxel_size_m = round(voxel_size_m, 3)
        if rounded_voxel_size_m in seen_voxel_sizes:
            continue
        seen_voxel_sizes.add(rounded_voxel_size_m)
        unique_levels.append((label, voxel_size_m, rounded_voxel_size_m))
    sampled_levels = _voxel_grid_subsample_many(
        unclassified_points,
        [voxel_size_m for _label, voxel_size_m, _rounded_size in unique_levels],
    )
    levels: list[dict[str, Any]] = []
    for (label, _voxel_size_m, rounded_voxel_size_m), sample_unclassified_points in zip(
        unique_levels,
        sampled_levels,
        strict=True,
    ):
        levels.append(
            {
                "label": label,
                "voxelSizeM": rounded_voxel_size_m,
                "totalLoadedPoints": len(sample_unclassified_points),
                "unclassifiedPoints": _local_preview_points(
                    sample_unclassified_points,
                    center_x,
                    center_y,
                    min_z,
                ),
            }
        )
    return levels or [
        {
            "label": "Empty",
            "voxelSizeM": round(base_voxel_size_m, 3),
            "totalLoadedPoints": 0,
            "unclassifiedPoints": [],
        }
    ]


def _local_preview_points(
    points: list[tuple[float, float, float]],
    center_x: float,
    center_y: float,
    min_z: float,
) -> list[list[float]]:
    return [
        [round(x - center_x, 3), round(y - center_y, 3), round(z - min_z, 3)]
        for x, y, z in points
    ]


def _preview_voxel_size_m(config: AppConfig) -> float:
    """Return the preview decimation voxel size in meters."""

    return max(2.0, config.region.outer_diameter_m / 50.0)


def _voxel_grid_subsample_many(
    points: list[tuple[float, float, float]],
    voxel_sizes_m: list[float],
) -> list[list[tuple[float, float, float]]]:
    """Subsample several voxel sizes in one traversal of the source points."""

    selected_levels: list[dict[tuple[int, int], tuple[tuple[float, float, float], float]]] = [
        {} for _voxel_size_m in voxel_sizes_m
    ]
    for point in points:
        x, y, _z = point
        for voxel_size_m, selected in zip(voxel_sizes_m, selected_levels, strict=True):
            key = (math.floor(x / voxel_size_m), math.floor(y / voxel_size_m))
            center_x = (key[0] + 0.5) * voxel_size_m
            center_y = (key[1] + 0.5) * voxel_size_m
            distance_sq = (x - center_x) ** 2 + (y - center_y) ** 2
            current = selected.get(key)
            if current is None or distance_sq < current[1] or (distance_sq == current[1] and point < current[0]):
                selected[key] = (point, distance_sq)

    return [
        [selected[key][0] for key in sorted(selected)]
        for selected in selected_levels
    ]


def _ground_point_index(points: list[tuple[float, float, float]]) -> dict[tuple[int, int], list[tuple[float, float, float]]]:
    index: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    cell_size = 2.0
    for x, y, z in points:
        key = (math.floor(x / cell_size), math.floor(y / cell_size))
        index.setdefault(key, []).append((x, y, z))
    return index


def _nearest_ground_z(
    x: float,
    y: float,
    index: dict[tuple[int, int], list[tuple[float, float, float]]],
    default_z: float,
) -> float:
    if not index:
        return default_z
    cell_size = 2.0
    key_x = math.floor(x / cell_size)
    key_y = math.floor(y / cell_size)
    best_distance = math.inf
    best_z = default_z
    for point_x, point_y, point_z in index.get((key_x, key_y), []):
        distance = (point_x - x) ** 2 + (point_y - y) ** 2
        if distance < best_distance:
            best_distance = distance
            best_z = point_z
    if best_distance < math.inf:
        return best_z
    for radius in range(1, 13):
        for delta_x in range(-radius, radius + 1):
            for delta_y in range(-radius, radius + 1):
                if abs(delta_x) != radius and abs(delta_y) != radius:
                    continue
                for point_x, point_y, point_z in index.get((key_x + delta_x, key_y + delta_y), []):
                    distance = (point_x - x) ** 2 + (point_y - y) ** 2
                    if distance < best_distance:
                        best_distance = distance
                        best_z = point_z
        if best_distance < math.inf:
            break
    return best_z
