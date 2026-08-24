from cities_reconstruction.stages.city_models.diagnostics import build_footprint_diagnostics


def test_build_footprint_diagnostics_reports_inner_ring_and_overlap_details() -> None:
    features = [
        {
            "type": "Feature",
            "properties": {"source_tag": "building:primary"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0], [0.0, 0.0]],
                    [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0], [1.0, 1.0]],
                ],
            },
        },
        {
            "type": "Feature",
            "properties": {"source_tag": "building:secondary"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[3.0, 0.0], [5.0, 0.0], [5.0, 2.0], [3.0, 2.0], [3.0, 0.0]],
                ],
            },
        },
    ]

    diagnostics = build_footprint_diagnostics(features)

    assert diagnostics["overlap_status"] == "warning"
    assert diagnostics["feature_count"] == 2
    assert diagnostics["polygon_count"] == 2
    assert diagnostics["invalid_or_empty_geometry_count"] == 0
    assert diagnostics["inner_ring_count"] == 1
    assert diagnostics["overlap_pair_count"] == 1
    assert diagnostics["largest_overlap_area_m2"] == 2.0
    assert diagnostics["overlaps"] == [
        {
            "first_feature_index": 0,
            "second_feature_index": 1,
            "first_source_tag": "building:primary",
            "second_source_tag": "building:secondary",
            "intersection_area_m2": 2.0,
            "overlap_ratio_of_smaller": 0.5,
        }
    ]
