"""Self-contained graphical feedback for air-purifier placement."""

from __future__ import annotations

import json
import math
from html import escape

from cities_reconstruction.geometry.stl_regions import REGION_NAMES, RegionMesh
from cities_reconstruction.stages.air_purifiers.models import AirPurifierInstance


def render_preview(
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
