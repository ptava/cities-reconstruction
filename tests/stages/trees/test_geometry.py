from cities_reconstruction.stages.trees import geometry
from cities_reconstruction.stages.trees.models import TreeInstance
from cities_reconstruction.stages.trees.publication import placement_geojson


def test_translate_triangles_offsets_each_vertex_without_changing_label() -> None:
    triangles = [("crown", (1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0))]

    translated = geometry.translate_triangles(triangles, dx=-1.0, dy=2.0, dz=0.5)

    assert translated == [("crown", (0.0, 4.0, 3.5), (3.0, 7.0, 6.5), (6.0, 10.0, 9.5))]


def test_placement_geojson_records_requested_working_crs() -> None:
    instance = TreeInstance(
        tree_id="tree_1", species="Tilia", source_species=None, model_category="large",
        crown_shape="ellipsoid", x=198643.415, y=4853099.823, z=0.0, height_m=10.0,
        crown_radius_m=2.0, trunk_radius_m=0.2, trunk_height_m=3.0, roi_zone="inner",
        osm_id=None, model_source="default", height_source="default", crown_radius_source="default",
        trunk_radius_source="default", used_tags=(), defaulted_fields=(),
    )

    payload = placement_geojson([instance], working_crs="EPSG:32633")

    assert payload["features"][0]["geometry"]["coordinates"][:2] == [198643.415, 4853099.823]
    assert payload["features"][0]["properties"]["projected_crs"] == "EPSG:32633"
