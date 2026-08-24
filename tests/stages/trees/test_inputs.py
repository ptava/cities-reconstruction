import json
from pathlib import Path

from cities_reconstruction.stages.trees import inputs


def test_read_feature_collection_keeps_only_features_with_geometry(tmp_path: Path) -> None:
    path = tmp_path / "trees.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {"id": "kept"}, "geometry": {"type": "Point", "coordinates": [11.2, 43.7]}},
                    {"type": "Feature", "properties": {"id": "missing"}},
                    "invalid",
                ],
            }
        ),
        encoding="utf-8",
    )

    features = inputs.read_feature_collection(path)

    assert [feature["properties"]["id"] for feature in features] == ["kept"]


def test_load_species_model_library_normalizes_names_and_defaults_shape(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "name": "Round Broadleaf",
                        "aliases": ["Round", "ROUND"],
                        "default_height_m": 12.0,
                        "default_crown_radius_m": 3.0,
                        "default_trunk_radius_m": 0.25,
                        "crown_base_fraction": 0.4,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    models = inputs.load_species_model_library(path)

    assert models == [
        inputs.TreeSpeciesModel(
            name="Round Broadleaf",
            aliases=("round broadleaf", "round"),
            default_height_m=12.0,
            default_crown_radius_m=3.0,
            default_trunk_radius_m=0.25,
            crown_base_fraction=0.4,
            crown_shape="ellipsoid",
        )
    ]
