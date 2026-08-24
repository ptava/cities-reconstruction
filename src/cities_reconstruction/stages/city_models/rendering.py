"""HTML and mesh-scene rendering for the City4CFD reconstruction stage."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any

from cities_reconstruction.config import AppConfig

Point3 = tuple[float, float, float]
Triangle = tuple[str, Point3, Point3, Point3]

MAX_CITY4CFD_BUILDING_PREVIEW_TRIANGLES = 20000
MAX_CITY4CFD_TERRAIN_PREVIEW_TRIANGLES = 8000
MAX_CITY4CFD_SURFACE_LAYER_PREVIEW_TRIANGLES = 4000
SURFACE_LAYER_PREVIEW_COLORS = (
    (0.91, 0.47, 0.13),
    (0.52, 0.31, 0.76),
    (0.04, 0.58, 0.53),
    (0.86, 0.27, 0.45),
    (0.46, 0.57, 0.13),
    (0.02, 0.52, 0.78),
)


def stl_scene_data(triangles: list[Triangle]) -> dict[str, Any]:
    """Build browser scene data from deterministic QA STL triangles."""

    points = [point for _label, a, b, c in triangles for point in (a, b, c)]
    if not points:
        return {"extent": 1.0, "triangles": [], "source": "qa-stl-preview", "label": "QA preview triangles"}
    center_x, center_y = _triangle_focus_point(triangles)
    extent = (
        max(
            max(point[0] for point in points) - min(point[0] for point in points),
            max(point[1] for point in points) - min(point[1] for point in points),
            1.0,
        )
        / 2.0
    )
    return {
        "extent": extent,
        "source": "qa-stl-preview",
        "label": "QA preview triangles",
        "triangles": [
            {
                "kind": "terrain" if label == "terrain" else "building",
                "points": [
                    [round(a[0] - center_x, 3), round(a[1] - center_y, 3), round(a[2], 3)],
                    [round(b[0] - center_x, 3), round(b[1] - center_y, 3), round(b[2], 3)],
                    [round(c[0] - center_x, 3), round(c[1] - center_y, 3), round(c[2], 3)],
                ],
            }
            for label, a, b, c in triangles
        ],
    }


def city4cfd_mesh_scene_data(
    building_mesh_path: Path,
    terrain_mesh_path: Path,
    surface_mesh_paths: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Read City4CFD OBJ meshes into a bounded browser scene payload."""

    building_triangles = _read_obj_triangles(building_mesh_path, "building")
    terrain_triangles = _read_obj_triangles(terrain_mesh_path, "terrain")
    surface_mesh_paths = surface_mesh_paths or {}
    surface_layer_triangles = {
        category: _read_obj_triangles(path, f"surface_layer:{category}")
        for category, path in surface_mesh_paths.items()
    }
    all_surface_layer_triangles = [triangle for triangles in surface_layer_triangles.values() for triangle in triangles]
    all_triangles = [*building_triangles, *terrain_triangles, *all_surface_layer_triangles]
    if not all_triangles:
        return stl_scene_data([])
    sampled_building_triangles = _evenly_sample_triangles(
        building_triangles,
        MAX_CITY4CFD_BUILDING_PREVIEW_TRIANGLES,
    )
    sampled_terrain_triangles = _evenly_sample_triangles(
        terrain_triangles,
        MAX_CITY4CFD_TERRAIN_PREVIEW_TRIANGLES,
    )
    sampled_surface_layer_triangles = [
        triangle
        for layer_triangles in surface_layer_triangles.values()
        for triangle in _evenly_sample_triangles(
            layer_triangles,
            MAX_CITY4CFD_SURFACE_LAYER_PREVIEW_TRIANGLES,
        )
    ]
    triangles = [*sampled_terrain_triangles, *sampled_surface_layer_triangles, *sampled_building_triangles]
    points = [point for _kind, a, b, c in all_triangles for point in (a, b, c)]
    center_x, center_y = _triangle_focus_point(building_triangles or all_triangles)
    base_z = min(point[2] for point in points)
    extent = (
        max(
            max(point[0] for point in points) - min(point[0] for point in points),
            max(point[1] for point in points) - min(point[1] for point in points),
            max(point[2] for point in points) - base_z,
            1.0,
        )
        / 2.0
    )
    surface_colors = _surface_layer_color_map(surface_mesh_paths)
    return {
        "extent": extent,
        "source": "city4cfd" if building_triangles or terrain_triangles else "qa-stl-preview",
        "label": "City4CFD OBJ triangles" if building_triangles or terrain_triangles else "QA preview triangles",
        "totalBuildingTriangles": len(building_triangles),
        "totalTerrainTriangles": len(terrain_triangles),
        "shownBuildingTriangles": len(sampled_building_triangles),
        "shownTerrainTriangles": len(sampled_terrain_triangles),
        "totalSurfaceLayerTriangles": len(all_surface_layer_triangles),
        "shownSurfaceLayerTriangles": len(sampled_surface_layer_triangles),
        "triangles": [
            {
                "kind": kind,
                **({"color": list(surface_colors[kind.split(":", 1)[1]])} if kind.startswith("surface_layer:") else {}),
                "points": [
                    [round(a[0] - center_x, 3), round(a[1] - center_y, 3), round(a[2] - base_z, 3)],
                    [round(b[0] - center_x, 3), round(b[1] - center_y, 3), round(b[2] - base_z, 3)],
                    [round(c[0] - center_x, 3), round(c[1] - center_y, 3), round(c[2] - base_z, 3)],
                ],
            }
            for kind, a, b, c in triangles
        ],
    }


def _surface_layer_color_map(surface_layers: dict[str, Any]) -> dict[str, tuple[float, float, float]]:
    return {
        category: SURFACE_LAYER_PREVIEW_COLORS[index % len(SURFACE_LAYER_PREVIEW_COLORS)]
        for index, category in enumerate(surface_layers)
    }


def _render_surface_layer_legend(stage1_surface_layers: list[dict[str, Any]]) -> str:
    colors = _surface_layer_color_map({layer["category"]: None for layer in stage1_surface_layers})
    entries: list[str] = []
    for layer in stage1_surface_layers:
        category = str(layer["category"])
        red, green, blue = colors[category]
        color = f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"
        entries.append(
            f'<span><span class="swatch" style="background:{color}"></span>{escape(category)} surface layer</span>'
        )
    return "\n    ".join(entries)


def _evenly_sample_triangles(triangles: list[Triangle], limit: int) -> list[Triangle]:
    if len(triangles) <= limit:
        return triangles
    step = len(triangles) / limit
    return [triangles[min(int(index * step), len(triangles) - 1)] for index in range(limit)]


def _triangle_focus_point(triangles: list[Triangle]) -> tuple[float, float]:
    building_points = [point for kind, a, b, c in triangles if kind == "building" for point in (a, b, c)]
    points = building_points or [point for _kind, a, b, c in triangles for point in (a, b, c)]
    if not points:
        return 0.0, 0.0
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _read_obj_triangles(path: Path, kind: str) -> list[Triangle]:
    if not path.exists():
        return []
    vertices: list[Point3] = []
    triangles: list[Triangle] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("v "):
                parts = line.split()
                if len(parts) >= 4:
                    vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                continue
            if not line.startswith("f "):
                continue
            indices: list[int] = []
            for part in line.split()[1:]:
                index_text = part.split("/")[0]
                if not index_text:
                    continue
                index = int(index_text)
                if index < 0:
                    index = len(vertices) + index + 1
                indices.append(index - 1)
            if len(indices) < 3:
                continue
            anchor = vertices[indices[0]]
            for left, right in zip(indices[1:], indices[2:], strict=False):
                triangles.append((kind, anchor, vertices[left], vertices[right]))
    return triangles


def render_preview(
    config: AppConfig,
    features: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    footprint_diagnostics: dict[str, Any],
    surface_scene: dict[str, Any],
    stage1_surface_layers: list[dict[str, Any]],
) -> str:
    stl_scene = surface_scene
    stl_scene_json = json.dumps(stl_scene, separators=(",", ":"))
    surface_legend = _render_surface_layer_legend(stage1_surface_layers)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{escape(config.region.name)} City4CFD surfaces</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; color: #1f2933; background: #f8fafc; }}
    .canvas-stack {{ position: relative; width: min(1080px, 100%); height: min(68vh, 720px); border: 1px solid #c8d1dc; background: #ffffff; margin-bottom: 1.2rem; }}
    .canvas-stack canvas {{ position: absolute; inset: 0; display: block; width: 100%; height: 100%; }}
    #meshOverlay {{ pointer-events: none; background: transparent; }}
    .note {{ max-width: 1080px; color: #52606d; line-height: 1.35; }}
    .legend {{ display: flex; gap: 1rem; flex-wrap: wrap; margin: 0.8rem 0; color: #334155; }}
    .swatch {{ display: inline-block; width: 0.9rem; height: 0.9rem; margin-right: 0.35rem; vertical-align: -0.12rem; }}
    .zoom-controls {{ display: flex; gap: 0.45rem; flex-wrap: wrap; margin: 0.7rem 0; }}
    .zoom-controls button {{ border: 1px solid #94a3b8; background: #ffffff; color: #1f2933; padding: 0.35rem 0.65rem; border-radius: 0.35rem; cursor: pointer; }}
    .zoom-controls button:hover {{ background: #e2e8f0; }}
  </style>
</head>
<body>
  <h1>{escape(config.region.name)} City4CFD generated surfaces</h1>
  <div class="zoom-controls" aria-label="Generated surface preview zoom controls">
    <button type="button" data-view-index="0" data-zoom-in>Zoom in</button>
    <button type="button" data-view-index="0" data-zoom-out>Zoom out</button>
    <button type="button" data-view-index="0" data-zoom-reset>Reset zoom</button>
  </div>
  <div class="canvas-stack">
    <canvas id="stlScene" width="1400" height="900" aria-label="3D generated City4CFD surface preview"></canvas>
    <canvas id="meshOverlay" width="1400" height="900" aria-hidden="true"></canvas>
  </div>
  <div class="legend">
    <span><span class="swatch" style="background:#2563eb"></span>generated building mesh</span>
    <span><span class="swatch" style="background:#16a34a"></span>generated terrain mesh</span>
    {surface_legend}
  </div>
  <p class="note">Drag to rotate the generated surface preview. Use the mouse wheel or zoom buttons to zoom in and out. This plot renders the generated City4CFD OBJ meshes when present, with a bounded preview sample focused on the 3D objects so browser rendering remains responsive. If City4CFD has not produced meshes yet, the view falls back to the deterministic QA STL previews built from the same projected footprint and point-cloud evidence. Alignment status from the point-cloud stage: {escape(str(diagnostics.get("alignment_status", "unknown")))}. Footprint overlap status: {escape(str(footprint_diagnostics["overlap_status"]))} ({footprint_diagnostics["overlap_pair_count"]} pairs).</p>
  <script>
    const stlScene = {stl_scene_json};
    const views = [
      {{ canvas: document.getElementById("stlScene"), overlayCanvas: document.getElementById("meshOverlay"), scene: stlScene, mode: "stl", yaw: -0.65, pitch: 0.78, zoom: 1.0, dragging: false, last: null }},
    ];

    function resize(view) {{
      const canvas = view.canvas;
      const ratio = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(640, Math.round(rect.width * ratio));
      canvas.height = Math.max(420, Math.round(rect.height * ratio));
      view.overlayCanvas.width = canvas.width;
      view.overlayCanvas.height = canvas.height;
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
      const scale = Math.min(canvas.width, canvas.height) * 0.42 / view.scene.extent * view.zoom;
      return [canvas.width / 2 + x * scale, canvas.height * 0.62 - y * scale, z];
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

    function drawFace(view, points, color, stroke) {{
      const ctx = view.canvas.getContext("2d");
      if (points.length < 3) return;
      const projected = points.map((point) => project(view, point));
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(projected[0][0], projected[0][1]);
      for (let i = 1; i < projected.length; i++) ctx.lineTo(projected[i][0], projected[i][1]);
      ctx.closePath();
      ctx.fill();
      if (stroke) {{
        ctx.strokeStyle = stroke;
        ctx.lineWidth = 0.7;
        ctx.stroke();
      }}
    }}

    function mat4Identity() {{
      const out = new Float32Array(16);
      out[0] = 1;
      out[5] = 1;
      out[10] = 1;
      out[15] = 1;
      return out;
    }}

    function mat4Multiply(a, b) {{
      const out = new Float32Array(16);
      const a00 = a[0], a01 = a[1], a02 = a[2], a03 = a[3];
      const a10 = a[4], a11 = a[5], a12 = a[6], a13 = a[7];
      const a20 = a[8], a21 = a[9], a22 = a[10], a23 = a[11];
      const a30 = a[12], a31 = a[13], a32 = a[14], a33 = a[15];
      const b00 = b[0], b01 = b[1], b02 = b[2], b03 = b[3];
      const b10 = b[4], b11 = b[5], b12 = b[6], b13 = b[7];
      const b20 = b[8], b21 = b[9], b22 = b[10], b23 = b[11];
      const b30 = b[12], b31 = b[13], b32 = b[14], b33 = b[15];

      out[0] = b00 * a00 + b01 * a10 + b02 * a20 + b03 * a30;
      out[1] = b00 * a01 + b01 * a11 + b02 * a21 + b03 * a31;
      out[2] = b00 * a02 + b01 * a12 + b02 * a22 + b03 * a32;
      out[3] = b00 * a03 + b01 * a13 + b02 * a23 + b03 * a33;
      out[4] = b10 * a00 + b11 * a10 + b12 * a20 + b13 * a30;
      out[5] = b10 * a01 + b11 * a11 + b12 * a21 + b13 * a31;
      out[6] = b10 * a02 + b11 * a12 + b12 * a22 + b13 * a32;
      out[7] = b10 * a03 + b11 * a13 + b12 * a23 + b13 * a33;
      out[8] = b20 * a00 + b21 * a10 + b22 * a20 + b23 * a30;
      out[9] = b20 * a01 + b21 * a11 + b22 * a21 + b23 * a31;
      out[10] = b20 * a02 + b21 * a12 + b22 * a22 + b23 * a32;
      out[11] = b20 * a03 + b21 * a13 + b22 * a23 + b23 * a33;
      out[12] = b30 * a00 + b31 * a10 + b32 * a20 + b33 * a30;
      out[13] = b30 * a01 + b31 * a11 + b32 * a21 + b33 * a31;
      out[14] = b30 * a02 + b31 * a12 + b32 * a22 + b33 * a32;
      out[15] = b30 * a03 + b31 * a13 + b32 * a23 + b33 * a33;
      return out;
    }}

    function mat4Translation(x, y, z) {{
      const out = mat4Identity();
      out[12] = x;
      out[13] = y;
      out[14] = z;
      return out;
    }}

    function mat4RotationX(angle) {{
      const out = mat4Identity();
      const c = Math.cos(angle);
      const s = Math.sin(angle);
      out[5] = c;
      out[6] = s;
      out[9] = -s;
      out[10] = c;
      return out;
    }}

    function mat4RotationY(angle) {{
      const out = mat4Identity();
      const c = Math.cos(angle);
      const s = Math.sin(angle);
      out[0] = c;
      out[2] = -s;
      out[8] = s;
      out[10] = c;
      return out;
    }}

    function mat4Perspective(fovy, aspect, near, far) {{
      const f = 1.0 / Math.tan(fovy / 2.0);
      const out = new Float32Array(16);
      out[0] = f / aspect;
      out[5] = f;
      out[10] = (far + near) / (near - far);
      out[11] = -1;
      out[14] = (2 * far * near) / (near - far);
      return out;
    }}

    function createShader(gl, type, source) {{
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {{
        throw new Error(gl.getShaderInfoLog(shader) || "failed to compile shader");
      }}
      return shader;
    }}

    function createProgram(gl, vertexSource, fragmentSource) {{
      const program = gl.createProgram();
      gl.attachShader(program, createShader(gl, gl.VERTEX_SHADER, vertexSource));
      gl.attachShader(program, createShader(gl, gl.FRAGMENT_SHADER, fragmentSource));
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {{
        throw new Error(gl.getProgramInfoLog(program) || "failed to link program");
      }}
      return program;
    }}

    function buildMeshBuffers(scene) {{
      const positions = [];
      const normals = [];
      const colors = [];
      for (const triangle of scene.triangles) {{
        const [a, b, c] = triangle.points;
        const ux = b[0] - a[0], uy = b[1] - a[1], uz = b[2] - a[2];
        const vx = c[0] - a[0], vy = c[1] - a[1], vz = c[2] - a[2];
        let nx = uy * vz - uz * vy;
        let ny = uz * vx - ux * vz;
        let nz = ux * vy - uy * vx;
        const length = Math.hypot(nx, ny, nz) || 1.0;
        nx /= length;
        ny /= length;
        nz /= length;
        const color = triangle.color || (triangle.kind === "terrain" ? [0.12, 0.60, 0.34] : [0.15, 0.42, 0.83]);
        for (const point of triangle.points) {{
          positions.push(point[0], point[1], point[2]);
          normals.push(nx, ny, nz);
          colors.push(color[0], color[1], color[2]);
        }}
      }}
      return {{
        positions: new Float32Array(positions),
        normals: new Float32Array(normals),
        colors: new Float32Array(colors),
        vertexCount: positions.length / 3,
      }};
    }}

    function initMeshView(view) {{
      if (view.gl || view.webglUnavailable) return;
      const gl = view.canvas.getContext("webgl2", {{ antialias: true, alpha: false }});
      if (!gl) {{
        view.webglUnavailable = true;
        return;
      }}
      const vertexSource = `#version 300 es
        in vec3 a_position;
        in vec3 a_normal;
        in vec3 a_color;
        uniform mat4 u_matrix;
        uniform mat4 u_normalMatrix;
        out vec3 v_normal;
        out vec3 v_color;
        void main() {{
          gl_Position = u_matrix * vec4(a_position, 1.0);
          v_normal = mat3(u_normalMatrix) * a_normal;
          v_color = a_color;
        }}
      `;
      const fragmentSource = `#version 300 es
        precision highp float;
        in vec3 v_normal;
        in vec3 v_color;
        out vec4 outColor;
        void main() {{
          vec3 n = normalize(v_normal);
          vec3 lightDir = normalize(vec3(0.45, 0.75, 0.50));
          float diffuse = max(dot(n, lightDir), 0.0);
          float light = 0.45 + diffuse * 0.55;
          outColor = vec4(v_color * light, 1.0);
        }}
      `;
      const program = createProgram(gl, vertexSource, fragmentSource);
      const mesh = buildMeshBuffers(view.scene);
      const vao = gl.createVertexArray();
      gl.bindVertexArray(vao);
      const positionBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, mesh.positions, gl.STATIC_DRAW);
      const positionLocation = gl.getAttribLocation(program, "a_position");
      gl.enableVertexAttribArray(positionLocation);
      gl.vertexAttribPointer(positionLocation, 3, gl.FLOAT, false, 0, 0);
      const normalBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, normalBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, mesh.normals, gl.STATIC_DRAW);
      const normalLocation = gl.getAttribLocation(program, "a_normal");
      gl.enableVertexAttribArray(normalLocation);
      gl.vertexAttribPointer(normalLocation, 3, gl.FLOAT, false, 0, 0);
      const colorBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, colorBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, mesh.colors, gl.STATIC_DRAW);
      const colorLocation = gl.getAttribLocation(program, "a_color");
      gl.enableVertexAttribArray(colorLocation);
      gl.vertexAttribPointer(colorLocation, 3, gl.FLOAT, false, 0, 0);
      gl.bindVertexArray(null);
      view.gl = gl;
      view.glProgram = program;
      view.glVao = vao;
      view.meshVertexCount = mesh.vertexCount;
      view.meshUniforms = {{
        matrix: gl.getUniformLocation(program, "u_matrix"),
        normalMatrix: gl.getUniformLocation(program, "u_normalMatrix"),
      }};
      gl.enable(gl.DEPTH_TEST);
      gl.disable(gl.CULL_FACE);
      gl.clearColor(1.0, 1.0, 1.0, 1.0);
    }}

    function drawMesh(view) {{
      initMeshView(view);
      if (!view.gl) {{
        const canvas = view.canvas;
        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        const faces = [...view.scene.triangles].sort((a, b) => {{
          const ap = a.points, bp = b.points;
          const ac = [(ap[0][0] + ap[1][0] + ap[2][0]) / 3, (ap[0][1] + ap[1][1] + ap[2][1]) / 3, (ap[0][2] + ap[1][2] + ap[2][2]) / 3];
          const bc = [(bp[0][0] + bp[1][0] + bp[2][0]) / 3, (bp[0][1] + bp[1][1] + bp[2][1]) / 3, (bp[0][2] + bp[1][2] + bp[2][2]) / 3];
          return rotate(view, ac)[2] - rotate(view, bc)[2];
        }});
        for (const triangle of faces) {{
          const rgb = triangle.color || (triangle.kind === "terrain" ? [0.12, 0.60, 0.34] : [0.15, 0.42, 0.83]);
          const fill = `rgba(${{Math.round(rgb[0] * 255)}}, ${{Math.round(rgb[1] * 255)}}, ${{Math.round(rgb[2] * 255)}}, 0.24)`;
          drawFace(view, triangle.points, fill, null);
        }}
        drawMeshOverlay(view);
        return;
      }}
      const gl = view.gl;
      const canvas = view.canvas;
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.useProgram(view.glProgram);
      gl.bindVertexArray(view.glVao);
      const aspect = canvas.width / canvas.height;
      const distance = Math.max(view.scene.extent * 2.8, 1.0) / view.zoom;
      const projection = mat4Perspective(45 * Math.PI / 180, aspect, 0.1, distance + view.scene.extent * 20.0);
      const rotation = mat4Multiply(mat4RotationX(view.pitch), mat4RotationY(view.yaw));
      const modelView = mat4Multiply(mat4Translation(0.0, 0.0, -distance), rotation);
      const matrix = mat4Multiply(projection, modelView);
      gl.uniformMatrix4fv(view.meshUniforms.matrix, false, matrix);
      gl.uniformMatrix4fv(view.meshUniforms.normalMatrix, false, rotation);
      gl.drawArrays(gl.TRIANGLES, 0, view.meshVertexCount);
      gl.bindVertexArray(null);
      drawMeshOverlay(view);
    }}

    function drawMeshOverlay(view) {{
      const overlay = view.overlayCanvas.getContext("2d");
      overlay.clearRect(0, 0, view.overlayCanvas.width, view.overlayCanvas.height);
      overlay.fillStyle = "#334155";
      overlay.font = `${{Math.max(13, view.overlayCanvas.width / 95)}}px Arial`;
      overlay.fillText(`Source: ${{view.scene.label}}`, 18, 28);
      if (view.scene.totalBuildingTriangles !== undefined) {{
        overlay.fillText(`Buildings: ${{view.scene.shownBuildingTriangles}} / ${{view.scene.totalBuildingTriangles}} triangles shown`, 18, 52);
        overlay.fillText(`Terrain: ${{view.scene.shownTerrainTriangles}} / ${{view.scene.totalTerrainTriangles}} triangles shown`, 18, 76);
        if (view.scene.totalSurfaceLayerTriangles !== undefined) {{
          overlay.fillText(`Surface layers: ${{view.scene.shownSurfaceLayerTriangles}} / ${{view.scene.totalSurfaceLayerTriangles}} triangles shown`, 18, 100);
        }}
      }}
    }}

    function draw(view) {{
      if (view.mode === "stl") {{
        drawMesh(view);
        return;
      }}
      const canvas = view.canvas;
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      const buildings = [...view.scene.buildings].sort((a, b) => rotate(view, a.center)[2] - rotate(view, b.center)[2]);
      for (const building of buildings) {{
        const top = building.top;
        const bottom = building.bottom;
        drawFace(view, top, building.hasRoofShape ? "rgba(15, 118, 110, 0.18)" : "rgba(217, 119, 6, 0.16)", null);
        if (building.peak) {{
          drawFace(view, [top[0], top[1], building.peak], "rgba(15, 118, 110, 0.10)", null);
        }}
      }}
      ctx.fillStyle = "#334155";
      ctx.font = `${{Math.max(13, canvas.width / 95)}}px Arial`;
      ctx.fillText(`3D buildings: ${{view.scene.buildings.length}}`, 18, 28);
      ctx.fillText(`LoD2.2 roof-shape evidence: ${{view.scene.roofShapeCount}}`, 18, 52);
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
        const factor = event.deltaY > 0 ? 0.9 : 1.1;
        view.zoom = Math.max(0.45, Math.min(2.75, view.zoom * factor));
        draw(view);
      }}, {{ passive: false }});
    }}
    for (const button of document.querySelectorAll("[data-view-index]")) {{
      button.addEventListener("click", () => {{
        const view = views[Number(button.dataset.viewIndex)];
        if (button.hasAttribute("data-zoom-in")) view.zoom = Math.max(0.45, Math.min(2.75, view.zoom * 1.2));
        if (button.hasAttribute("data-zoom-out")) view.zoom = Math.max(0.45, Math.min(2.75, view.zoom / 1.2));
        if (button.hasAttribute("data-zoom-reset")) view.zoom = 1.0;
        draw(view);
      }});
    }}
    window.addEventListener("resize", () => views.forEach(resize));
    views.forEach(resize);
  </script>
</body>
</html>
"""
