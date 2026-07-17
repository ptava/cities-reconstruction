from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


DEFAULT_SHAPEFILES_BLOCK = """
[shapefiles]
surface_precedence = [
    "buildings:building_part", "buildings", "water", "green_areas",
    "roads", "concrete", "other_terrain",
]

[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building"
match_any = ["building"]

[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building_part"
match_any = ["building:part"]

[[shapefiles.classification_rules]]
category = "trees"
group_tag = "tree"
match_any = ["natural=tree"]

[[shapefiles.classification_rules]]
category = "water"
group_tag = "water"
match_any = ["natural=water", "natural=wetland", "natural=bay", "water", "waterway", "landuse=reservoir", "landuse=basin"]

[[shapefiles.classification_rules]]
category = "green_areas"
group_tag = "green_area"
match_any = ["landuse=grass", "landuse=forest", "landuse=meadow", "landuse=recreation_ground", "landuse=village_green", "landuse=allotments", "landuse=orchard", "landuse=vineyard", "landuse=plant_nursery", "landuse=cemetery", "leisure=park", "leisure=garden", "leisure=pitch", "leisure=golf_course", "leisure=nature_reserve", "natural=wood", "natural=scrub", "natural=grassland", "natural=heath", "natural=tree_row"]

[[shapefiles.classification_rules]]
category = "roads"
group_tag = "road"
match_any = ["highway"]

[[shapefiles.classification_rules]]
category = "concrete"
group_tag = "paved_ground"
match_any = ["surface=asphalt", "surface=chipseal", "surface=cobblestone", "surface=compacted", "surface=concrete", "surface=concrete:lanes", "surface=concrete:plates", "surface=fine_gravel", "surface=gravel", "surface=paved", "surface=paving_stones", "surface=sett", "surface=stone", "surface=tiles", "surface=unpaved", "amenity=parking", "amenity=marketplace", "amenity=bicycle_parking", "amenity=motorcycle_parking", "landuse=industrial", "landuse=commercial", "landuse=retail", "landuse=construction", "landuse=railway", "landuse=brownfield", "place=square", "area=yes"]

[[shapefiles.classification_rules]]
category = "other_terrain"
group_tag = "other_terrain"
match_any = ["landuse", "leisure", "natural", "amenity", "tourism", "historic", "place", "surface", "area", "man_made"]
""".strip()


def write_complete_config(
    path: Path,
    *,
    output_root: Path | None = None,
    name: str = "Fixture",
    center_lat: float = 43.7696,
    center_lon: float = 11.2558,
    crs: str = "EPSG:25832",
    inner_diameter_m: float | None = 200.0,
    outer_diameter_m: float = 400.0,
    overpass_url: str = "https://example.test/overpass",
    overpass_timeout_s: float = 60.0,
    tree_overlap_tolerance_m: float = 2.0,
    input_lines: tuple[str, ...] = (),
    default_tree_species: str = "Tilia",
    model_library_path: Path | None = None,
    category_mapping_path: Path | None = None,
    shapefiles_block: str = DEFAULT_SHAPEFILES_BLOCK,
    shapefiles_extra: str = "",
    air_purifiers_block: str = "",
    reconstruction_influence_region_m: float = 150.0,
    city_models_block: str | None = None,
    imagery_block: str = "sources = []",
) -> Path:
    output_root = output_root or path.parent / "outputs"
    default_city_models_block = f"""
[city_models]
lod = "2.2"
domain_bnd = 200.0
building_roof_default_base_height_m = 2.0
top_height = 300.0
bnd_type_bpg = "Rectangle"
bpg_blockage_ratio = false
flow_direction = [1.0, 1.0]
buffer_region = 20.0
reconstruct_boundaries = true
terrain_thinning = 10.0
building_percentile = 90.0
edge_max_len = 5.0
output_file_name = "Mesh"
output_format = "obj"
output_separately = true
output_log = true
log_file = "logFile.log"
docker_image = "tudelft3d/city4cfd:0.8.0"

[city_models.smooth_terrain]
iterations = 1
max_pts = 100000

[city_models.reconstruction_region]
influence_region_m = {reconstruction_influence_region_m}
complexity_factor = 0.6
validate = true

[city_models.filters]
min_area = 4.0
min_height = 2.0
""".strip()

    inner_diameter_line = (
        f"inner_diameter_m = {inner_diameter_m}"
        if inner_diameter_m is not None
        else ""
    )
    path.write_text(
        f"""
[region]
name = "{name}"
center_lat = {center_lat}
center_lon = {center_lon}
crs = "{crs}"
{inner_diameter_line}
outer_diameter_m = {outer_diameter_m}

[inputs]
overpass_url = "{overpass_url}"
overpass_timeout_s = {overpass_timeout_s}
tree_overlap_tolerance_m = {tree_overlap_tolerance_m}
{chr(10).join(input_lines)}

{shapefiles_block}

{shapefiles_extra}

[trees]
default = "{default_tree_species}"
model_library_path = "{(model_library_path or ROOT / "docs/assets/tree_models/categories/tree_categories.json").as_posix()}"
category_mapping_path = "{(category_mapping_path or ROOT / "docs/assets/data/florence_opendata/trees_diameter/species_category_mapping.json").as_posix()}"

{air_purifiers_block}

{city_models_block or default_city_models_block}

[imagery]
{imagery_block}

[output]
root_directory = "{output_root.as_posix()}"
""".strip(),
        encoding="utf-8",
    )
    return path
