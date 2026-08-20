from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cities_reconstruction.artifacts import (
    atomic_text_writer,
    lightweight_state_fingerprint,
    stage_output_lock,
)
from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.stages import city_models, shapefiles
from tests.config_helpers import write_complete_config


def test_stage_output_lock_rejects_concurrent_writer_and_releases(tmp_path: Path) -> None:
    output_dir = tmp_path / "stage"

    with stage_output_lock(output_dir, "fixture"):
        with pytest.raises(ConfigError, match="locked by another run"):
            with stage_output_lock(output_dir, "fixture"):
                pass

    with stage_output_lock(output_dir, "fixture"):
        assert (output_dir / ".stage.lock").exists()
    assert not (output_dir / ".stage.lock").exists()


def test_interrupted_atomic_write_preserves_previous_file_and_cleans_temp(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("old\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="interrupted"):
        with atomic_text_writer(path) as handle:
            handle.write("new\n")
            raise RuntimeError("interrupted")

    assert path.read_text(encoding="utf-8") == "old\n"
    assert list(tmp_path.glob(".manifest.json.*.tmp")) == []


def test_lightweight_fingerprint_is_canonical_and_metadata_sensitive(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    payload = {"stage": "fixture", "settings": {"b": 2, "a": 1}}

    original = lightweight_state_fingerprint(payload, [second, first])
    reordered = lightweight_state_fingerprint(payload, [first, second])
    second.write_text("changed", encoding="utf-8")
    changed = lightweight_state_fingerprint(payload, [first, second])

    assert original["value"] == reordered["value"]
    assert original["value"] != changed["value"]
    assert "contents are not hashed" in original["limitation"]


def test_known_mesh_cleanup_unlinks_symlink_without_touching_target(tmp_path: Path) -> None:
    output_dir = tmp_path / "04_city_models"
    generated_dir = output_dir / "city4cfd_output"
    generated_dir.mkdir(parents=True)
    external_target = tmp_path / "external.obj"
    external_target.write_text("user data\n", encoding="utf-8")
    link = generated_dir / "Mesh_Buildings.obj"
    os.symlink(external_target, link)

    city_models._remove_known_city4cfd_outputs(output_dir, ["Mesh_Buildings.obj"])

    assert not link.exists()
    assert external_target.read_text(encoding="utf-8") == "user data\n"


def test_known_mesh_cleanup_rejects_non_file_and_path_escape(tmp_path: Path) -> None:
    output_dir = tmp_path / "04_city_models"
    generated_dir = output_dir / "city4cfd_output"
    (generated_dir / "Mesh_Terrain.obj").mkdir(parents=True)

    with pytest.raises(ConfigError, match="is a directory"):
        city_models._remove_known_city4cfd_outputs(output_dir, ["Mesh_Terrain.obj"])
    with pytest.raises(ConfigError, match="invalid City4CFD output filename"):
        city_models._remove_known_city4cfd_outputs(output_dir, ["../outside.obj"])


def test_shapefiles_stage_publishes_stable_category_and_reference_artifact_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    cached = tmp_path / "overpass.json"
    write_complete_config(config_path, output_root=tmp_path / "outputs")
    cached.write_text(json.dumps({"elements": []}), encoding="utf-8")

    result = shapefiles.run(load_config(config_path), overpass_json_path=cached)

    expected = tmp_path / "outputs" / "01_shapefiles" / "air_purifiers.geojson"
    artifacts = {artifact["name"]: artifact for artifact in result.to_dict()["artifacts"]}
    assert result.air_purifiers_path == expected
    assert artifacts["air-purifiers"]["path"] == str(expected)
    assert expected.exists()
    assert result.urban_planning_path == expected.with_name("urban_planning.geojson")
    assert artifacts["urban-planning"]["path"] == str(expected.with_name("urban_planning.geojson"))
    assert json.loads(result.urban_planning_path.read_text(encoding="utf-8"))["features"] == []
    assert result.category_paths["trees"] == expected.with_name("trees.geojson")
    assert result.category_paths["roads"] == expected.with_name("roads.geojson")
    assert artifacts["category-trees"]["path"] == str(expected.with_name("trees.geojson"))
