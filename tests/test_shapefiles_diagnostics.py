from __future__ import annotations

from pathlib import Path

from cities_reconstruction.config import load_config
from cities_reconstruction.stages.shapefiles_diagnostics import (
    build_geometry_diagnostics,
    build_summary,
    non_contributing_features,
    supplemental_surface_input_diagnostics,
    supplemental_tree_input_diagnostics,
    urban_planning_diagnostics,
)
from cities_reconstruction.urban_planning import UrbanPlanningLoadResult
from tests.config_helpers import write_complete_config


def _feature(
    *,
    geometry_type: str,
    category: str,
    contributes: bool,
    roi_zone: str = "inner",
    group_tag: str = "fixture",
    source_tag: str = "fixture=yes",
) -> dict[str, object]:
    coordinates: object = [11.2558, 43.7696]
    if geometry_type == "LineString":
        coordinates = [[11.2558, 43.7696], [11.2559, 43.7697]]
    elif geometry_type == "Polygon":
        coordinates = [[[11.2558, 43.7696], [11.2559, 43.7696], [11.2558, 43.7696]]]
    return {
        "type": "Feature",
        "geometry": {"type": geometry_type, "coordinates": coordinates},
        "properties": {
            "category": category,
            "contributes_to_geometry": contributes,
            "roi_zone": roi_zone,
            "group_tag": group_tag,
            "source_tag": source_tag,
        },
    }


def test_build_geometry_diagnostics_counts_contributing_and_reference_features() -> None:
    features = [
        _feature(geometry_type="Polygon", category="buildings", contributes=True),
        _feature(geometry_type="LineString", category="roads", contributes=False),
    ]

    diagnostics = build_geometry_diagnostics(features, generated_gap_fill_count=3)

    assert diagnostics["contributing_feature_count"] == 1
    assert diagnostics["non_contributing_feature_count"] == 1
    assert diagnostics["geometry_type_counts"] == {"LineString": 1, "Polygon": 1}
    assert diagnostics["contributing_by_category"] == {"buildings": 1}
    assert diagnostics["non_contributing_by_category"] == {"roads": 1}
    assert diagnostics["non_contributing_by_geometry_type"] == {"LineString": 1}
    assert diagnostics["generated_gap_fill_feature_count"] == 3


def test_non_contributing_features_returns_only_reference_geometry() -> None:
    contributing = _feature(geometry_type="Polygon", category="buildings", contributes=True)
    reference = _feature(geometry_type="Point", category="trees", contributes=False)

    assert non_contributing_features([contributing, reference]) == [reference]


def test_build_summary_aggregates_feature_provenance_and_uniform_roi_assumptions(
    tmp_path: Path,
) -> None:
    config = load_config(
        write_complete_config(
            tmp_path / "config.toml",
            output_root=tmp_path / "outputs",
            inner_diameter_m=None,
            name="Diagnostics fixture",
        )
    )
    features = [
        _feature(
            geometry_type="Polygon",
            category="other_terrain",
            contributes=True,
            roi_zone="full",
            group_tag="unmapped",
            source_tag="landuse=railway",
        ),
        _feature(
            geometry_type="Point",
            category="trees",
            contributes=False,
            roi_zone="full",
            group_tag="tree",
            source_tag="natural=tree",
        ),
    ]

    summary = build_summary(
        config=config,
        features=features,
        raw_element_count=4,
        skipped_count=2,
        skipped_by_reason={"unsupported": 2},
        category_features={"other_terrain": [features[0]], "trees": [features[1]]},
        source="cached fixture",
        tag_inventory={"raw_elements": 4},
        geometry_diagnostics={"contributing_feature_count": 1},
        tree_overlap_filter={"removed_overpass_tree_count": 0},
        tree_input_diagnostics={"inputs": {}},
        surface_input_diagnostics={"surfaces": {}},
        surface_overlap_diagnostics={"precedence": []},
        urban_planning_diagnostics={"inputs": {}},
    )

    assert summary["region"]["name"] == "Diagnostics fixture"
    assert summary["feature_counts"] == {
        "raw_overpass_elements": 4,
        "accepted": 2,
        "skipped": 2,
        "skipped_by_reason": {"unsupported": 2},
        "by_category": {"other_terrain": 1, "trees": 1},
        "by_group_tag": {"tree": 1, "unmapped": 1},
        "by_source_tag": {"landuse=railway": 1, "natural=tree": 1},
        "by_roi_zone": {"full": 2},
        "available_not_mapped_to_core": {"landuse=railway": 1},
    }
    assert summary["source"] == "cached fixture"
    assert summary["assumptions"][0].startswith("No inner diameter is configured")


def test_input_diagnostics_report_supplemental_and_planning_counts(tmp_path: Path) -> None:
    tree_path = tmp_path / "trees.shp"
    surface_path = tmp_path / "streets.shp"
    planning_path = tmp_path / "planned.geojson"
    config = load_config(
        write_complete_config(
            tmp_path / "config.toml",
            output_root=tmp_path / "outputs",
            shapefiles_block=f'''[shapefiles]
surface_precedence = ["buildings", "supplemental:streets", "roads"]

[[shapefiles.supplemental]]
name = "trees"
path = "{tree_path.as_posix()}"
crs = "EPSG:4326"
category = "trees"

[[shapefiles.supplemental]]
name = "streets"
path = "{surface_path.as_posix()}"
crs = "EPSG:4326"
category = "roads"
group_tag = "municipal_streets"

[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building"
match_any = ["building"]

[[shapefiles.classification_rules]]
category = "roads"
group_tag = "road"
match_any = ["highway"]

[[urban_planning.inputs]]
name = "planned"
path = "{planning_path.as_posix()}"
crs = "EPSG:4326"''',
        )
    )
    loaded = {
        "trees": [_feature(geometry_type="Point", category="trees", contributes=False)],
        "streets": [
            _feature(geometry_type="Polygon", category="roads", contributes=True),
            _feature(geometry_type="Polygon", category="roads", contributes=True),
        ],
    }

    tree_diagnostics = supplemental_tree_input_diagnostics(config, loaded)
    surface_diagnostics = supplemental_surface_input_diagnostics(config, loaded)

    assert tree_diagnostics["configured_inputs"] == 1
    assert tree_diagnostics["loaded_features"] == 1
    assert tree_diagnostics["inputs"]["trees"]["path"] == str(tree_path)
    assert surface_diagnostics["configured_surfaces"] == 1
    assert surface_diagnostics["loaded_features"] == 2
    assert surface_diagnostics["surfaces"]["streets"]["group_tag"] == "municipal_streets"

    accepted_tree = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [11.2558, 43.7696]},
        "properties": {
            "urban_planning_input_id": "planned",
            "source_feature_index": 0,
            "id": "TREE-1",
            "kind": "tree",
            "roi_distance_m": 0.0,
        },
    }
    outside_purifier = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [11.3, 43.8]},
        "properties": {
            "urban_planning_input_id": "planned",
            "source_feature_index": 1,
            "id": "AP-1",
            "kind": "air_purifier",
            "roi_distance_m": 5000.0,
        },
    }
    result = UrbanPlanningLoadResult(
        accepted_features=(accepted_tree,),
        outside_roi_features=(outside_purifier,),
        per_input={"planned": {"source_features": 2, "accepted_features": 1, "outside_roi": 1}},
    )

    planning_diagnostics = urban_planning_diagnostics(config, result)

    assert planning_diagnostics["accepted_by_kind"] == {"tree": 1, "air_purifier": 0}
    assert planning_diagnostics["outside_by_kind"] == {"tree": 0, "air_purifier": 1}
    assert planning_diagnostics["inputs"]["planned"]["accepted_by_kind"] == {
        "tree": 1,
        "air_purifier": 0,
    }
    assert planning_diagnostics["outside_records"] == [
        {
            "urban_planning_input_id": "planned",
            "source_feature_index": 1,
            "id": "AP-1",
            "kind": "air_purifier",
            "coordinates": [11.3, 43.8],
            "roi_distance_m": 5000.0,
        }
    ]
