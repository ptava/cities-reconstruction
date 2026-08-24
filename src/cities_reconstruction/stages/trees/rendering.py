"""Self-contained graphical feedback for parametric tree reconstruction."""

from __future__ import annotations

import json
from html import escape
from typing import Any

from cities_reconstruction.config import AppConfig
from cities_reconstruction.stages.trees.diagnostics import information_summary, species_counts
from cities_reconstruction.stages.trees.models import TreeInstance


def render_preview(
    config: AppConfig,
    instances: list[TreeInstance],
    surface_origin_x: float,
    surface_origin_y: float,
) -> str:
    scene_json = json.dumps(scene_data(instances, surface_origin_x, surface_origin_y), separators=(",", ":"))
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
  <p class="note">Drag to rotate the 3D tree preview. Use the mouse wheel or zoom buttons to zoom in and out. The placement GeoJSON stays in projected EPSG:25832 coordinates, while the STL surfaces are translated to the same local origin used by the City4CFD handoff so they line up with city-models output.</p>
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


def scene_data(
    instances: list[TreeInstance],
    surface_origin_x: float,
    surface_origin_y: float,
) -> dict[str, Any]:
    if not instances:
        return {
            "extent": 10.0,
            "trees": [],
            "information": information_summary(instances),
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
        "information": information_summary(instances),
        "namedSpecies": [
            {"species": species, "count": count}
            for species, count in sorted(species_counts(instances).items(), key=lambda item: (-item[1], item[0]))
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
