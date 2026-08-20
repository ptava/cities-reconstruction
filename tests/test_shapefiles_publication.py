from __future__ import annotations

from pathlib import Path

from cities_reconstruction.stage_contract import ArtifactKind, StageStatus
from cities_reconstruction.stages.shapefiles_publication import (
    ShapefilesPublicationInput,
    imagery_source_slug,
    publish_shapefiles_manifest,
)


def _artifact(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name, encoding="utf-8")
    return path


def test_publish_shapefiles_manifest_preserves_artifact_contract(tmp_path: Path) -> None:
    paths = {
        name: _artifact(tmp_path, name)
        for name in (
            "all.geojson",
            "urban.geojson",
            "purifiers.geojson",
            "buildings.geojson",
            "trees.geojson",
            "inner.geojson",
            "outer.geojson",
            "tag-query.txt",
            "tag-raw.json",
            "tag-inventory.json",
            "query.txt",
            "raw.json",
            "diagnostics.json",
            "non-contributing.geojson",
            "imagery.json",
            "overlay.html",
            "imagery/source-request.txt",
            "imagery/source.png",
            "imagery/error-request.txt",
            "imagery/error.txt",
            "summary.json",
            "report.md",
            "preview.html",
        )
    }
    publication = ShapefilesPublicationInput(
        output_directory=tmp_path,
        report_path=paths["report.md"],
        preview_path=paths["preview.html"],
        input_state_fingerprint={"config": "fixture"},
        all_features_path=paths["all.geojson"],
        urban_planning_path=paths["urban.geojson"],
        air_purifiers_path=paths["purifiers.geojson"],
        category_paths={
            "trees": paths["trees.geojson"],
            "buildings": paths["buildings.geojson"],
        },
        region_paths={
            "outer_ring": paths["outer.geojson"],
            "inner": paths["inner.geojson"],
        },
        tag_inventory_query_path=paths["tag-query.txt"],
        tag_inventory_raw_path=paths["tag-raw.json"],
        tag_inventory_path=paths["tag-inventory.json"],
        query_path=paths["query.txt"],
        raw_path=paths["raw.json"],
        diagnostics_path=paths["diagnostics.json"],
        diagnostics_geojson_path=paths["non-contributing.geojson"],
        imagery_diagnostics_path=paths["imagery.json"],
        imagery_overlay_path=paths["overlay.html"],
        imagery_diagnostics={
            "sources": [
                {
                    "name": "Comune WMS #1",
                    "status": "fetched",
                    "request_url_path": str(paths["imagery/source-request.txt"]),
                    "image_path": str(paths["imagery/source.png"]),
                },
                {
                    "name": "Failed source",
                    "status": "error",
                    "request_url_path": str(paths["imagery/error-request.txt"]),
                    "error_path": str(paths["imagery/error.txt"]),
                },
                {
                    "name": "Missing evidence",
                    "status": "fetched",
                    "request_url_path": str(tmp_path / "missing-request.txt"),
                    "image_path": str(tmp_path / "missing.png"),
                },
            ]
        },
        summary_path=paths["summary.json"],
        raw_element_count=12,
        accepted_feature_count=9,
        skipped_feature_count=3,
        source="fixture input",
    )

    manifest = publish_shapefiles_manifest(publication)

    assert manifest.status is StageStatus.COMPLETED
    assert manifest.stage == "shapefiles"
    assert manifest.metrics == {
        "raw_element_count": 12,
        "accepted_feature_count": 9,
        "skipped_feature_count": 3,
    }
    assert manifest.details == {
        "source": "fixture input",
        "categories": ["buildings", "trees"],
        "regions": ["inner", "outer_ring"],
    }
    assert [(artifact.name, artifact.kind) for artifact in manifest.artifacts] == [
        ("all-features", ArtifactKind.HANDOFF),
        ("urban-planning", ArtifactKind.HANDOFF),
        ("air-purifiers", ArtifactKind.HANDOFF),
        ("category-buildings", ArtifactKind.HANDOFF),
        ("category-trees", ArtifactKind.HANDOFF),
        ("region-inner", ArtifactKind.HANDOFF),
        ("region-outer-ring", ArtifactKind.HANDOFF),
        ("tag-inventory-query", ArtifactKind.DIAGNOSTIC),
        ("tag-inventory-raw", ArtifactKind.DIAGNOSTIC),
        ("tag-inventory", ArtifactKind.SUPPORTING),
        ("overpass-query", ArtifactKind.DIAGNOSTIC),
        ("overpass-raw", ArtifactKind.DIAGNOSTIC),
        ("geometry-diagnostics", ArtifactKind.DIAGNOSTIC),
        ("non-contributing-features", ArtifactKind.DIAGNOSTIC),
        ("imagery-diagnostics", ArtifactKind.DIAGNOSTIC),
        ("imagery-overlay", ArtifactKind.DIAGNOSTIC),
        ("imagery-comune_wms_1-1-request", ArtifactKind.DIAGNOSTIC),
        ("imagery-comune_wms_1-1-image", ArtifactKind.SUPPORTING),
        ("imagery-failed_source-2-request", ArtifactKind.DIAGNOSTIC),
        ("imagery-failed_source-2-error", ArtifactKind.DIAGNOSTIC),
        ("summary", ArtifactKind.SUPPORTING),
        ("report", ArtifactKind.REPORT),
        ("preview", ArtifactKind.PREVIEW),
    ]
    assert manifest.manifest_path.is_file()


def test_imagery_source_slug_has_stable_fallback() -> None:
    assert imagery_source_slug("Comune WMS #1") == "comune_wms_1"
    assert imagery_source_slug("---") == "imagery"
