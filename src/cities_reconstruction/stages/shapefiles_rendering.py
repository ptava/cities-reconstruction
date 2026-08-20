"""Self-contained HTML and SVG rendering for the shapefiles stage."""

from __future__ import annotations

import math
from html import escape
from pathlib import Path
from typing import Any

from cities_reconstruction.config import AppConfig

EARTH_RADIUS_M = 6_371_000.0

CATEGORY_STYLES = {
    "buildings": {"label": "Buildings", "color": "#b45f3c", "opacity": "0.64"},
    "roads": {"label": "Roads", "color": "#3f4954", "opacity": "0.86"},
    "green_areas": {"label": "Green areas", "color": "#2f8f46", "opacity": "0.58"},
    "concrete": {"label": "Concrete / paved areas", "color": "#9aa6af", "opacity": "0.66"},
    "water": {"label": "Water", "color": "#2b8fd7", "opacity": "0.62"},
    "trees": {"label": "Individual trees", "color": "#0f6b3a", "opacity": "0.9"},
    "other_terrain": {"label": "Other terrain features", "color": "#d6a93a", "opacity": "0.56"},
    "gap_fill": {"label": "Generated gap-fill surfaces", "color": "#f59e0b", "opacity": "0.5"},
    "air_purifiers": {"label": "Air purifiers", "color": "#7c3aed", "opacity": "0.98"},
}

UNCLASSIFIED_STYLE = {
    "label": "No retrieved surface / possible gap",
    "color": "#f8fafc",
    "stroke": "#94a3b8",
}

def render_preview_html(
    config: AppConfig,
    features: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    categories: tuple[str, ...],
) -> str:
    width = 900
    height = 700
    margin = 70
    scale = min((width - 2 * margin) / config.region.outer_diameter_m, (height - 2 * margin) / config.region.outer_diameter_m)
    center_x = width / 2.0
    center_y = height / 2.0
    inner_boundary_svg = ""
    if config.region.inner_diameter_m is not None:
        inner_radius_px = config.region.inner_diameter_m * scale / 2.0
        inner_boundary_svg = f'<circle cx="{center_x:.3f}" cy="{center_y:.3f}" r="{inner_radius_px:.3f}" fill="none" stroke="#102a43" stroke-width="2"/>'
    outer_radius_px = config.region.outer_diameter_m * scale / 2.0

    tree_overlap_filter = summary["tree_overlap_filter"]
    feature_svg = "\n".join(_feature_to_svg(feature, config, center_x, center_y, scale) for feature in _preview_order(features))
    removed_tree_svg = "\n".join(
        _removed_tree_marker_to_svg(marker, config, center_x, center_y, scale)
        for marker in tree_overlap_filter.get("removed_overpass_tree_markers", [])
    )
    counts = summary["feature_counts"]["by_category"]
    rows = "\n".join(
        _category_table_row(category, counts[category])
        for category in categories
    )
    gap_row = _gap_table_row()
    tree_source_rows = _tree_source_table_rows(features, tree_overlap_filter)
    green_source_rows = _green_source_table_rows(features)
    supplemental_surface_rows = _supplemental_surface_table_rows(config, features)
    planning_input_rows = _planning_input_table_rows(config, features)
    purifier_source_rows = _air_purifier_source_table_rows(features)
    surface_overlaps = summary["surface_overlap_diagnostics"]
    surface_precedence = " &gt; ".join(escape(item) for item in surface_overlaps["precedence"])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{escape(config.region.name)} shapefiles preview</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; color: #1f2933; }}
    main {{ display: grid; grid-template-columns: minmax(0, 900px) 260px; gap: 1.5rem; align-items: start; }}
    svg {{ width: 100%; height: auto; border: 1px solid #c8d1dc; background: #f8fafc; }}
    .zoom-frame {{ overflow: hidden; border: 1px solid #c8d1dc; background: #f8fafc; }}
    .zoom-frame svg {{ border: 0; display: block; }}
    .zoom-content {{ transform-origin: center center; transition: transform 120ms ease-out; }}
    .zoom-controls {{ display: flex; gap: 0.45rem; flex-wrap: wrap; margin: 0 0 0.7rem 0; }}
    .zoom-controls button {{ border: 1px solid #94a3b8; background: #ffffff; color: #1f2933; padding: 0.35rem 0.65rem; border-radius: 0.35rem; cursor: pointer; }}
    .zoom-controls button:hover {{ background: #e2e8f0; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #d9e2ec; padding: 0.4rem; text-align: left; }}
    th {{ font-weight: 600; }}
    .swatch {{ display: inline-block; width: 0.85rem; height: 0.85rem; margin-right: 0.45rem; border: 1px solid #475569; vertical-align: -0.1rem; }}
    .layer-toggle {{ display: inline-flex; align-items: center; width: 100%; border: 0; background: transparent; color: inherit; padding: 0; font: inherit; font-weight: 600; text-align: left; cursor: pointer; }}
    .layer-toggle:hover {{ color: #0f4c81; }}
    .layer-toggle.is-hidden {{ color: #7b8794; text-decoration: line-through; }}
    .toggle-indicator {{ display: inline-grid; place-items: center; width: 1rem; height: 1rem; margin-right: 0.35rem; border: 1px solid #64748b; border-radius: 0.2rem; background: #ffffff; color: #166534; font-size: 0.72rem; line-height: 1; text-decoration: none; }}
    .layer-toggle.is-hidden .toggle-indicator {{ color: transparent; background: #e2e8f0; }}
    .note {{ color: #52606d; font-size: 0.9rem; line-height: 1.35; }}
  </style>
</head>
<body>
  <h1>{escape(config.region.name)} feature retrieval preview</h1>
  <main>
    <section>
      {_zoom_controls_html()}
      <div class="zoom-frame" data-zoomable>
        <div class="zoom-content">
          <svg viewBox="0 0 {width} {height}" role="img" aria-label="Retrieved feature preview">
            <circle cx="{center_x:.3f}" cy="{center_y:.3f}" r="{outer_radius_px:.3f}" fill="none" stroke="#334e68" stroke-width="2" stroke-dasharray="8 6"/>
            {inner_boundary_svg}
            <circle cx="{center_x:.3f}" cy="{center_y:.3f}" r="4" fill="#102a43"/>
            {feature_svg}
            {removed_tree_svg}
          </svg>
        </div>
      </div>
    </section>
    <section>
      <h2>Legend and counts</h2>
      <table>{rows}{gap_row}</table>
      <h2>Tree source QA</h2>
      <table>{tree_source_rows}</table>
      <h2>Air purifier source QA</h2>
      <table>{purifier_source_rows}</table>
      <h2>Urban-planning inputs</h2>
      <table>{planning_input_rows}</table>
      <h2>Green-area source QA</h2>
      <table>{green_source_rows}</table>
      <h2>Supplemental surfaces</h2>
      <table>{supplemental_surface_rows}</table>
      <h2>Surface overlap QA</h2>
      <table>
        <tr><th>Input polygons</th><td>{surface_overlaps["input_polygon_features"]}</td></tr>
        <tr><th>Accepted disjoint polygons</th><td>{surface_overlaps["accepted_polygon_features"]}</td></tr>
        <tr><th>Partially clipped</th><td>{surface_overlaps["clipped_polygon_features"]}</td></tr>
        <tr><th>Fully covered and removed</th><td>{surface_overlaps["removed_polygon_features"]}</td></tr>
        <tr><th>Overlap area removed</th><td>{surface_overlaps["removed_overlap_area_m2"]:g} m²</td></tr>
      </table>
      <p class="note"><strong>Precedence:</strong> {surface_precedence}</p>
      <p class="note">Click any legend or source row to hide or show that feature layer.</p>
      <p class="note">All displayed polygon surfaces have been partitioned into mutually disjoint coverage using the configured precedence.</p>
      <p class="note">Only polygon features contribute to geometry reconstruction in this stage. Lines are shown as dashed reference features and do not fill terrain yet.</p>
      <p class="note">Supplemental tree points take precedence over Overpass tree nodes inside the configured overlap tolerance. Removed Overpass duplicates are shown as red crosses for review and are not written to <code>trees.geojson</code>.</p>
      <p class="note">Uncolored areas inside the ROI mean no polygon surface feature was retrieved for that location. Review those gaps together with the diagnostic report before continuing.</p>
      <p class="note">The preview uses a local tangent-plane approximation and feature centroids for region assignment. It is intended for stage-level feedback, not as a survey-grade map.</p>
      <p class="note">Use the mouse wheel or zoom buttons to zoom every feedback plot.</p>
    </section>
  </main>
  {_zoomable_preview_script()}
  {_feature_toggle_script()}
</body>
</html>
"""


def render_imagery_overlay_html(
    config: AppConfig,
    features: list[dict[str, Any]],
    imagery_diagnostics: dict[str, Any],
    tree_overlap_filter: dict[str, Any],
    *,
    categories: tuple[str, ...],
) -> str:
    bbox_data = imagery_diagnostics["bbox_lon_lat"]
    bbox = (
        float(bbox_data["min_lon"]),
        float(bbox_data["min_lat"]),
        float(bbox_data["max_lon"]),
        float(bbox_data["max_lat"]),
    )
    successful_sources = [
        source
        for source in imagery_diagnostics["sources"]
        if source.get("status") == "fetched" and isinstance(source.get("image_path"), str)
    ]
    counts = {category: 0 for category in categories}
    for feature in features:
        category = feature["properties"]["category"]
        if category in counts:
            counts[category] += 1
    rows = "\n".join(_category_table_row(category, counts[category]) for category in categories)
    rows = f"{rows}{_gap_table_row()}"
    tree_source_rows = ""
    green_source_rows = _green_source_table_rows(features)
    supplemental_surface_rows = _supplemental_surface_table_rows(config, features)
    planning_input_rows = _planning_input_table_rows(config, features)
    purifier_source_rows = _air_purifier_source_table_rows(features)
    if successful_sources:
        sections = "\n".join(
            _imagery_source_section(source, features, bbox, tree_overlap_filter)
            for source in successful_sources
        )
        tree_source_rows = _tree_source_table_rows(features, tree_overlap_filter)
    else:
        status_rows = "\n".join(
            f"<li>{escape(str(source.get('name', 'unknown')))}: {escape(str(source.get('status', 'unknown')))}"
            f"{_optional_error_text(source)}</li>"
            for source in imagery_diagnostics["sources"]
        ) or "<li>No imagery sources are configured.</li>"
        sections = f"""
      <section class="empty">
        <h2>No imagery fetched</h2>
        <ul>{status_rows}</ul>
      </section>
"""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{escape(config.region.name)} imagery overlay</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; color: #1f2933; }}
    main {{ display: grid; grid-template-columns: minmax(0, 920px) 280px; gap: 1.5rem; align-items: start; }}
    figure {{ margin: 0 0 1.5rem 0; }}
    figcaption {{ margin-top: 0.45rem; color: #52606d; font-size: 0.9rem; }}
    .overlay {{ position: relative; width: 100%; border: 1px solid #c8d1dc; background: #111827; }}
    .overlay img {{ display: block; width: 100%; height: auto; }}
    .overlay svg {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
    .zoom-frame {{ overflow: hidden; }}
    .zoom-content {{ position: relative; transform-origin: center center; transition: transform 120ms ease-out; }}
    .zoom-controls {{ display: flex; gap: 0.45rem; flex-wrap: wrap; margin: 0 0 0.7rem 0; }}
    .zoom-controls button {{ border: 1px solid #94a3b8; background: #ffffff; color: #1f2933; padding: 0.35rem 0.65rem; border-radius: 0.35rem; cursor: pointer; }}
    .zoom-controls button:hover {{ background: #e2e8f0; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #d9e2ec; padding: 0.4rem; text-align: left; }}
    th {{ font-weight: 600; }}
    .swatch {{ display: inline-block; width: 0.85rem; height: 0.85rem; margin-right: 0.45rem; border: 1px solid #475569; vertical-align: -0.1rem; }}
    .layer-toggle {{ display: inline-flex; align-items: center; width: 100%; border: 0; background: transparent; color: inherit; padding: 0; font: inherit; font-weight: 600; text-align: left; cursor: pointer; }}
    .layer-toggle:hover {{ color: #0f4c81; }}
    .layer-toggle.is-hidden {{ color: #7b8794; text-decoration: line-through; }}
    .toggle-indicator {{ display: inline-grid; place-items: center; width: 1rem; height: 1rem; margin-right: 0.35rem; border: 1px solid #64748b; border-radius: 0.2rem; background: #ffffff; color: #166534; font-size: 0.72rem; line-height: 1; text-decoration: none; }}
    .layer-toggle.is-hidden .toggle-indicator {{ color: transparent; background: #e2e8f0; }}
    .note, .empty {{ color: #52606d; font-size: 0.9rem; line-height: 1.35; }}
  </style>
</head>
<body>
  <h1>{escape(config.region.name)} imagery diagnostic overlay</h1>
  <main>
    <section>
      {sections}
    </section>
    <section>
      <h2>Legend and counts</h2>
      <table>{rows}</table>
      <h2>Tree source QA</h2>
      <table>{tree_source_rows or _tree_source_table_rows(features, tree_overlap_filter)}</table>
      <h2>Air purifier source QA</h2>
      <table>{purifier_source_rows}</table>
      <h2>Urban-planning inputs</h2>
      <table>{planning_input_rows}</table>
      <h2>Green-area source QA</h2>
      <table>{green_source_rows}</table>
      <h2>Supplemental surfaces</h2>
      <table>{supplemental_surface_rows}</table>
      <p class="note">Click any legend or source row to hide or show that feature layer on every imagery panel.</p>
      <p class="note">The image is diagnostic evidence only. No geometry is generated from imagery in this stage.</p>
      <p class="note">Supplemental tree points take precedence over Overpass tree nodes inside the configured overlap tolerance. Removed Overpass duplicates are shown as red crosses when imagery is available.</p>
      <p class="note">Colored filled shapes are OSM polygons that contribute to reconstruction. Dashed lines and points are retained as reference-only evidence.</p>
      <p class="note">Visible image areas without colored polygons are candidate coverage gaps to inspect in the text report and OSM tag inventory.</p>
      <p class="note">Use the mouse wheel or zoom buttons to zoom every feedback plot.</p>
    </section>
  </main>
  {_zoomable_preview_script()}
  {_feature_toggle_script()}
</body>
</html>
"""


def _imagery_source_section(
    source: dict[str, Any],
    features: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
    tree_overlap_filter: dict[str, Any],
) -> str:
    width = int(source.get("width", 1200))
    height = int(source.get("height", 1200))
    image_path = Path(str(source["image_path"]))
    image_src = f"imagery/{image_path.name}"
    feature_svg = "\n".join(_feature_to_bbox_svg(feature, bbox, width, height) for feature in _preview_order(features))
    removed_tree_svg = "\n".join(
        _removed_tree_marker_to_bbox_svg(marker, bbox, width, height)
        for marker in tree_overlap_filter.get("removed_overpass_tree_markers", [])
    )
    return f"""
      <figure>
        {_zoom_controls_html()}
        <div class="overlay zoom-frame" data-zoomable>
          <div class="zoom-content">
            <img src="{escape(image_src)}" alt="{escape(str(source['name']))}">
            <svg viewBox="0 0 {width} {height}" role="img" aria-label="OSM features over imagery">
              {feature_svg}
              {removed_tree_svg}
            </svg>
          </div>
        </div>
        <figcaption>{escape(str(source['name']))} / layer {escape(str(source['layer']))}</figcaption>
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


def _feature_toggle_script() -> str:
    return """
  <script>
    const hiddenFeatureCategories = new Set();
    const hiddenFeatureSources = new Set();
    const hiddenSupplementalInputs = new Set();
    const hiddenPlanningInputs = new Set();

    function updateFeatureLayerVisibility() {
      for (const layer of document.querySelectorAll(".feature-layer")) {
        const categoryVisible = !hiddenFeatureCategories.has(layer.dataset.featureCategory);
        const sourceVisible = !hiddenFeatureSources.has(layer.dataset.featureSource);
        const supplementalVisible = !layer.dataset.supplementalInput || !hiddenSupplementalInputs.has(layer.dataset.supplementalInput);
        const planningVisible = !layer.dataset.planningInput || !hiddenPlanningInputs.has(layer.dataset.planningInput);
        layer.style.display = categoryVisible && sourceVisible && supplementalVisible && planningVisible ? "" : "none";
      }
    }

    for (const toggle of document.querySelectorAll(".layer-toggle")) {
      toggle.addEventListener("click", () => {
        const category = toggle.dataset.toggleCategory;
        const source = toggle.dataset.toggleSource;
        const supplementalInput = toggle.dataset.toggleSupplementalInput;
        const planningInput = toggle.dataset.togglePlanningInput;
        const hiddenSet = category
          ? hiddenFeatureCategories
          : source
            ? hiddenFeatureSources
            : supplementalInput
              ? hiddenSupplementalInputs
              : hiddenPlanningInputs;
        const key = category || source || supplementalInput || planningInput;
        if (hiddenSet.has(key)) {
          hiddenSet.delete(key);
        } else {
          hiddenSet.add(key);
        }
        const isVisible = !hiddenSet.has(key);
        toggle.setAttribute("aria-pressed", String(isVisible));
        toggle.classList.toggle("is-hidden", !isVisible);
        updateFeatureLayerVisibility();
      });
    }
    updateFeatureLayerVisibility();
  </script>
"""


def _feature_to_bbox_svg(
    feature: dict[str, Any],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> str:
    geometry = feature["geometry"]
    category = feature["properties"]["category"]
    style = CATEGORY_STYLES[category]
    color, stroke, opacity, dash = _surface_render_style(feature, style)
    rendered = ""
    if geometry["type"] == "Point":
        x, y = _project_to_bbox(geometry["coordinates"], bbox, width, height)
        if category == "trees":
            rendered = _accepted_tree_marker_to_bbox_svg(x, y, feature)
        elif category == "air_purifiers":
            rendered = _air_purifier_marker_to_svg(x, y, feature, 7.5)
        else:
            rendered = f'<circle cx="{x:.3f}" cy="{y:.3f}" r="5.0" fill="{color}" stroke="#ffffff" stroke-width="1.4" opacity="{opacity}"/>'
    elif geometry["type"] == "LineString":
        points = _bbox_svg_points(geometry["coordinates"], bbox, width, height)
        rendered = f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="7 6" opacity="0.62"/>'
    elif geometry["type"] == "Polygon":
        path = _bbox_svg_path(geometry["coordinates"], bbox, width, height)
        rendered = f'<path d="{path}" fill="{color}" stroke="{stroke}" stroke-width="1.5" opacity="{opacity}" fill-rule="evenodd"{dash}/>'
    elif geometry["type"] == "MultiPolygon":
        rendered = "\n".join(
            f'<path d="{_bbox_svg_path(polygon, bbox, width, height)}" fill="{color}" stroke="{stroke}" stroke-width="1.5" opacity="{opacity}" fill-rule="evenodd"{dash}/>'
            for polygon in geometry["coordinates"]
        )
    return _toggleable_feature_group(feature, rendered)


def _bbox_svg_path(
    rings: list[list[list[float]]],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> str:
    return " ".join(
        _bbox_svg_ring_path(ring, bbox, width, height)
        for ring in rings
        if ring
    )


def _bbox_svg_ring_path(
    coordinates: list[list[float]],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> str:
    points = [
        f"{x:.3f} {y:.3f}"
        for x, y in (_project_to_bbox(point, bbox, width, height) for point in coordinates)
    ]
    if not points:
        return ""
    return f"M {' L '.join(points)} Z"


def _bbox_svg_points(
    coordinates: list[list[float]],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> str:
    return " ".join(
        f"{x:.3f},{y:.3f}"
        for x, y in (_project_to_bbox(point, bbox, width, height) for point in coordinates)
    )


def _project_to_bbox(
    coordinate: list[float],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[float, float]:
    min_lon, min_lat, max_lon, max_lat = bbox
    lon, lat = coordinate
    x = (lon - min_lon) / (max_lon - min_lon) * width
    y = (max_lat - lat) / (max_lat - min_lat) * height
    return x, y


def _optional_error_text(source: dict[str, Any]) -> str:
    error_text = source.get("error")
    if not error_text:
        return ""
    return f": {escape(str(error_text))}"


def _feature_to_svg(feature: dict[str, Any], config: AppConfig, center_x: float, center_y: float, scale: float) -> str:
    geometry = feature["geometry"]
    category = feature["properties"]["category"]
    style = CATEGORY_STYLES[category]
    color, stroke, opacity, dash = _surface_render_style(feature, style)
    rendered = ""
    if geometry["type"] == "Point":
        x, y = _project_to_preview(geometry["coordinates"], config, center_x, center_y, scale)
        if category == "trees":
            rendered = _accepted_tree_marker_to_svg(x, y, feature)
        elif category == "air_purifiers":
            rendered = _air_purifier_marker_to_svg(x, y, feature, 5.5)
        else:
            rendered = f'<circle cx="{x:.3f}" cy="{y:.3f}" r="3.8" fill="{color}" stroke="#ffffff" stroke-width="0.8" opacity="{opacity}"/>'
    elif geometry["type"] == "LineString":
        points = _svg_points(geometry["coordinates"], config, center_x, center_y, scale)
        rendered = f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="5 4" opacity="0.48"/>'
    elif geometry["type"] == "Polygon":
        path = _svg_path(geometry["coordinates"], config, center_x, center_y, scale)
        rendered = f'<path d="{path}" fill="{color}" stroke="{stroke}" stroke-width="1.1" opacity="{opacity}" fill-rule="evenodd"{dash}/>'
    elif geometry["type"] == "MultiPolygon":
        rendered = "\n".join(
            f'<path d="{_svg_path(polygon, config, center_x, center_y, scale)}" fill="{color}" stroke="{stroke}" stroke-width="1.1" opacity="{opacity}" fill-rule="evenodd"{dash}/>'
            for polygon in geometry["coordinates"]
        )
    return _toggleable_feature_group(feature, rendered)


def _toggleable_feature_group(feature: dict[str, Any], rendered: str) -> str:
    if not rendered:
        return ""
    category = str(feature.get("properties", {}).get("category", "unknown"))
    properties = feature.get("properties", {})
    supplemental_input = str(properties.get("supplemental_input_id", ""))
    planning_input = str(properties.get("urban_planning_input_id", ""))
    source = _feature_source_toggle_key(feature)
    return (
        f'<g class="feature-layer" data-feature-category="{escape(category)}" '
        f'data-feature-source="{escape(source)}" '
        f'data-supplemental-input="{escape(supplemental_input)}" '
        f'data-planning-input="{escape(planning_input)}">{rendered}</g>'
    )


def _feature_source_toggle_key(feature: dict[str, Any]) -> str:
    properties = feature.get("properties", {})
    category = properties.get("category")
    if category == "trees":
        if properties.get("source_type") == "urban_planning":
            return "planned_tree"
        if properties.get("source_type") == "supplemental":
            return "supplemental_tree"
        return "overpass_tree"
    if category == "air_purifiers":
        return "air_purifier"
    if category == "green_areas":
        return "supplemental_green" if properties.get("source_type") == "supplemental" else "overpass_green"
    return "category_only"


def _surface_render_style(feature: dict[str, Any], style: dict[str, str]) -> tuple[str, str, str, str]:
    if (
        feature.get("properties", {}).get("category") == "green_areas"
        and feature.get("properties", {}).get("source_type") == "supplemental"
    ):
        return "#84cc16", "#365314", "0.78", ' stroke-dasharray="5 3"'
    return style["color"], style["color"], style["opacity"], ""


def _accepted_tree_marker_to_svg(x: float, y: float, feature: dict[str, Any]) -> str:
    properties = feature.get("properties", {})
    if properties.get("source_type") == "urban_planning":
        points = f"{x:.3f},{y - 6.0:.3f} {x + 5.5:.3f},{y + 4.0:.3f} {x - 5.5:.3f},{y + 4.0:.3f}"
        return f'<polygon points="{points}" fill="#06b6d4" stroke="#164e63" stroke-width="1.4" opacity="0.98"/>'
    if properties.get("source_type") == "supplemental":
        points = f"{x:.3f},{y - 5.0:.3f} {x + 5.0:.3f},{y:.3f} {x:.3f},{y + 5.0:.3f} {x - 5.0:.3f},{y:.3f}"
        return f'<polygon points="{points}" fill="#22c55e" stroke="#064e3b" stroke-width="1.2" opacity="0.95"/>'
    return f'<circle cx="{x:.3f}" cy="{y:.3f}" r="4.4" fill="#facc15" stroke="#713f12" stroke-width="1.2" opacity="0.95"/>'


def _accepted_tree_marker_to_bbox_svg(x: float, y: float, feature: dict[str, Any]) -> str:
    properties = feature.get("properties", {})
    if properties.get("source_type") == "urban_planning":
        points = f"{x:.3f},{y - 8.0:.3f} {x + 7.5:.3f},{y + 5.5:.3f} {x - 7.5:.3f},{y + 5.5:.3f}"
        return f'<polygon points="{points}" fill="#06b6d4" stroke="#164e63" stroke-width="2.0" opacity="0.98"/>'
    if properties.get("source_type") == "supplemental":
        points = f"{x:.3f},{y - 7.0:.3f} {x + 7.0:.3f},{y:.3f} {x:.3f},{y + 7.0:.3f} {x - 7.0:.3f},{y:.3f}"
        return f'<polygon points="{points}" fill="#22c55e" stroke="#064e3b" stroke-width="2.0" opacity="0.95"/>'
    return f'<circle cx="{x:.3f}" cy="{y:.3f}" r="6.0" fill="#facc15" stroke="#713f12" stroke-width="2.0" opacity="0.95"/>'


def _removed_tree_marker_to_svg(marker: dict[str, Any], config: AppConfig, center_x: float, center_y: float, scale: float) -> str:
    coordinates = marker.get("coordinates")
    if not isinstance(coordinates, list | tuple) or len(coordinates) < 2:
        return ""
    x, y = _project_to_preview([float(coordinates[0]), float(coordinates[1])], config, center_x, center_y, scale)
    return _removed_tree_toggle_group(_cross_marker_svg(x, y, 5.5, 1.6))


def _removed_tree_marker_to_bbox_svg(marker: dict[str, Any], bbox: tuple[float, float, float, float], width: int, height: int) -> str:
    coordinates = marker.get("coordinates")
    if not isinstance(coordinates, list | tuple) or len(coordinates) < 2:
        return ""
    x, y = _project_to_bbox([float(coordinates[0]), float(coordinates[1])], bbox, width, height)
    return _removed_tree_toggle_group(_cross_marker_svg(x, y, 8.0, 2.4))


def _removed_tree_toggle_group(rendered: str) -> str:
    return (
        '<g class="feature-layer" data-feature-category="trees" '
        f'data-feature-source="removed_tree">{rendered}</g>'
    )


def _cross_marker_svg(x: float, y: float, radius: float, stroke_width: float) -> str:
    return (
        f'<g opacity="0.98">'
        f'<line x1="{x - radius:.3f}" y1="{y - radius:.3f}" x2="{x + radius:.3f}" y2="{y + radius:.3f}" '
        f'stroke="#dc2626" stroke-width="{stroke_width:g}" stroke-linecap="round"/>'
        f'<line x1="{x - radius:.3f}" y1="{y + radius:.3f}" x2="{x + radius:.3f}" y2="{y - radius:.3f}" '
        f'stroke="#dc2626" stroke-width="{stroke_width:g}" stroke-linecap="round"/>'
        f'</g>'
    )


def _preview_order(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = {
        "gap_fill": 0,
        "water": 1,
        "green_areas": 2,
        "concrete": 3,
        "other_terrain": 4,
        "buildings": 5,
        "roads": 6,
        "trees": 7,
        "air_purifiers": 8,
    }
    return sorted(features, key=lambda feature: order[feature["properties"]["category"]])


def _category_table_row(category: str, count: int) -> str:
    style = CATEGORY_STYLES[category]
    swatch = (
        f'<span class="swatch" style="background:{style["color"]}; opacity:{style["opacity"]};"></span>'
    )
    button = _layer_toggle_button(
        swatch=swatch,
        label=style["label"],
        attribute="category",
        key=category,
    )
    return f"<tr><th>{button}</th><td>{count}</td></tr>"


def _gap_table_row() -> str:
    swatch = (
        f'<span class="swatch" style="background:{UNCLASSIFIED_STYLE["color"]}; '
        f'border-color:{UNCLASSIFIED_STYLE["stroke"]};"></span>'
    )
    return f"<tr><th>{swatch}{escape(UNCLASSIFIED_STYLE['label'])}</th><td>-</td></tr>"


def _tree_source_table_rows(features: list[dict[str, Any]], tree_overlap_filter: dict[str, Any]) -> str:
    overpass_count = 0
    supplemental_count = 0
    planned_count = 0
    for feature in features:
        properties = feature.get("properties", {})
        if properties.get("category") != "trees":
            continue
        if properties.get("source_type") == "urban_planning":
            planned_count += 1
        elif properties.get("source_type") == "supplemental":
            supplemental_count += 1
        elif properties.get("source_tag") == "natural=tree":
            overpass_count += 1
    removed_count = int(tree_overlap_filter.get("removed_overpass_tree_count", 0))
    return "\n".join(
        [
            _tree_source_table_row("overpass", "Accepted Overpass trees", overpass_count),
            _tree_source_table_row("supplemental", "Accepted supplemental trees", supplemental_count),
            _tree_source_table_row("planned", "Accepted planned trees", planned_count),
            _tree_source_table_row("removed", "Removed Overpass duplicates", removed_count),
        ]
    )


def _air_purifier_source_table_rows(features: list[dict[str, Any]]) -> str:
    accepted_count = sum(
        feature.get("properties", {}).get("category") == "air_purifiers"
        for feature in features
    )
    return (
        '<tr><th>'
        + _layer_toggle_button(
            swatch='<span class="swatch" style="background:#7c3aed; border-color:#3b0764; transform:rotate(45deg);"></span>',
            label="Accepted air purifiers",
            attribute="source",
            key="air_purifier",
        )
        + f"</th><td>{accepted_count}</td></tr>"
    )


def _air_purifier_marker_to_svg(x: float, y: float, feature: dict[str, Any], radius: float) -> str:
    points = f"{x:.3f},{y - radius:.3f} {x + radius:.3f},{y:.3f} {x:.3f},{y + radius:.3f} {x - radius:.3f},{y:.3f}"
    return f'<polygon points="{points}" fill="#7c3aed" stroke="#3b0764" stroke-width="1.5" opacity="0.98"/>'


def _green_source_table_rows(features: list[dict[str, Any]]) -> str:
    overpass_count = 0
    supplemental_count = 0
    for feature in features:
        properties = feature.get("properties", {})
        if properties.get("category") != "green_areas":
            continue
        if properties.get("source_type") == "supplemental":
            supplemental_count += 1
        else:
            overpass_count += 1
    return "\n".join(
        (
            '<tr><th>'
            + _layer_toggle_button(
                swatch='<span class="swatch" style="background:#2f8f46; border-color:#14532d;"></span>',
                label="Overpass green areas",
                attribute="source",
                key="overpass_green",
            )
            + "</th>"
            f"<td>{overpass_count}</td></tr>",
            '<tr><th>'
            + _layer_toggle_button(
                swatch='<span class="swatch" style="background:#84cc16; border:2px dashed #365314;"></span>',
                label="Supplemental green areas",
                attribute="source",
                key="supplemental_green",
            )
            + "</th>"
            f"<td>{supplemental_count}</td></tr>",
        )
    )


def _supplemental_surface_table_rows(config: AppConfig, features: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for feature in features:
        surface_id = feature.get("properties", {}).get("supplemental_input_id")
        if isinstance(surface_id, str):
            counts[surface_id] = counts.get(surface_id, 0) + 1
    surfaces = tuple(item for item in config.shapefiles.supplemental if item.category != "trees")
    if not surfaces:
        return '<tr><th>No named surfaces configured</th><td>0</td></tr>'
    rows: list[str] = []
    for surface in surfaces:
        style = CATEGORY_STYLES[surface.category]
        state = "" if surface.enabled else " (disabled)"
        button = _layer_toggle_button(
            swatch=(
                f'<span class="swatch" style="background:{style["color"]}; '
                f'opacity:{style["opacity"]}; border-style:dashed;"></span>'
            ),
            label=f"{surface.name} [{surface.category}]{state}",
            attribute="supplemental-input",
            key=surface.name,
        )
        rows.append(f"<tr><th>{button}</th><td>{counts.get(surface.name, 0)}</td></tr>")
    return "\n".join(rows)


def _planning_input_table_rows(config: AppConfig, features: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for feature in features:
        input_id = feature.get("properties", {}).get("urban_planning_input_id")
        if isinstance(input_id, str):
            counts[input_id] = counts.get(input_id, 0) + 1
    if not config.urban_planning.inputs:
        return '<tr><th>No urban-planning inputs configured</th><td>0</td></tr>'
    rows: list[str] = []
    for planning_input in config.urban_planning.inputs:
        state = "" if planning_input.enabled else " (disabled)"
        button = _layer_toggle_button(
            swatch='<span class="swatch" style="background:#0ea5e9; border-color:#075985;"></span>',
            label=f"{planning_input.name}{state}",
            attribute="planning-input",
            key=planning_input.name,
        )
        rows.append(f"<tr><th>{button}</th><td>{counts.get(planning_input.name, 0)}</td></tr>")
    return "\n".join(rows)


def _tree_source_table_row(kind: str, label: str, count: int) -> str:
    source_key = {
        "overpass": "overpass_tree",
        "supplemental": "supplemental_tree",
        "planned": "planned_tree",
        "removed": "removed_tree",
    }[kind]
    button = _layer_toggle_button(
        swatch=_tree_source_symbol(kind),
        label=label,
        attribute="source",
        key=source_key,
    )
    return f"<tr><th>{button}</th><td>{count}</td></tr>"


def _layer_toggle_button(*, swatch: str, label: str, attribute: str, key: str) -> str:
    return (
        f'<button type="button" class="layer-toggle" data-toggle-{attribute}="{escape(key)}" '
        f'aria-pressed="true" title="Show or hide {escape(label)}">'
        f'<span class="toggle-indicator" aria-hidden="true">&#10003;</span>{swatch}{escape(label)}</button>'
    )


def _tree_source_symbol(kind: str) -> str:
    if kind == "supplemental":
        return '<span class="swatch" style="background:#22c55e; border-color:#064e3b; transform:rotate(45deg);"></span>'
    if kind == "planned":
        return '<span class="swatch" style="background:#06b6d4; border-color:#164e63; clip-path:polygon(50% 0, 100% 100%, 0 100%);"></span>'
    if kind == "removed":
        return '<span class="swatch" style="background:#ffffff; border-color:#dc2626; color:#dc2626; text-align:center; line-height:0.85rem; font-weight:700;">x</span>'
    return '<span class="swatch" style="background:#facc15; border-color:#713f12; border-radius:50%;"></span>'


def _svg_points(coordinates: list[list[float]], config: AppConfig, center_x: float, center_y: float, scale: float) -> str:
    return " ".join(
        f"{x:.3f},{y:.3f}"
        for x, y in (_project_to_preview(point, config, center_x, center_y, scale) for point in coordinates)
    )


def _svg_path(
    rings: list[list[list[float]]],
    config: AppConfig,
    center_x: float,
    center_y: float,
    scale: float,
) -> str:
    return " ".join(
        _svg_ring_path(ring, config, center_x, center_y, scale)
        for ring in rings
        if ring
    )


def _svg_ring_path(
    coordinates: list[list[float]],
    config: AppConfig,
    center_x: float,
    center_y: float,
    scale: float,
) -> str:
    points = [
        f"{x:.3f} {y:.3f}"
        for x, y in (_project_to_preview(point, config, center_x, center_y, scale) for point in coordinates)
    ]
    if not points:
        return ""
    return f"M {' L '.join(points)} Z"


def _project_to_preview(
    coordinate: list[float],
    config: AppConfig,
    center_x: float,
    center_y: float,
    scale: float,
) -> tuple[float, float]:
    lon, lat = coordinate
    x_m = math.radians(lon - config.region.center_lon) * EARTH_RADIUS_M * math.cos(math.radians(config.region.center_lat))
    y_m = math.radians(lat - config.region.center_lat) * EARTH_RADIUS_M
    return center_x + x_m * scale, center_y - y_m * scale
