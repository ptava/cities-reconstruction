from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

import pytest

from tools.air_purifier_towers.config import (
    FourSideTowerSpec,
    OctagonalTowerSpec,
    ParameterError,
    load_specs,
)
from tools.air_purifier_towers.geometry import (
    REGION_NAMES,
    build_region_mesh,
    mesh_edge_counts,
)
from tools.air_purifier_towers.outputs import stl_text, write_assets


ROOT = Path(__file__).resolve().parents[1]
PARAMETERS = ROOT / "docs/assets/air_purifier_towers/parameters.json"


def test_loads_two_compact_tower_specs() -> None:
    octagonal, four_side = load_specs(PARAMETERS)

    assert isinstance(octagonal, OctagonalTowerSpec)
    assert octagonal.name == "compact_octagonal_tower"
    assert octagonal.height_m == 4.0
    assert octagonal.base_width_m == 1.5
    assert isinstance(four_side, FourSideTowerSpec)
    assert four_side.name == "compact_four_side_tower"
    assert four_side.height_m == 4.0
    assert (four_side.width_m, four_side.depth_m) == (1.5, 1.5)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("height_m", 0.0, "height_m must be greater than zero"),
        ("inlet_base_m", -0.1, "inlet_base_m must be non-negative"),
        ("inlet_height_m", 4.0, "inlet panel must remain below the roof"),
        ("inlet_width_m", 1.5, "inlet_width_m must be smaller than both side widths"),
    ],
)
def test_rejects_invalid_four_side_parameters(
    tmp_path: Path, field: str, value: float, message: str
) -> None:
    data = json.loads(PARAMETERS.read_text(encoding="utf-8"))
    data["models"][1][field] = value
    path = tmp_path / "parameters.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ParameterError, match=message):
        load_specs(path)


@pytest.mark.parametrize("spec", load_specs(PARAMETERS))
def test_region_mesh_is_finite_closed_and_uses_exact_regions(spec) -> None:
    mesh = build_region_mesh(spec)

    assert tuple(mesh) == REGION_NAMES == ("inlet", "outlet", "tower")
    assert all(mesh[region] for region in REGION_NAMES)
    assert all(
        math.isfinite(component)
        for triangles in mesh.values()
        for triangle in triangles
        for point in triangle
        for component in point
    )
    assert set(mesh_edge_counts(mesh).values()) == {2}


def test_default_mesh_bounds_match_parameters() -> None:
    octagonal, four_side = load_specs(PARAMETERS)
    expected = ((octagonal, 1.5, 1.5), (four_side, 1.5, 1.5))
    for spec, width, depth in expected:
        points = [
            point
            for triangles in build_region_mesh(spec).values()
            for triangle in triangles
            for point in triangle
        ]
        xs, ys, zs = zip(*points, strict=True)
        assert max(xs) - min(xs) == pytest.approx(width)
        assert max(ys) - min(ys) == pytest.approx(depth)
        assert min(zs) == pytest.approx(0.0)
        assert max(zs) == pytest.approx(4.0)


def test_four_side_has_one_inlet_panel_on_each_vertical_side() -> None:
    spec = load_specs(PARAMETERS)[1]
    assert isinstance(spec, FourSideTowerSpec)
    triangles = build_region_mesh(spec)["inlet"]
    side_counts = Counter()
    half_x, half_y = spec.width_m / 2.0, spec.depth_m / 2.0
    for triangle in triangles:
        centroid = tuple(
            sum(point[axis] for point in triangle) / 3.0 for axis in range(3)
        )
        if abs(abs(centroid[0]) - half_x) < 1e-8:
            side_counts["x+" if centroid[0] > 0 else "x-"] += 1
        if abs(abs(centroid[1]) - half_y) < 1e-8:
            side_counts["y+" if centroid[1] > 0 else "y-"] += 1
    assert set(side_counts) == {"x+", "x-", "y+", "y-"}


def test_four_side_entire_roof_is_outlet() -> None:
    spec = load_specs(PARAMETERS)[1]
    assert isinstance(spec, FourSideTowerSpec)
    mesh = build_region_mesh(spec)
    assert all(
        all(point[2] == pytest.approx(spec.height_m) for point in triangle)
        for triangle in mesh["outlet"]
    )
    assert not any(
        all(point[2] == pytest.approx(spec.height_m) for point in triangle)
        for triangle in mesh["tower"]
    )


def test_ascii_stl_has_exact_nonempty_solid_blocks() -> None:
    spec = load_specs(PARAMETERS)[0]
    text = stl_text(spec.name, build_region_mesh(spec))

    assert text.startswith("solid inlet\n")
    assert [line for line in text.splitlines() if line.startswith("solid ")] == [
        "solid inlet",
        "solid outlet",
        "solid tower",
    ]
    assert [line for line in text.splitlines() if line.startswith("endsolid ")] == [
        "endsolid inlet",
        "endsolid outlet",
        "endsolid tower",
    ]
    assert "facet normal" in text


def test_generation_is_byte_identical(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_paths = write_assets(PARAMETERS, first)
    second_paths = write_assets(PARAMETERS, second)

    assert [path.relative_to(first) for path in first_paths] == [
        path.relative_to(second) for path in second_paths
    ]
    assert [path.read_bytes() for path in first_paths] == [
        path.read_bytes() for path in second_paths
    ]


def test_committed_stls_match_generation(tmp_path: Path) -> None:
    generated = write_assets(PARAMETERS, tmp_path)
    committed_root = PARAMETERS.parent
    for path in generated:
        assert path.read_bytes() == (
            committed_root / path.relative_to(tmp_path)
        ).read_bytes()


def test_preview_is_offline_and_labels_models_and_regions(tmp_path: Path) -> None:
    paths = write_assets(PARAMETERS, tmp_path)
    preview = tmp_path / "air_purifier_towers_preview.html"

    assert preview in paths
    text = preview.read_text(encoding="utf-8")
    assert "compact_octagonal_tower" in text
    assert "compact_four_side_tower" in text
    assert all(f'data-region="{region}"' in text for region in REGION_NAMES)
    assert "https://" not in text and "http://" not in text
    assert "Inlet" in text and "Outlet" in text and "Tower" in text
