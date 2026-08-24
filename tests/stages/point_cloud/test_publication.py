from __future__ import annotations

from pathlib import Path

from cities_reconstruction.stage_contract import ArtifactKind, StageStatus
from cities_reconstruction.stages.point_cloud.publication import (
    PointCloudPublicationInput,
    publish_point_cloud_manifest,
)


def _artifact(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text(name, encoding="utf-8")
    return path


def test_publish_point_cloud_manifest_preserves_artifact_contract(tmp_path: Path) -> None:
    paths = {
        name: _artifact(tmp_path, name)
        for name in (
            "building_footprints_epsg25832.geojson",
            "ground_points.ply",
            "building_points.ply",
            "tree_points.ply",
            "unclassified_points.ply",
            "alignment_diagnostics.json",
            "point_cloud_report.md",
            "point_cloud_alignment_preview.html",
        )
    }
    publication = PointCloudPublicationInput(
        output_directory=tmp_path,
        report_path=paths["point_cloud_report.md"],
        preview_path=paths["point_cloud_alignment_preview.html"],
        input_state_fingerprint={"config": "fixture"},
        projected_footprints_path=paths["building_footprints_epsg25832.geojson"],
        ground_points_path=paths["ground_points.ply"],
        building_points_path=paths["building_points.ply"],
        tree_points_path=paths["tree_points.ply"],
        unclassified_points_path=paths["unclassified_points.ply"],
        diagnostics_path=paths["alignment_diagnostics.json"],
        ground_point_count=25,
        building_point_count=9,
        tree_point_count=3,
        unclassified_point_count=13,
        alignment_status="warning",
        source_building_footprints=tmp_path / "source_buildings.geojson",
        crs="EPSG:25832",
        tree_filter={"enabled": True, "tree_point_count": 3},
    )

    manifest = publish_point_cloud_manifest(publication)

    assert manifest.status is StageStatus.COMPLETED
    assert manifest.stage == "point-cloud"
    assert manifest.metrics == {
        "ground_point_count": 25,
        "building_point_count": 9,
        "tree_point_count": 3,
        "unclassified_point_count": 13,
        "alignment_status": "warning",
    }
    assert manifest.details == {
        "source_building_footprints": str(tmp_path / "source_buildings.geojson"),
        "crs": "EPSG:25832",
        "tree_filter": {"enabled": True, "tree_point_count": 3},
    }
    assert [(artifact.name, artifact.kind, artifact.required) for artifact in manifest.artifacts] == [
        ("projected-building-footprints", ArtifactKind.HANDOFF, True),
        ("ground-points", ArtifactKind.HANDOFF, True),
        ("building-points", ArtifactKind.HANDOFF, True),
        ("tree-points", ArtifactKind.HANDOFF, False),
        ("unclassified-points", ArtifactKind.DIAGNOSTIC, True),
        ("alignment-diagnostics", ArtifactKind.DIAGNOSTIC, True),
        ("report", ArtifactKind.REPORT, True),
        ("preview", ArtifactKind.PREVIEW, True),
    ]
    assert manifest.manifest_path.is_file()


def test_publish_point_cloud_manifest_omits_disabled_tree_handoff(tmp_path: Path) -> None:
    paths = {
        name: _artifact(tmp_path, name)
        for name in (
            "building_footprints_epsg25832.geojson",
            "ground_points.ply",
            "building_points.ply",
            "unclassified_points.ply",
            "alignment_diagnostics.json",
            "point_cloud_report.md",
            "point_cloud_alignment_preview.html",
        )
    }
    publication = PointCloudPublicationInput(
        output_directory=tmp_path,
        report_path=paths["point_cloud_report.md"],
        preview_path=paths["point_cloud_alignment_preview.html"],
        input_state_fingerprint={"config": "fixture"},
        projected_footprints_path=paths["building_footprints_epsg25832.geojson"],
        ground_points_path=paths["ground_points.ply"],
        building_points_path=paths["building_points.ply"],
        tree_points_path=None,
        unclassified_points_path=paths["unclassified_points.ply"],
        diagnostics_path=paths["alignment_diagnostics.json"],
        ground_point_count=25,
        building_point_count=9,
        tree_point_count=0,
        unclassified_point_count=16,
        alignment_status="passed",
        source_building_footprints=tmp_path / "source_buildings.geojson",
        crs="EPSG:25832",
        tree_filter={"enabled": False, "tree_point_count": 0},
    )

    manifest = publish_point_cloud_manifest(publication)

    assert "tree-points" not in {artifact.name for artifact in manifest.artifacts}
    assert manifest.metrics["tree_point_count"] == 0
