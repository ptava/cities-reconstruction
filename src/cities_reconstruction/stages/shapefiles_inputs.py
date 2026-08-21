"""Raw external-input adapters and parsers for the shapefiles stage."""

from __future__ import annotations

import json
import struct
import time
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from shapely.geometry import MultiPolygon, Polygon
from shapely.validation import make_valid

from cities_reconstruction.config import AppConfig, ConfigError, ImagerySourceConfig

SHAPEFILE_HEADER_BYTES = 100
SHAPEFILE_FILE_CODE = 9994
SHAPEFILE_VERSION = 1000
SHAPEFILE_NULL = 0
SHAPEFILE_POINT_TYPES = frozenset({1, 11, 21})
SHAPEFILE_MULTIPOINT_TYPES = frozenset({8, 18, 28})
SHAPEFILE_POLYGON_TYPES = frozenset({5, 15, 25})
TRANSIENT_OVERPASS_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
DBF_FIELD_TERMINATOR = 0x0D


def load_or_fetch_geometry_batches(
    config: AppConfig,
    output_dir: Path,
    overpass_json_path: Path | None,
    *,
    query: str,
    batch_queries: list[str],
) -> tuple[dict[str, Any], str]:
    """Load an explicit cache or fetch, cache, and merge Overpass batches."""

    if overpass_json_path is not None:
        return load_or_fetch_overpass(config, query, overpass_json_path)

    payloads: list[dict[str, Any]] = []
    for index, batch_query in enumerate(batch_queries, start=1):
        query_path = output_dir / f"overpass_query_batch_{index:02d}.txt"
        cache_path = output_dir / f"overpass_raw_batch_{index:02d}.json"
        query_path.write_text(batch_query, encoding="utf-8")
        if cache_path.exists():
            with cache_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        else:
            payload, _source = load_or_fetch_overpass(config, batch_query, None)
            cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        payloads.append(payload)
    return merge_overpass_payloads(payloads), (
        f"{config.inputs.overpass_url} ({len(payloads)} batched geometry requests)"
    )


def merge_overpass_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge Overpass payloads while de-duplicating typed element identities."""

    if not payloads:
        return {"elements": []}
    merged = dict(payloads[0])
    elements_by_id: dict[tuple[str, int], dict[str, Any]] = {}
    for payload in payloads:
        elements = payload.get("elements")
        if not isinstance(elements, list):
            raise ValueError("Overpass JSON must contain an 'elements' list")
        for element in elements:
            if not isinstance(element, dict):
                continue
            element_type = element.get("type")
            element_id = element.get("id")
            if isinstance(element_type, str) and isinstance(element_id, int):
                elements_by_id[(element_type, element_id)] = element
    merged["elements"] = list(elements_by_id.values())
    return merged


def load_or_fetch_overpass(
    config: AppConfig,
    query: str,
    overpass_json_path: Path | None,
    cached_source_label: str = "cached file",
) -> tuple[dict[str, Any], str]:
    """Read cached Overpass JSON or fetch it with configured retry behavior."""

    if overpass_json_path is not None:
        with overpass_json_path.open("r", encoding="utf-8") as handle:
            return json.load(handle), f"{cached_source_label}: {overpass_json_path}"

    payload = parse.urlencode({"data": query}).encode("utf-8")
    http_request = request.Request(
        config.inputs.overpass_url,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "cities-reconstruction/0.1.0",
        },
        method="POST",
    )
    failure: BaseException
    for attempt in range(1, config.inputs.overpass_max_attempts + 1):
        try:
            with request.urlopen(http_request, timeout=config.inputs.overpass_timeout_s) as response:
                return json.loads(response.read().decode("utf-8")), config.inputs.overpass_url
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            suffix = f": {detail[:500]}" if detail else ""
            message = f"failed to fetch Overpass data from {config.inputs.overpass_url}: HTTP {exc.code}{suffix}"
            retryable = exc.code in TRANSIENT_OVERPASS_HTTP_STATUS
            failure = exc
        except TimeoutError as exc:
            message = (
                f"Overpass request timed out after {config.inputs.overpass_timeout_s:g} seconds: "
                f"{config.inputs.overpass_url}"
            )
            retryable = True
            failure = exc
        except error.URLError as exc:
            message = f"failed to fetch Overpass data from {config.inputs.overpass_url}: {exc}"
            retryable = True
            failure = exc

        if not retryable or attempt == config.inputs.overpass_max_attempts:
            raise RuntimeError(message) from failure
        delay_s = config.inputs.overpass_retry_backoff_s * (2 ** (attempt - 1))
        if delay_s > 0:
            time.sleep(delay_s)

    raise AssertionError("unreachable Overpass retry state")


def read_polygon_records(path: Path, input_name: str) -> list[tuple[int, list[Polygon]]]:
    """Read and decode polygon records from one ESRI shapefile."""

    payload = path.read_bytes()
    _validate_polygon_header(payload, path, input_name)
    return _polygon_records(payload, path)


def read_point_records(
    path: Path,
    input_name: str,
) -> list[tuple[int, list[tuple[float, float]], bool]]:
    """Read and decode point or multipoint records from one ESRI shapefile."""

    payload = path.read_bytes()
    _validate_point_header(payload, path, input_name)
    return _point_records(payload, path)


def read_dbf_attributes(dbf_path: Path) -> list[dict[str, Any] | None]:
    """Read the optional DBF attribute table paired with a shapefile."""

    if not dbf_path.exists():
        return []
    payload = dbf_path.read_bytes()
    if len(payload) < 33:
        raise ConfigError(f"tree shapefile DBF attribute table is too small: {dbf_path}")
    record_count = struct.unpack("<I", payload[4:8])[0]
    header_length = struct.unpack("<H", payload[8:10])[0]
    record_length = struct.unpack("<H", payload[10:12])[0]
    if header_length > len(payload) or record_length <= 1:
        raise ConfigError(f"invalid tree shapefile DBF header: {dbf_path}")

    fields = _dbf_fields(payload, dbf_path)
    records: list[dict[str, Any] | None] = []
    offset = header_length
    for _record_index in range(record_count):
        if offset + record_length > len(payload):
            raise ConfigError(f"truncated tree shapefile DBF records: {dbf_path}")
        raw_record = payload[offset : offset + record_length]
        offset += record_length
        if raw_record[:1] == b"*":
            records.append(None)
            continue
        records.append(_dbf_record_attributes(raw_record, fields))
    return records


def fetch_imagery_diagnostics(
    config: AppConfig,
    output_dir: Path,
    bbox: tuple[float, float, float, float],
) -> dict[str, Any]:
    """Fetch configured WMS evidence and describe every source outcome."""

    diagnostics: dict[str, Any] = {
        "bbox_lon_lat": {
            "min_lon": bbox[0],
            "min_lat": bbox[1],
            "max_lon": bbox[2],
            "max_lat": bbox[3],
        },
        "assumptions": [
            "Imagery is diagnostic evidence only; it is not used to generate or fill geometry in this stage.",
            "The WMS request uses EPSG:4326 with a WMS 1.1.1 lon/lat bounding box around the outer ROI.",
            "Overlay geometry is drawn from integrated Overpass, supplemental shapefile, and urban-planning GeoJSON features; polygon gaps remain visible over the imagery.",
        ],
        "sources": [],
    }
    if not config.imagery.sources:
        return diagnostics

    imagery_dir = output_dir / "imagery"
    imagery_dir.mkdir(parents=True, exist_ok=True)
    for source in config.imagery.sources:
        source_record = _imagery_source_record(source)
        if not source.enabled:
            source_record["status"] = "disabled"
            diagnostics["sources"].append(source_record)
            continue

        url = build_wms_getmap_url(source, bbox)
        slug = imagery_source_slug(source.name)
        request_path = imagery_dir / f"{slug}_request.url"
        request_path.write_text(url, encoding="utf-8")
        image_path = imagery_dir / f"{slug}.{_image_extension(source.format)}"
        source_record.update(
            {
                "status": "requested",
                "request_url_path": str(request_path),
                "image_path": str(image_path),
            }
        )
        http_request = request.Request(
            url,
            headers={
                "Accept": source.format,
                "User-Agent": "cities-reconstruction/0.1.0",
            },
            method="GET",
        )
        try:
            with request.urlopen(http_request, timeout=max(10.0, config.inputs.overpass_timeout_s)) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            source_record.update({"status": "error", "error": f"HTTP {exc.code}: {detail[:500]}"})
        except error.URLError as exc:
            source_record.update({"status": "error", "error": str(exc)})
        else:
            if _looks_like_wms_error(payload, content_type):
                error_path = imagery_dir / f"{slug}_error.txt"
                error_path.write_text(payload.decode("utf-8", errors="replace")[:2000], encoding="utf-8")
                source_record.update(
                    {
                        "status": "error",
                        "error": "WMS returned a text/XML response instead of an image",
                        "error_path": str(error_path),
                        "content_type": content_type,
                    }
                )
            else:
                image_path.write_bytes(payload)
                source_record.update(
                    {
                        "status": "fetched",
                        "content_type": content_type,
                        "bytes": len(payload),
                        "width": source.width,
                        "height": source.height,
                    }
                )
        diagnostics["sources"].append(source_record)

    return diagnostics


def imagery_source_slug(value: str) -> str:
    """Return the stable file and artifact slug for an imagery source."""

    normalized = []
    for character in value.lower():
        if character.isalnum():
            normalized.append(character)
        elif normalized and normalized[-1] != "_":
            normalized.append("_")
    return "".join(normalized).strip("_") or "imagery"


def build_wms_getmap_url(
    source: ImagerySourceConfig,
    bbox: tuple[float, float, float, float],
) -> str:
    """Build a WMS 1.1.1 GetMap URL for one configured source."""

    parts = parse.urlsplit(source.url)
    query = parse.parse_qsl(parts.query, keep_blank_values=True)
    query.extend(
        [
            ("SERVICE", "WMS"),
            ("VERSION", "1.1.1"),
            ("REQUEST", "GetMap"),
            ("LAYERS", source.layer),
            ("STYLES", source.style),
            ("FORMAT", source.format),
            ("TRANSPARENT", "TRUE" if source.transparent else "FALSE"),
            ("SRS", source.crs),
            ("BBOX", ",".join(f"{value:.8f}" for value in bbox)),
            ("WIDTH", str(source.width)),
            ("HEIGHT", str(source.height)),
        ]
    )
    return parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parse.urlencode(query), parts.fragment))


def _validate_polygon_header(payload: bytes, path: Path, input_name: str) -> None:
    if len(payload) < SHAPEFILE_HEADER_BYTES:
        raise ConfigError(f"supplemental input '{input_name}' shapefile is too small to contain a valid header: {path}")
    file_code = struct.unpack(">i", payload[0:4])[0]
    version = struct.unpack("<i", payload[28:32])[0]
    shape_type = struct.unpack("<i", payload[32:36])[0]
    if file_code != SHAPEFILE_FILE_CODE or version != SHAPEFILE_VERSION:
        raise ConfigError(f"invalid ESRI shapefile header for supplemental input '{input_name}': {path}")
    if shape_type not in SHAPEFILE_POLYGON_TYPES:
        raise ConfigError(
            f"supplemental input '{input_name}' shapefile must contain Polygon records; "
            f"{path} has shapefile type {shape_type}"
        )


def _polygon_records(payload: bytes, path: Path) -> list[tuple[int, list[Polygon]]]:
    records: list[tuple[int, list[Polygon]]] = []
    offset = SHAPEFILE_HEADER_BYTES
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise ConfigError(f"truncated shapefile record header in polygon input: {path}")
        record_number, content_words = struct.unpack(">2i", payload[offset : offset + 8])
        offset += 8
        content_bytes = content_words * 2
        content = payload[offset : offset + content_bytes]
        if len(content) != content_bytes:
            raise ConfigError(f"truncated shapefile record payload in polygon input: {path}")
        offset += content_bytes
        records.append((record_number, _polygon_record(content, path, record_number)))
    return records


def _polygon_record(content: bytes, path: Path, record_number: int) -> list[Polygon]:
    if len(content) < 4:
        raise ConfigError(f"polygon shapefile record {record_number} has no shape type: {path}")
    shape_type = struct.unpack("<i", content[0:4])[0]
    if shape_type == SHAPEFILE_NULL:
        return []
    if shape_type not in SHAPEFILE_POLYGON_TYPES:
        raise ConfigError(f"polygon shapefile record {record_number} in {path} has unsupported shape type {shape_type}")
    if len(content) < 44:
        raise ConfigError(f"polygon shapefile record {record_number} is truncated: {path}")
    part_count, point_count = struct.unpack("<2i", content[36:44])
    parts_end = 44 + part_count * 4
    points_end = parts_end + point_count * 16
    if part_count <= 0 or point_count < 4 or len(content) < points_end:
        raise ConfigError(f"polygon shapefile record {record_number} has invalid part data: {path}")
    part_starts = list(struct.unpack(f"<{part_count}i", content[44:parts_end]))
    if part_starts[0] != 0 or part_starts != sorted(part_starts) or part_starts[-1] >= point_count:
        raise ConfigError(f"polygon shapefile record {record_number} has invalid part offsets: {path}")
    points = [struct.unpack("<2d", content[index : index + 16]) for index in range(parts_end, points_end, 16)]
    rings = [
        points[start:end]
        for start, end in zip(part_starts, [*part_starts[1:], point_count], strict=True)
        if end - start >= 4
    ]
    return _polygons_from_rings(rings)


def _polygons_from_rings(rings: list[list[tuple[float, float]]]) -> list[Polygon]:
    ring_polygons = [make_valid(Polygon(ring)) for ring in rings]
    simple_rings = [
        polygon for geometry in ring_polygons for polygon in _extract_polygons(geometry) if polygon.area > 0.0
    ]
    if not simple_rings:
        return []
    parents: list[int | None] = []
    for index, polygon in enumerate(simple_rings):
        containers = [
            (candidate.area, candidate_index)
            for candidate_index, candidate in enumerate(simple_rings)
            if candidate_index != index
            and candidate.area > polygon.area
            and candidate.covers(polygon.representative_point())
        ]
        parents.append(min(containers)[1] if containers else None)

    def depth(index: int) -> int:
        result = 0
        parent = parents[index]
        while parent is not None:
            result += 1
            parent = parents[parent]
        return result

    polygons: list[Polygon] = []
    for index, shell in enumerate(simple_rings):
        if depth(index) % 2:
            continue
        holes = [
            list(simple_rings[child].exterior.coords)
            for child, parent in enumerate(parents)
            if parent == index and depth(child) % 2 == 1
        ]
        polygons.extend(_extract_polygons(make_valid(Polygon(shell.exterior.coords, holes))))
    return polygons


def _validate_point_header(payload: bytes, path: Path, input_name: str) -> None:
    if len(payload) < SHAPEFILE_HEADER_BYTES:
        raise ConfigError(
            f"supplemental input '{input_name}' tree shapefile is too small to contain a valid header: {path}"
        )
    file_code = struct.unpack(">i", payload[0:4])[0]
    version = struct.unpack("<i", payload[28:32])[0]
    shape_type = struct.unpack("<i", payload[32:36])[0]
    if file_code != SHAPEFILE_FILE_CODE or version != SHAPEFILE_VERSION:
        raise ConfigError(f"invalid ESRI shapefile header for supplemental tree input '{input_name}': {path}")
    if shape_type not in SHAPEFILE_POINT_TYPES | SHAPEFILE_MULTIPOINT_TYPES:
        raise ConfigError(
            f"supplemental tree input '{input_name}' shapefile must contain Point or MultiPoint records; "
            f"{path} has shapefile type {shape_type}"
        )


def _point_records(
    payload: bytes,
    path: Path,
) -> list[tuple[int, list[tuple[float, float]], bool]]:
    records: list[tuple[int, list[tuple[float, float]], bool]] = []
    offset = SHAPEFILE_HEADER_BYTES
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise ConfigError(f"truncated shapefile record header in tree input: {path}")
        record_number, content_words = struct.unpack(">2i", payload[offset : offset + 8])
        offset += 8
        content_bytes = content_words * 2
        content = payload[offset : offset + content_bytes]
        if len(content) != content_bytes:
            raise ConfigError(f"truncated shapefile record payload in tree input: {path}")
        offset += content_bytes
        is_null = len(content) >= 4 and struct.unpack("<i", content[0:4])[0] == SHAPEFILE_NULL
        points = _point_record(content, path, record_number)
        records.append((record_number, points, is_null))
    return records


def _point_record(content: bytes, path: Path, record_number: int) -> list[tuple[float, float]]:
    if len(content) < 4:
        raise ConfigError(f"tree shapefile record {record_number} has no shape type: {path}")
    shape_type = struct.unpack("<i", content[0:4])[0]
    if shape_type == SHAPEFILE_NULL:
        return []
    if shape_type in SHAPEFILE_POINT_TYPES:
        if len(content) < 20:
            raise ConfigError(f"tree shapefile point record {record_number} is truncated: {path}")
        return [struct.unpack("<2d", content[4:20])]
    if shape_type in SHAPEFILE_MULTIPOINT_TYPES:
        if len(content) < 40:
            raise ConfigError(f"tree shapefile multipoint record {record_number} is truncated: {path}")
        point_count = struct.unpack("<i", content[36:40])[0]
        points_end = 40 + point_count * 16
        if point_count < 0 or len(content) < points_end:
            raise ConfigError(f"tree shapefile multipoint record {record_number} has invalid point data: {path}")
        return [struct.unpack("<2d", content[index : index + 16]) for index in range(40, points_end, 16)]
    raise ConfigError(
        "tree shapefile supports only Point and MultiPoint records; "
        f"record {record_number} in {path} has shape type {shape_type}"
    )


def _dbf_fields(payload: bytes, dbf_path: Path) -> list[tuple[str, str, int, int, int]]:
    fields: list[tuple[str, str, int, int, int]] = []
    offset = 32
    field_offset = 1
    while offset + 32 <= len(payload):
        descriptor = payload[offset : offset + 32]
        if descriptor[0] == DBF_FIELD_TERMINATOR:
            return fields
        name = descriptor[0:11].split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()
        field_type = chr(descriptor[11])
        field_length = descriptor[16]
        decimal_count = descriptor[17]
        if not name or field_length <= 0:
            raise ConfigError(f"invalid DBF field descriptor in tree attributes: {dbf_path}")
        fields.append((name, field_type, field_offset, field_length, decimal_count))
        field_offset += field_length
        offset += 32
    raise ConfigError(f"tree shapefile DBF header is missing a field terminator: {dbf_path}")


def _dbf_record_attributes(
    raw_record: bytes,
    fields: list[tuple[str, str, int, int, int]],
) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for name, field_type, offset, field_length, decimal_count in fields:
        raw_value = raw_record[offset : offset + field_length]
        value = _dbf_value(raw_value, field_type, decimal_count)
        if value is not None:
            attributes[name] = value
    return attributes


def _dbf_value(raw_value: bytes, field_type: str, decimal_count: int) -> Any:
    text = raw_value.decode("latin-1", errors="replace").strip()
    if not text:
        return None
    normalized_type = field_type.upper()
    if normalized_type in {"C", "D", "M"}:
        return text
    if normalized_type in {"N", "F", "B", "Y"}:
        try:
            if decimal_count == 0 and "." not in text and "," not in text:
                return int(text)
            return float(text.replace(",", "."))
        except ValueError:
            return text
    if normalized_type == "L":
        if text.upper() in {"Y", "T"}:
            return True
        if text.upper() in {"N", "F"}:
            return False
    return text


def _extract_polygons(geometry: Any) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if hasattr(geometry, "geoms"):
        return [polygon for item in geometry.geoms for polygon in _extract_polygons(item)]
    return []


def _imagery_source_record(source: ImagerySourceConfig) -> dict[str, Any]:
    return {
        "name": source.name,
        "type": source.type,
        "url": source.url,
        "layer": source.layer,
        "enabled": source.enabled,
        "crs": source.crs,
        "format": source.format,
        "width": source.width,
        "height": source.height,
    }


def _image_extension(image_format: str) -> str:
    normalized = image_format.lower()
    if "png" in normalized:
        return "png"
    if "jpeg" in normalized or "jpg" in normalized:
        return "jpg"
    return "img"


def _looks_like_wms_error(payload: bytes, content_type: str) -> bool:
    prefix = payload.lstrip()[:80].lower()
    return (
        "text" in content_type.lower()
        or "xml" in content_type.lower()
        or prefix.startswith(b"<?xml")
        or prefix.startswith(b"<html")
        or b"serviceexception" in prefix
    )
