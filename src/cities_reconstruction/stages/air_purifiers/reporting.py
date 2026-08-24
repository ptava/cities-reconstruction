"""Markdown reporting for air-purifier placement."""

from __future__ import annotations

from pathlib import Path

from cities_reconstruction.stages.air_purifiers.geometry import TERRAIN_CLEARANCE_M
from cities_reconstruction.stages.air_purifiers.models import AirPurifierInstance


def render_report(
    source: Path, catalog: Path, terrain: Path | None, origin_x: float, origin_y: float,
    instances: list[AirPurifierInstance], model_counts: dict[str, int],
    input_counts: dict[str, int], parameter_source_counts: dict[str, dict[str, int]], placement: Path,
    combined: Path, instance_paths: dict[str, Path], preview: Path, manifest: Path,
) -> str:
    model_lines = report_counts(model_counts)
    input_lines = report_counts(input_counts)
    parameter_lines = "\n\n".join(
        f"### {field.removesuffix('_source').replace('_', ' ').title()}\n\n{report_counts(counts)}"
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


def report_counts(counts: dict[str, int]) -> str:
    return "\n".join(f"- `{name}`: {count}" for name, count in sorted(counts.items()))
