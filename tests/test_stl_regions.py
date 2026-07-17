from __future__ import annotations

import math
from pathlib import Path

import pytest

from cities_reconstruction.config import ConfigError
from cities_reconstruction.geometry.stl_regions import (
    mesh_bounds,
    mesh_edge_counts,
    read_region_stl,
    transform_region_mesh,
    write_region_stl,
)


MODELS = Path(__file__).parents[1] / "docs" / "assets" / "air_purifier_towers" / "models"
FACET = (
    "  facet normal 0 0 1\n"
    "    outer loop\n"
    "      vertex 0.000000000 0 0\n"
    "      vertex 1.000000000 0 0\n"
    "      vertex 0.000000000 1 0\n"
    "    endloop\n"
    "  endfacet\n"
)


@pytest.mark.parametrize(
    "name",
    ["compact_octagonal_tower.stl", "compact_four_side_tower.stl"],
)
def test_reads_closed_three_region_tower_mesh(name: str) -> None:
    mesh = read_region_stl(MODELS / name)

    assert tuple(mesh) == ("inlet", "outlet", "tower")
    assert all(mesh[region] for region in mesh)
    assert set(mesh_edge_counts(mesh).values()) == {2}
    assert all(
        math.isfinite(value)
        for triangles in mesh.values()
        for triangle in triangles
        for point in triangle
        for value in point
    )


def test_transform_scales_rotates_and_preserves_base() -> None:
    mesh = read_region_stl(MODELS / "compact_four_side_tower.stl")
    transformed = transform_region_mesh(
        mesh,
        scale=(2.0, 1.0, 0.5),
        rotation_deg=90.0,
        translation=(10.0, 20.0, 3.0),
    )

    assert mesh_bounds(transformed) == pytest.approx((9.25, 10.75, 18.5, 21.5, 3.0, 5.0))


def test_transform_rejects_non_finite_output_coordinates() -> None:
    mesh = read_region_stl(MODELS / "compact_four_side_tower.stl")

    with pytest.raises(ConfigError, match="transformation produced non-finite coordinates"):
        transform_region_mesh(
            mesh,
            scale=(1.0, 1.0, 1e308),
            rotation_deg=0.0,
            translation=(0.0, 0.0, 0.0),
        )


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (b"\x00\x01solid inlet\n", "ASCII STL"),
        (b"# comment\nsolid inlet\nendsolid inlet\n", "before first solid"),
        (b"nonsense\n", "unexpected STL token"),
        (b"solid inlet\nendsolid outlet\n", "does not match"),
    ],
)
def test_rejects_binary_or_malformed_stl(tmp_path: Path, contents: bytes, message: str) -> None:
    path = tmp_path / "invalid.stl"
    path.write_bytes(contents)

    with pytest.raises(ConfigError, match=message):
        read_region_stl(path)


@pytest.mark.parametrize(
    "facet",
    [
        FACET.replace("facet normal 0 0 1", "facet normal 0 0"),
        FACET.replace("outer loop", "outer loops"),
        FACET.replace("      vertex 0.000000000 1 0\n", ""),
        FACET.replace("vertex 0.000000000 0 0", "vertex 0.000000000 0"),
        FACET.replace("    endloop\n", ""),
        FACET.replace("  endfacet\n", ""),
    ],
    ids=(
        "malformed-normal",
        "incorrect-outer-loop",
        "fewer-than-three-vertices",
        "malformed-vertex-arity",
        "missing-endloop",
        "missing-endfacet",
    ),
)
def test_rejects_malformed_facet_structure(tmp_path: Path, facet: str) -> None:
    path = tmp_path / "malformed_facet.stl"
    path.write_text(f"solid inlet\n{facet}endsolid inlet\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="malformed STL"):
        read_region_stl(path)


@pytest.mark.parametrize(
    ("regions", "message"),
    [
        (("inlet", "outlet"), "exactly.*inlet.*outlet.*tower"),
        (("inlet", "outlet", "tower", "extra"), "exactly.*inlet.*outlet.*tower"),
        (("outlet", "inlet", "tower"), "order"),
        (("inlet", "outlet", "tower"), "empty solid"),
    ],
)
def test_rejects_missing_extra_wrong_order_or_empty_regions(
    tmp_path: Path,
    regions: tuple[str, ...],
    message: str,
) -> None:
    path = tmp_path / "regions.stl"
    solids = []
    for index, region in enumerate(regions):
        facets = "" if len(regions) == 3 else _facet(float(index))
        solids.append(f"solid {region}\n{facets}endsolid {region}\n")
    path.write_text("".join(solids), encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        read_region_stl(path)


@pytest.mark.parametrize(
    ("vertex", "message"),
    [
        ("vertex nan 0 0", "finite"),
        ("vertex 0 0 0", "degenerate"),
    ],
)
def test_rejects_non_finite_or_degenerate_triangles(tmp_path: Path, vertex: str, message: str) -> None:
    path = tmp_path / "invalid_triangle.stl"
    first_facet = _facet(0.0).replace("vertex 0.000000000 0 0", vertex, 1)
    if message == "degenerate":
        first_facet = first_facet.replace("vertex 1.000000000 0 0", "vertex 0 0 0")
    path.write_text(
        "".join(
            f"solid {region}\n{first_facet if index == 0 else _facet(float(index + 1))}endsolid {region}\n"
            for index, region in enumerate(("inlet", "outlet", "tower"))
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        read_region_stl(path)


def test_rejects_non_manifold_combined_mesh(tmp_path: Path) -> None:
    path = tmp_path / "open.stl"
    path.write_text(
        "".join(
            f"solid {region}\n{_facet(float(index))}endsolid {region}\n"
            for index, region in enumerate(("inlet", "outlet", "tower"))
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="closed manifold"):
        read_region_stl(path)


def test_writes_deterministic_round_trip(tmp_path: Path) -> None:
    source = read_region_stl(MODELS / "compact_four_side_tower.stl")
    first = tmp_path / "first.stl"
    second = tmp_path / "second.stl"

    write_region_stl(first, source)
    write_region_stl(second, {region: list(reversed(triangles)) for region, triangles in source.items()})

    assert first.read_bytes() == second.read_bytes()
    assert read_region_stl(first) == source
    assert ".000000000" in first.read_text(encoding="utf-8")


def _facet(offset: float) -> str:
    return (
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        f"      vertex {offset:.9f} 0 0\n"
        f"      vertex {offset + 1.0:.9f} 0 0\n"
        f"      vertex {offset:.9f} 1 0\n"
        "    endloop\n"
        "  endfacet\n"
    )
