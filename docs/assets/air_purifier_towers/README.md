# Parametric Air-Purifier Tower Assets

## Purpose

This folder contains two compact, generic base air-purifier tower models for city-scale CFD geometry and OpenFOAM surface-region preparation. They are intentionally simple external envelopes, not manufacturer-specific purifier designs. These source assets remain in model-local coordinates; the application writes scaled and city-aligned copies under `05_air_purifiers`.

Both models use metres, stand on `z = 0`, are 4 m tall by default, and fit inside a 1.5 m by 1.5 m footprint. The authoritative dimensions are in `parameters.json`.

## Models

- `models/compact_octagonal_tower.stl`: the preferred tapered octagonal design, with a continuous lower intake band and a smaller raised roof exhaust.
- `models/compact_four_side_tower.stl`: a square tower with one lower intake panel on each of its four sides and the entire roof assigned as exhaust.

Each file is a deterministic, multi-solid ASCII STL generated from CadQuery faces.

## Surface regions

Every STL contains exactly three named solids:

- `inlet`: the lower intake band or four lower intake panels
- `outlet`: the upward-facing roof exhaust
- `tower`: the rest of the external envelope

Together, the three regions form one closed manifold surface. Each triangle belongs to exactly one region, and patch boundaries follow shared triangle edges. The inlet and outlet are surface labels for later boundary assignment: they are not holes, ducts, fans, filters, or an internal flow volume.

OpenFOAM tools that read multi-solid ASCII STL files can retain these names as surface regions. Case-specific meshing and boundary conditions remain the responsibility of the downstream OpenFOAM workflow.

## Parameters

`parameters.json` contains one entry per model. Common fields are:

| Field | Meaning |
|---|---|
| `name` | Stable model identifier |
| `kind` | Geometry generator: `octagonal` or `four_side` |
| `output_path` | Generated STL path relative to this folder |
| `height_m` | Overall height, including the raised exhaust |
| `linear_tolerance_m` | CadQuery tessellation linear tolerance |
| `angular_tolerance_rad` | CadQuery tessellation angular tolerance |

Octagonal-only fields are `base_width_m`, `body_top_width_m`, `inlet_bottom_m`, `inlet_top_m`, `exhaust_height_m`, and `outlet_width_m`. The widths are across-flats dimensions.

Four-side-only fields are `width_m`, `depth_m`, `inlet_width_m`, `inlet_height_m`, and `inlet_base_m`. The inlet dimensions apply independently to the centred panel on every side; the whole top face remains the outlet.

Invalid or overlapping dimensions are rejected with a clear parameter error rather than producing a partial mesh.

## Regeneration

CadQuery is a development-only dependency managed by `uv`. From the repository root, generate both STLs and the preview with:

```bash
uv run python tools/build_air_purifier_tower_models.py
```

Verify that the committed assets match a fresh deterministic generation without rewriting them:

```bash
uv run python tools/build_air_purifier_tower_models.py --check
```

Use `--parameters` and `--output-directory` to test an alternative parameter file without replacing the documented defaults.

## Visual QA

Open [air_purifier_towers_preview.html](air_purifier_towers_preview.html) locally. The self-contained preview requires no network connection and shows both models side by side:

- blue: inlet
- red: outlet
- grey: tower

Drag a model to orbit, use the mouse wheel to zoom, or select **Reset view**. Check that the octagonal inlet is continuous, the four-side tower has one inlet on each side, and the outlet surfaces face upward.

This preview verifies the two unplaced base models. After placement, inspect `<output.root_directory>/05_air_purifiers/air_purifier_models_preview.html`; that separate preview is centred on the generated city instances and renders the exact transformed triangles written to the placed STL files.

## Placement workflow

Stage 1 reads air-purifier Point features from enabled mixed
`[[urban_planning.inputs]]` GeoJSON files and normalizes them into
`01_shapefiles/air_purifiers.geojson`. Inputs may use `EPSG:4326` (the default)
or `EPSG:3857`; Stage 1 converts both to longitude/latitude. Every purifier
requires a globally unique safe `id`, `kind = "air_purifier"`, and an exact
catalog `model`. Optional positive `height_m`, `width_m`, and `depth_m` values
override model defaults independently, while finite `rotation_deg` defaults to
zero. The executable `air-purifiers` stage resolves this catalog, applies the
requested scaling and counter-clockwise +Z rotation, translates each model into
the City4CFD local frame, and optionally samples terrain.

```toml
[[urban_planning.inputs]]
name = "mercato_centrale"
path = "../data/urban_planning/mercato_centrale/urban_plan.geojson"
# crs = "EPSG:4326"

[air_purifiers]
model_library_path = "../models/air_purifier_towers/parameters.json"
```

```bash
uv run cities-reconstruction run-stage --config path/to/config.toml air-purifiers \
  --model-library path/to/parameters.json \
  --terrain-geometry path/to/terrain.obj
```

`--model-library` and `--terrain-geometry` override the corresponding optional `[air_purifiers]` keys and resolve relative to the selected TOML file. A model catalog is required at execution; unresolved terrain is allowed and produces an explicit `z=0` fallback.

Placed-city outputs are written under `<output.root_directory>/05_air_purifiers`:

- `surfaces/air_purifiers_combined.stl`: every instance aggregated into exact non-empty `inlet`, `outlet`, and `tower` solids
- `surfaces/instances/<PURIF_ID>.stl`: one transformed three-region STL per safe ID
- `air_purifier_placements.geojson`: projected/source coordinates, local transforms, dimensions, scale, rotation, terrain, and provenance
- `air_purifier_models_manifest.json`, `air_purifier_models_report.md`, and `air_purifier_models_preview.html`: completion, textual QA, and graphical QA

The externally authored Mercato Centrale example is
`docs/assets/data/urban_planning/mercato_centrale/urban_plan.geojson`. Its one
portable EPSG:4326 FeatureCollection mixes 35 tree proposals with six
air-purifier proposals. Placement suitability and constraint review are
external to this application; Stage 1 and the model stages validate and
reconstruct the supplied records without optimizing their positions.

## OpenFOAM handoff

The source STL coordinates are local model coordinates in metres. The application now places transformed copies through the `air-purifiers` stage while preserving these originals. It does not merge the placed geometry into City4CFD meshes or generate corresponding OpenFOAM boundary conditions.

## Limitations

These assets model only the external closed envelope and named boundary patches. They contain no internal flow passage, purifier performance data, pressure jump, porous region, fan curve, pollutant-removal model, foundation, maintenance access, or electrical equipment.
