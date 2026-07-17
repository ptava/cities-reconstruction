from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import re

import pytest


from cities_reconstruction.config import (
    AirPurifiersConfig,
    ConfigError,
    load_config,
    validate_config,
)
from tests.config_helpers import DEFAULT_SHAPEFILES_BLOCK, write_complete_config


ROOT = Path(__file__).resolve().parents[1]


def test_loads_complete_config_fixture(tmp_path: Path) -> None:
    config = load_config(write_complete_config(tmp_path / "config.toml"))

    assert config.region.name == "Fixture"
    assert config.region.crs == "EPSG:25832"
    assert config.region.inner_diameter_m == 200.0
    assert config.region.outer_diameter_m == 400.0
    assert config.trees.default == "Tilia"
    assert config.city_models.lod == "2.2"
    assert config.city_models.domain_bnd == 200.0
    assert config.city_models.building_roof_default_base_height_m == 2.0
    assert config.city_models.top_height == 300.0
    assert config.city_models.flow_direction == (1.0, 1.0)
    assert config.city_models.smooth_terrain.iterations == 1
    assert config.city_models.reconstruction_region.influence_region_m == 150.0
    assert config.city_models.filters.min_height == 2.0
    assert config.inputs.dtm_directory is None
    assert config.inputs.tree_overlap_tolerance_m == 2.0
    assert config.inputs.overpass_max_attempts == 3
    assert config.inputs.overpass_retry_backoff_s == 2.0
    assert len(config.shapefiles.classification_rules) == 8
    assert config.shapefiles.classification_rules[0].category == "buildings"
    assert config.shapefiles.classification_rules[0].match_any == ("building",)
    assert config.shapefiles.classification_rules[-1].category == "other_terrain"
    assert config.shapefiles.surface_precedence[:2] == ("buildings:building_part", "buildings")
    assert str(config.trees.model_library_path).endswith("docs/assets/tree_models/categories/tree_categories.json")
    assert str(config.trees.category_mapping_path).endswith("docs/assets/data/florence_opendata/trees_diameter/species_category_mapping.json")
    assert config.shapefiles.supplemental == ()
    assert config.urban_planning.inputs == ()
    assert config.imagery.sources == ()


@pytest.mark.parametrize(
    ("original", "injected", "unknown_key"),
    [
        ("[region]", "unexpected_root_key = true\n\n[region]", "unexpected_root_key"),
        (
            "[city_models]\n",
            "[city_models]\nenforce_validity = \"lod1.2\"\n",
            "city_models.enforce_validity",
        ),
        (
            'category = "buildings"\n',
            'category = "buildings"\npriority = 10\n',
            "shapefiles.classification_rules[1].priority",
        ),
    ],
)
def test_rejects_unknown_configuration_keys(
    tmp_path: Path,
    original: str,
    injected: str,
    unknown_key: str,
) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    path.write_text(
        path.read_text(encoding="utf-8").replace(original, injected, 1),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=rf"unknown configuration key: {re.escape(unknown_key)}"):
        load_config(path)


def test_validates_optional_overpass_retry_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    text = path.read_text(encoding="utf-8").replace(
        "overpass_timeout_s = 60.0",
        "overpass_timeout_s = 60.0\noverpass_max_attempts = 0\noverpass_retry_backoff_s = -1.0",
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="inputs.overpass_max_attempts must be a positive integer"):
        load_config(path)

    path.write_text(text.replace("overpass_max_attempts = 0", "overpass_max_attempts = 2"), encoding="utf-8")
    with pytest.raises(ConfigError, match="inputs.overpass_retry_backoff_s must be non-negative"):
        load_config(path)


def test_city_models_domain_boundary_is_optional_but_must_be_positive(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    configured = path.read_text(encoding="utf-8")

    path.write_text(configured.replace("domain_bnd = 200.0\n", ""), encoding="utf-8")
    assert load_config(path).city_models.domain_bnd is None

    path.write_text(configured.replace("domain_bnd = 200.0", "domain_bnd = -1.0"), encoding="utf-8")
    with pytest.raises(ConfigError, match="city_models.domain_bnd must be positive"):
        load_config(path)


@pytest.mark.parametrize(
    ("original", "invalid", "field"),
    [
        ('lod = "2.2"', 'lod = "3.0"', "city_models.lod"),
        (
            'bnd_type_bpg = "Rectangle"',
            'bnd_type_bpg = "Circle"',
            "city_models.bnd_type_bpg",
        ),
        ('output_format = "obj"', 'output_format = "ply"', "city_models.output_format"),
    ],
)
def test_rejects_undocumented_city_models_enums(
    tmp_path: Path,
    original: str,
    invalid: str,
    field: str,
) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    path.write_text(path.read_text(encoding="utf-8").replace(original, invalid), encoding="utf-8")

    with pytest.raises(ConfigError, match=field.replace(".", r"\.")):
        load_config(path)


@pytest.mark.parametrize(
    "setting",
    [
        "domain_bnd",
        "building_roof_default_base_height_m",
        "top_height",
        "buffer_region",
        "terrain_thinning",
        "building_percentile",
        "edge_max_len",
        "influence_region_m",
        "complexity_factor",
        "min_area",
        "min_height",
    ],
)
def test_rejects_non_finite_city_models_numbers(tmp_path: Path, setting: str) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(
            f"{setting} = nan" if line.startswith(f"{setting} = ") else line
            for line in lines
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must be finite"):
        load_config(path)


@pytest.mark.parametrize("setting", ["top_height", "edge_max_len", "influence_region_m", "min_area", "min_height"])
def test_rejects_zero_for_positive_city_models_fields(tmp_path: Path, setting: str) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(f"{setting} = 0.0" if line.startswith(f"{setting} = ") else line for line in lines) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must be positive"):
        load_config(path)


def test_accepts_documented_inclusive_city_models_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    text = path.read_text(encoding="utf-8")
    text = text.replace("building_roof_default_base_height_m = 2.0", "building_roof_default_base_height_m = 0.0")
    text = text.replace("buffer_region = 20.0", "buffer_region = -5.0")
    text = text.replace("terrain_thinning = 10.0", "terrain_thinning = 0.0")
    text = text.replace("building_percentile = 90.0", "building_percentile = 100.0")
    text = text.replace("complexity_factor = 0.6", "complexity_factor = 0.0")
    path.write_text(text, encoding="utf-8")

    config = load_config(path)

    assert config.city_models.building_roof_default_base_height_m == 0.0
    assert config.city_models.buffer_region == -5.0
    assert config.city_models.terrain_thinning == 0.0
    assert config.city_models.building_percentile == 100.0
    assert config.city_models.reconstruction_region.complexity_factor == 0.0


@pytest.mark.parametrize("setting", ["output_file_name", "log_file", "docker_image"])
def test_rejects_whitespace_city_models_strings(tmp_path: Path, setting: str) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(f'{setting} = "   "' if line.startswith(f"{setting} = ") else line for line in lines) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=setting):
        load_config(path)


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("output_file_name", "../Mesh"),
        ("output_file_name", r"nested\Mesh"),
        ("log_file", "/tmp/city4cfd.log"),
        ("log_file", r"nested\city4cfd.log"),
    ],
)
def test_rejects_path_like_city_models_filenames(
    tmp_path: Path,
    setting: str,
    value: str,
) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    lines = path.read_text(encoding="utf-8").splitlines()
    escaped_value = value.replace("\\", "\\\\")
    path.write_text(
        "\n".join(
            f'{setting} = "{escaped_value}"' if line.startswith(f"{setting} = ") else line
            for line in lines
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=rf"city_models\.{setting} must be a filename"):
        load_config(path)


def test_rejects_option_like_city4cfd_docker_image(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    text = path.read_text(encoding="utf-8").replace(
        'docker_image = "tudelft3d/city4cfd:0.8.0"',
        'docker_image = "--privileged"',
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="docker_image must not begin with '-'"):
        load_config(path)


@pytest.mark.parametrize(
    ("boundary", "blockage", "accepted"),
    [
        ("Rectangle", False, False),
        ("Oval", False, False),
        ("Round", True, False),
        ("Round", False, True),
    ],
)
def test_zero_flow_direction_is_valid_only_when_not_required(
    tmp_path: Path,
    boundary: str,
    blockage: bool,
    accepted: bool,
) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    text = path.read_text(encoding="utf-8")
    text = text.replace('bnd_type_bpg = "Rectangle"', f'bnd_type_bpg = "{boundary}"')
    text = text.replace("bpg_blockage_ratio = false", f"bpg_blockage_ratio = {str(blockage).lower()}")
    text = text.replace("flow_direction = [1.0, 1.0]", "flow_direction = [0.0, 0.0]")
    path.write_text(text, encoding="utf-8")

    if accepted:
        assert load_config(path).city_models.flow_direction == (0.0, 0.0)
    else:
        with pytest.raises(ConfigError, match="flow_direction must be non-zero"):
            load_config(path)


def test_programmatic_replacement_requires_shared_validation(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    config = load_config(path)
    replaced = replace(config, city_models=replace(config.city_models, building_percentile=101.0))

    with pytest.raises(ConfigError, match="city_models.building_percentile must be between 0 and 100 inclusive"):
        validate_config(replaced)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("terrain_thinning", 100.1, "between 0 and 100 inclusive"),
        ("building_percentile", -0.1, "between 0 and 100 inclusive"),
        ("complexity_factor", 1.1, "between 0 and 1 inclusive"),
    ],
)
def test_rejects_city_models_values_outside_closed_ranges(
    tmp_path: Path,
    field: str,
    value: float,
    message: str,
) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(f"{field} = {value}" if line.startswith(f"{field} = ") else line for line in lines) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize("field", ["iterations", "max_pts"])
def test_rejects_zero_city_models_smoothing_counts(tmp_path: Path, field: str) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(f"{field} = 0" if line.startswith(f"{field} = ") else line for line in lines) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=rf"city_models\.smooth_terrain\.{field} must be a positive integer"):
        load_config(path)


def test_rejects_non_finite_flow_direction_component(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "flow_direction = [1.0, 1.0]",
            "flow_direction = [nan, 1.0]",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"city_models\.flow_direction\[0\] must be finite"):
        load_config(path)


@pytest.mark.parametrize("invalid", [True, 1.5, float("inf"), float("nan")])
@pytest.mark.parametrize("field", ["iterations", "max_pts"])
def test_programmatic_smoothing_counts_require_positive_integers(
    tmp_path: Path,
    field: str,
    invalid: object,
) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    config = load_config(path)
    smooth = replace(config.city_models.smooth_terrain, **{field: invalid})
    replaced = replace(
        config,
        city_models=replace(config.city_models, smooth_terrain=smooth),
    )

    with pytest.raises(
        ConfigError,
        match=rf"city_models\.smooth_terrain\.{field} must be a positive integer",
    ):
        validate_config(replaced)


def test_programmatic_smoothing_counts_accept_positive_integers(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    config = load_config(path)
    smooth = replace(config.city_models.smooth_terrain, iterations=2, max_pts=1)
    replaced = replace(config, city_models=replace(config.city_models, smooth_terrain=smooth))

    assert validate_config(replaced).city_models.smooth_terrain == smooth


def test_rejects_negative_building_roof_default_base_height(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_complete_config(path, output_root=tmp_path / "outputs")
    configured = path.read_text(encoding="utf-8")
    path.write_text(
        configured.replace(
            "building_roof_default_base_height_m = 2.0",
            "building_roof_default_base_height_m = -0.1",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="building_roof_default_base_height_m must be non-negative"):
        load_config(path)


def test_florence_config_documents_unrelated_optional_usage() -> None:
    example = (ROOT / "config/examples/florence.toml").read_text(encoding="utf-8")

    for documented_setting in (
        "inner_diameter_m",
        "point_cloud_path",
        "enabled",
        "tree_canopy_overlay_path",
        "tree_terrain_geometry_path",
        "[[shapefiles.classification_rules]]",
        "match_any",
        "surface_precedence",
        "style",
        "sources = []",
    ):
        assert documented_setting in example
    assert '"1.2"' in example
    assert '"1.3"' in example
    assert '"2.2"' in example
    assert '"Rectangle", "Round", or "Oval"' in example
    assert '"obj", "stl", or "cityjson"' in example
    assert "not yet exposed by this application's TOML schema" in example


def test_allows_omitting_inner_diameter_for_uniform_roi(tmp_path: Path) -> None:
    path = tmp_path / "uniform.toml"
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        inner_diameter_m=None,
        outer_diameter_m=500.0,
    )

    config = load_config(path)

    assert config.region.inner_diameter_m is None
    assert config.region.outer_diameter_m == 500.0


def test_rejects_outer_diameter_smaller_than_inner_diameter(tmp_path: Path) -> None:
    path = tmp_path / "invalid.toml"
    path.write_text(
        """
[region]
name = "Invalid"
center_lat = 43.0
center_lon = 11.0
crs = "EPSG:25832"
inner_diameter_m = 500.0
outer_diameter_m = 300.0

[output]
root_directory = "outputs"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="outer_diameter_m"):
        load_config(path)


def test_rejects_negative_tree_overlap_tolerance(tmp_path: Path) -> None:
    path = tmp_path / "invalid.toml"
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        name="Invalid",
        center_lat=43.0,
        center_lon=11.0,
        inner_diameter_m=300.0,
        outer_diameter_m=500.0,
        tree_overlap_tolerance_m=-1.0,
    )

    with pytest.raises(ConfigError, match="tree_overlap_tolerance_m"):
        load_config(path)


def test_rejects_invalid_shapefile_classification_rule(tmp_path: Path) -> None:
    path = tmp_path / "invalid.toml"
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        shapefiles_block="""
[shapefiles]
[[shapefiles.classification_rules]]
category = "unsupported"
group_tag = "invalid"
match_any = ["amenity=parking"]
""".strip(),
    )

    with pytest.raises(ConfigError, match="classification_rules.*category"):
        load_config(path)


def test_rejects_empty_shapefile_classification_rules(tmp_path: Path) -> None:
    path = tmp_path / "invalid.toml"
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        shapefiles_block="""
[shapefiles]
classification_rules = []
""".strip(),
    )

    with pytest.raises(ConfigError, match="classification_rules must contain at least one rule"):
        load_config(path)


def test_allows_omitting_fallbacks_for_unconfigured_polygon_categories(tmp_path: Path) -> None:
    path = tmp_path / "minimal.toml"
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        shapefiles_block="""
[shapefiles]
surface_precedence = ["buildings:building_part", "buildings"]
[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building"
match_any = ["building"]
""".strip(),
    )

    config = load_config(path)

    assert config.shapefiles.surface_precedence == ("buildings:building_part", "buildings")


def test_rejects_missing_fallback_for_configured_polygon_category(tmp_path: Path) -> None:
    path = tmp_path / "invalid.toml"
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        shapefiles_block="""
[shapefiles]
surface_precedence = ["buildings"]
[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building"
match_any = ["building"]
[[shapefiles.classification_rules]]
category = "roads"
group_tag = "road"
match_any = ["highway"]
""".strip(),
    )

    with pytest.raises(ConfigError, match="configured polygon classification categories: roads"):
        load_config(path)


def test_loads_supplemental_shapefiles_and_multiple_planning_inputs(
    tmp_path: Path,
) -> None:
    config = load_config(write_complete_config(
        tmp_path / "config.toml",
        shapefiles_extra='''
[[shapefiles.supplemental]]
name = "municipal_trees"
path = "trees.shp"
crs = "EPSG:3003"
category = "trees"

[[urban_planning.inputs]]
name = "plan_a"
path = "plan-a.geojson"

[[urban_planning.inputs]]
name = "plan_b"
path = "plan-b.geojson"
crs = "EPSG:3857"
enabled = false
'''))

    assert config.shapefiles.supplemental[0].category == "trees"
    assert config.shapefiles.supplemental[0].path == tmp_path / "trees.shp"
    assert config.shapefiles.supplemental[0].enabled is True
    assert config.urban_planning.inputs[0].crs == "EPSG:4326"
    assert config.urban_planning.inputs[1].crs == "EPSG:3857"
    assert config.urban_planning.inputs[1].enabled is False


@pytest.mark.parametrize(
    ("config_extra", "error_match"),
    [
        (
            '''
[[shapefiles.supplemental]]
name = "duplicate"
path = "first.shp"
crs = "EPSG:4326"
category = "trees"
[[shapefiles.supplemental]]
name = "duplicate"
path = "second.shp"
crs = "EPSG:4326"
category = "trees"
''',
            "duplicate name: duplicate",
        ),
        (
            '''
[[urban_planning.inputs]]
name = "duplicate"
path = "first.geojson"
[[urban_planning.inputs]]
name = "duplicate"
path = "second.geojson"
''',
            "duplicate name: duplicate",
        ),
        (
            '''
[[shapefiles.supplemental]]
name = "unsupported"
path = "data.shp"
crs = "EPSG:4326"
category = "air_purifiers"
group_tag = "purifier"
''',
            r"supplemental\[1\]\.category must be one of",
        ),
        (
            '''
[[shapefiles.supplemental]]
name = "unsupported"
path = "data.shp"
crs = "EPSG:3857"
category = "trees"
''',
            r"supplemental\[1\]\.crs must be one of",
        ),
        (
            '''
[[urban_planning.inputs]]
name = "unsupported"
path = "plan.geojson"
crs = "EPSG:3003"
''',
            r"urban_planning\.inputs\[1\]\.crs must be one of",
        ),
        (
            '''
[[shapefiles.supplemental]]
name = "streets"
path = "streets.shp"
crs = "EPSG:4326"
category = "roads"
''',
            r"supplemental\[1\]\.group_tag is required",
        ),
        (
            '''
[[shapefiles.supplemental]]
name = "trees"
path = "trees.shp"
crs = "EPSG:4326"
category = "trees"
group_tag = "tree"
''',
            r"supplemental\[1\]\.group_tag is not allowed for trees",
        ),
        (
            '''
[[shapefiles.supplemental]]
name = "trees"
path = "trees.geojson"
crs = "EPSG:4326"
category = "trees"
''',
            r"supplemental\[1\]\.path must point to an ESRI \.shp file",
        ),
        (
            '''
[[urban_planning.inputs]]
name = "plan"
path = "plan.shp"
''',
            r"urban_planning\.inputs\[1\]\.path must point to a \.geojson file",
        ),
    ],
    ids=(
        "duplicate-supplemental-name",
        "duplicate-urban-planning-name",
        "unsupported-supplemental-category",
        "unsupported-supplemental-crs",
        "unsupported-urban-planning-crs",
        "missing-surface-group-tag",
        "tree-group-tag",
        "non-shapefile-supplemental-path",
        "non-geojson-urban-planning-path",
    ),
)
def test_rejects_invalid_supplemental_or_urban_planning_configuration(
    tmp_path: Path,
    config_extra: str,
    error_match: str,
) -> None:
    path = write_complete_config(tmp_path / "config.toml", shapefiles_extra=config_extra)

    with pytest.raises(ConfigError, match=error_match):
        load_config(path)


@pytest.mark.parametrize(
    "config_extra",
    [
        '[urban_planning]\npriority = 1',
        '[[urban_planning.inputs]]\nname = "plan"\npath = "plan.geojson"\npriority = 1',
    ],
)
def test_urban_planning_rejects_unknown_keys(
    tmp_path: Path,
    config_extra: str,
) -> None:
    path = write_complete_config(tmp_path / "config.toml", shapefiles_extra=config_extra)

    with pytest.raises(ConfigError, match="unknown configuration key: urban_planning"):
        load_config(path)


@pytest.mark.parametrize(
    ("input_lines", "shapefiles_extra", "removed_key"),
    [
        (
            ('streets_shapefile_path = "streets.shp"', 'streets_shapefile_crs = "EPSG:4326"'),
            "",
            "streets_shapefile_path",
        ),
        (
            ('green_areas_shapefile_path = "green.shp"', 'green_areas_shapefile_crs = "EPSG:4326"'),
            "",
            "green_areas_shapefile_path",
        ),
        (
            ('trees_shapefile_path = "trees.shp"', 'trees_shapefile_crs = "EPSG:4326"'),
            "",
            "trees_shapefile_path",
        ),
        (
            (),
            "[[shapefiles.user_surfaces]]\nname = 'old'\npath = 'old.shp'\ncrs = 'EPSG:4326'\ncategory = 'roads'\ngroup_tag = 'road'",
            "user_surfaces",
        ),
        (
            (),
            "[[shapefiles.user_trees]]\nname = 'old'\npath = 'old.shp'\ncrs = 'EPSG:4326'\nstatus = 'existing'",
            "user_trees",
        ),
        (
            (),
            "[[shapefiles.user_air_purifiers]]\nname = 'old'\npath = 'old.shp'\ncrs = 'EPSG:4326'\nstatus = 'existing'",
            "user_air_purifiers",
        ),
    ],
)
def test_rejects_removed_shapefile_configuration(
    tmp_path: Path,
    input_lines: tuple[str, ...],
    shapefiles_extra: str,
    removed_key: str,
) -> None:
    path = write_complete_config(
        tmp_path / "config.toml",
        input_lines=input_lines,
        shapefiles_extra=shapefiles_extra,
    )

    with pytest.raises(ConfigError, match=removed_key):
        load_config(path)


def test_allows_supplemental_specific_priority_without_category_rule_or_fallback(tmp_path: Path) -> None:
    path = tmp_path / "surface-only-roads.toml"
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        shapefiles_block="""
[shapefiles]
    surface_precedence = ["buildings", "supplemental:streets"]
    [[shapefiles.supplemental]]
name = "streets"
path = "streets.shp"
crs = "EPSG:4326"
category = "roads"
group_tag = "street_area"
[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building"
match_any = ["building"]
""".strip(),
    )

    config = load_config(path)

    assert "roads" not in config.shapefiles.surface_precedence
    assert "supplemental:streets" in config.shapefiles.surface_precedence


def test_rejects_surface_precedence_reference_to_undeclared_supplemental(tmp_path: Path) -> None:
    path = tmp_path / "invalid.toml"
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        shapefiles_block="""
[shapefiles]
surface_precedence = [
        "buildings", "water", "supplemental:missing", "green_areas",
    "roads", "concrete", "other_terrain",
]
[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building"
match_any = ["building"]
""".strip(),
    )

    with pytest.raises(ConfigError, match="undeclared supplemental input"):
        load_config(path)


def test_rejects_removed_legacy_surface_inputs(tmp_path: Path) -> None:
    path = tmp_path / "legacy.toml"
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        input_lines=(
            'streets_shapefile_path = "streets.shp"',
            'streets_shapefile_crs = "EPSG:3003"',
            'green_areas_shapefile_path = "green.shp"',
            'green_areas_shapefile_crs = "EPSG:4326"',
        ),
    )

    with pytest.raises(ConfigError, match="streets_shapefile"):
        load_config(path)


def test_loads_multiple_supplemental_tree_inputs(tmp_path: Path) -> None:
    path = tmp_path / "trees.toml"
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        shapefiles_block="""
[shapefiles]
surface_precedence = [
    "buildings", "water", "green_areas", "roads", "concrete", "other_terrain",
]
    [[shapefiles.supplemental]]
name = "inventory"
path = "inventory.shp"
crs = "EPSG:3003"
    category = "trees"
    [[shapefiles.supplemental]]
name = "market_plan"
path = "planned.shp"
crs = "EPSG:4326"
    category = "trees"
enabled = false
[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building"
match_any = ["building"]
""".strip(),
    )

    config = load_config(path)

    assert [(item.name, item.category, item.enabled) for item in config.shapefiles.supplemental] == [
        ("inventory", "trees", True),
        ("market_plan", "trees", False),
    ]


def test_loads_optional_air_purifier_stage_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        air_purifiers_block='''
[air_purifiers]
model_library_path = "models/parameters.json"
terrain_geometry_path = "terrain.obj"
''',
    )

    config = load_config(config_path)

    assert config.air_purifiers.model_library_path == tmp_path / "models/parameters.json"
    assert config.air_purifiers.terrain_geometry_path == tmp_path / "terrain.obj"


def test_existing_config_defaults_air_purifier_paths_to_none(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(config_path, output_root=tmp_path / "outputs")

    config = load_config(config_path)

    assert config.air_purifiers == AirPurifiersConfig(None, None)


@pytest.mark.parametrize(
    ("user_air_purifiers_block", "air_purifiers_block", "error_match"),
    [
        (
            '''
[[shapefiles.user_air_purifiers]]
name = "duplicate"
path = "first.shp"
crs = "EPSG:25832"
status = "existing"
[[shapefiles.user_air_purifiers]]
name = "duplicate"
path = "second.shp"
crs = "EPSG:25832"
status = "planned"
''',
            "",
            r"unknown configuration key: shapefiles\.user_air_purifiers",
        ),
        (
            '''
[[shapefiles.user_air_purifiers]]
name = "invalid_crs"
path = "purifiers.shp"
crs = "EPSG:3857"
status = "existing"
''',
            "",
            r"unknown configuration key: shapefiles\.user_air_purifiers",
        ),
        (
            '''
[[shapefiles.user_air_purifiers]]
name = "invalid_status"
path = "purifiers.shp"
crs = "EPSG:25832"
status = "removed"
''',
            "",
            r"unknown configuration key: shapefiles\.user_air_purifiers",
        ),
        (
            '''
[[shapefiles.user_air_purifiers]]
name = "invalid_path"
path = "purifiers.geojson"
crs = "EPSG:25832"
status = "existing"
''',
            "",
            r"unknown configuration key: shapefiles\.user_air_purifiers",
        ),
        (
            '''
[[shapefiles.user_air_purifiers]]
name = "invalid_enabled"
path = "purifiers.shp"
crs = "EPSG:25832"
status = "existing"
enabled = "yes"
''',
            "",
            r"unknown configuration key: shapefiles\.user_air_purifiers",
        ),
        (
            '''
[[shapefiles.user_air_purifiers]]
name = "unknown_key"
path = "purifiers.shp"
crs = "EPSG:25832"
status = "existing"
priority = 10
''',
            "",
            r"unknown configuration key: shapefiles\.user_air_purifiers",
        ),
        (
            "",
            '''
[air_purifiers]
model_library_path = "models/parameters.json"
unexpected = true
''',
            r"unknown configuration key: air_purifiers\.unexpected",
        ),
    ],
    ids=(
        "duplicate-name",
        "unsupported-crs",
        "unsupported-status",
        "non-shapefile-path",
        "non-boolean-enabled",
        "unknown-input-key",
        "unknown-stage-key",
    ),
)
def test_rejects_removed_air_purifier_input_or_invalid_stage_configuration(
    tmp_path: Path,
    user_air_purifiers_block: str,
    air_purifiers_block: str,
    error_match: str,
) -> None:
    config_path = tmp_path / "config.toml"
    write_complete_config(
        config_path,
        output_root=tmp_path / "outputs",
        shapefiles_block=DEFAULT_SHAPEFILES_BLOCK + user_air_purifiers_block,
        air_purifiers_block=air_purifiers_block,
    )

    with pytest.raises(ConfigError, match=error_match):
        load_config(config_path)


def test_rejects_removed_legacy_tree_input(tmp_path: Path) -> None:
    path = tmp_path / "legacy-tree.toml"
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        input_lines=(
            'trees_shapefile_path = "inventory.shp"',
            'trees_shapefile_crs = "EPSG:3003"',
        ),
        shapefiles_block="""
[shapefiles]
surface_precedence = [
    "buildings", "water", "green_areas", "roads", "concrete", "other_terrain",
]
[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building"
match_any = ["building"]
""".strip(),
    )

    with pytest.raises(ConfigError, match="trees_shapefile"):
        load_config(path)


def test_rejects_combined_removed_legacy_surface_configuration(tmp_path: Path) -> None:
    path = tmp_path / "legacy-conflict.toml"
    write_complete_config(
        path,
        output_root=tmp_path / "outputs",
        input_lines=(
            'streets_shapefile_path = "streets.shp"',
            'streets_shapefile_crs = "EPSG:4326"',
        ),
        shapefiles_block="""
[shapefiles]
surface_precedence = [
    "buildings", "water", "green_areas", "roads", "concrete", "other_terrain",
]
[[shapefiles.user_surfaces]]
name = "legacy_streets"
path = "another.shp"
crs = "EPSG:4326"
category = "roads"
group_tag = "street_area"
[[shapefiles.classification_rules]]
category = "buildings"
group_tag = "building"
match_any = ["building"]
""".strip(),
    )

    with pytest.raises(ConfigError, match="streets_shapefile"):
        load_config(path)


def test_rejects_missing_required_inputs_table(tmp_path: Path) -> None:
    path = tmp_path / "invalid.toml"
    path.write_text(
        """
[region]
name = "Invalid"
center_lat = 43.0
center_lon = 11.0
crs = "EPSG:25832"
inner_diameter_m = 300.0
outer_diameter_m = 500.0

[trees]
default = "Tilia"
model_library_path = "tree_categories.json"
category_mapping_path = "species_category_mapping.json"

[city_models]
lod = "2.2"
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

[city_models.smooth_terrain]
iterations = 1
max_pts = 100000

[city_models.reconstruction_region]
influence_region_m = 150.0
complexity_factor = 0.6
validate = true

[city_models.filters]
min_area = 4.0
min_height = 2.0

[imagery]
sources = []

[output]
root_directory = "outputs"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"missing required \[inputs\] table"):
        load_config(path)
