from __future__ import annotations

import json
import time
from io import BytesIO
from pathlib import Path
from urllib import error

import pytest

from cities_reconstruction.config import ConfigError, load_config
from cities_reconstruction.stages import shapefiles_inputs
from tests.config_helpers import write_complete_config


def test_merge_overpass_payloads_deduplicates_elements_by_type_and_id() -> None:
    merged = shapefiles_inputs.merge_overpass_payloads(
        [
            {"version": 0.6, "elements": [{"type": "node", "id": 7, "tags": {"name": "old"}}]},
            {
                "elements": [
                    {"type": "node", "id": 7, "tags": {"name": "new"}},
                    {"type": "way", "id": 7},
                    "ignored",
                ]
            },
        ]
    )

    assert merged == {
        "version": 0.6,
        "elements": [
            {"type": "node", "id": 7, "tags": {"name": "new"}},
            {"type": "way", "id": 7},
        ],
    }


def test_load_or_fetch_overpass_reads_the_explicit_cache(tmp_path: Path) -> None:
    config_path = write_complete_config(tmp_path / "config.toml")
    cached_path = tmp_path / "cached.json"
    cached_path.write_text(json.dumps({"elements": [{"type": "node", "id": 1}]}), encoding="utf-8")

    payload, source = shapefiles_inputs.load_or_fetch_overpass(
        load_config(config_path),
        "out;",
        cached_path,
    )

    assert payload == {"elements": [{"type": "node", "id": 1}]}
    assert source == f"cached file: {cached_path}"


def test_load_or_fetch_overpass_retries_transient_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_complete_config(tmp_path / "config.toml", output_root=tmp_path / "outputs")
    config = load_config(config_path)
    responses = iter(
        [
            error.HTTPError(
                config.inputs.overpass_url,
                504,
                "Gateway Timeout",
                hdrs=None,
                fp=BytesIO(b"temporarily busy"),
            ),
            TimeoutError("read operation timed out"),
            BytesIO(b'{"elements": []}'),
        ]
    )
    sleeps: list[float] = []

    def fake_urlopen(_request, timeout):
        assert timeout == config.inputs.overpass_timeout_s
        response = next(responses)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(shapefiles_inputs.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    payload, source = shapefiles_inputs.load_or_fetch_overpass(config, "out;", None)

    assert payload == {"elements": []}
    assert source == config.inputs.overpass_url
    assert sleeps == [2.0, 4.0]


def test_load_or_fetch_overpass_reports_timeout_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_complete_config(tmp_path / "config.toml", output_root=tmp_path / "outputs")
    text = config_path.read_text(encoding="utf-8").replace(
        "overpass_timeout_s = 60.0",
        "overpass_timeout_s = 60.0\noverpass_max_attempts = 1",
    )
    config_path.write_text(text, encoding="utf-8")
    config = load_config(config_path)
    monkeypatch.setattr(
        shapefiles_inputs.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("read operation timed out")),
    )

    with pytest.raises(RuntimeError, match="Overpass request timed out after 60 seconds"):
        shapefiles_inputs.load_or_fetch_overpass(config, "out;", None)


def test_read_point_records_rejects_a_truncated_shapefile(tmp_path: Path) -> None:
    path = tmp_path / "trees.shp"
    path.write_bytes(b"not-a-shapefile")

    with pytest.raises(
        ConfigError,
        match="supplemental input 'municipal_trees' tree shapefile is too small",
    ):
        shapefiles_inputs.read_point_records(path, "municipal_trees")


def test_build_wms_getmap_url_preserves_existing_query_and_uses_requested_bbox(tmp_path: Path) -> None:
    config_path = write_complete_config(
        tmp_path / "config.toml",
        imagery_block="""[[imagery.sources]]
name = "Comune ortho"
type = "wms"
url = "https://example.test/wms?token=abc"
layer = "ortho"
enabled = true
crs = "EPSG:4326"
format = "image/png"
width = 32
height = 24
transparent = false""",
    )
    source = load_config(config_path).imagery.sources[0]

    url = shapefiles_inputs.build_wms_getmap_url(source, (11.0, 43.0, 12.0, 44.0))

    assert url == (
        "https://example.test/wms?token=abc&SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS=ortho&"
        "STYLES=&FORMAT=image%2Fpng&TRANSPARENT=FALSE&SRS=EPSG%3A4326&"
        "BBOX=11.00000000%2C43.00000000%2C12.00000000%2C44.00000000&WIDTH=32&HEIGHT=24"
    )
