import json
from pathlib import Path

from cities_reconstruction.stages.air_purifiers import inputs


def test_load_features_returns_normalized_feature_collection_entries(tmp_path: Path) -> None:
    path = tmp_path / "air_purifiers.geojson"
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [11.2, 43.7]},
        "properties": {"purifier_id": "AP-1"},
    }
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": [feature]}),
        encoding="utf-8",
    )

    assert inputs.load_features(path) == [feature]
