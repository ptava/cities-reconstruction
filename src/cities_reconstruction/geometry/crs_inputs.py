"""Read CRS sidecar evidence independently of configuration and stages."""

from dataclasses import dataclass
from pathlib import Path

from cities_reconstruction.errors import ConfigError
from cities_reconstruction.geometry.crs import canonical_crs, crs_equal


def projection_sidecar(path: Path) -> Path | None:
    candidates = [item for item in (path.with_suffix(".prj"), path.with_suffix(".PRJ")) if item.is_file()]
    if len(candidates) > 1:
        raise ConfigError(f"ambiguous CRS sidecars for {path}: {candidates}")
    return candidates[0] if candidates else None


def source_crs(path: Path, declaration: str | None) -> str:
    """Resolve a declaration against adjacent projection metadata, rejecting conflicts."""
    declared = canonical_crs(declaration) if declaration is not None else None
    sidecar = projection_sidecar(path)
    if sidecar is not None:
        try:
            detected = canonical_crs(sidecar.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeError, ConfigError) as exc:
            raise ConfigError(f"cannot read source CRS from {sidecar}: {exc}") from exc
        if declared is not None and not crs_equal(declared, detected):
            raise ConfigError(f"source CRS conflict for {path}: declared {declared}, metadata {detected}")
        return detected
    if declared is not None:
        return declared
    raise ConfigError(f"missing source CRS for {path}; supply a CRS declaration or .prj sidecar")


@dataclass(frozen=True)
class RasterCRS:
    crs: str
    sidecars: tuple[Path, ...]


def raster_crs(directory: Path, declaration: str | None) -> RasterCRS:
    """Require every ASCII tile in a dataset to have one consistent horizontal CRS."""
    paths = sorted(path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() == ".asc")
    sidecars: list[Path] = []
    resolved = canonical_crs(declaration) if declaration is not None else None
    for path in paths:
        sidecar = projection_sidecar(path)
        current = source_crs(path, declaration)
        if resolved is not None and not crs_equal(resolved, current):
            raise ConfigError(f"source CRS conflict between raster tiles in {directory}: {resolved} and {current}")
        resolved = current
        if sidecar is not None:
            sidecars.append(sidecar)
    if resolved is None:
        raise ConfigError(f"missing source CRS for raster dataset {directory}; set its source CRS explicitly")
    return RasterCRS(resolved, tuple(sidecars))
