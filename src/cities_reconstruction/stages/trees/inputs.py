"""Input parsing and tree-model catalogue loading for the trees stage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cities_reconstruction.config import AppConfig, ConfigError


@dataclass(frozen=True)
class TreeSpeciesModel:
    name: str
    aliases: tuple[str, ...]
    default_height_m: float
    default_crown_radius_m: float
    default_trunk_radius_m: float
    crown_base_fraction: float
    crown_shape: str


def read_feature_collection(path: Path) -> list[dict[str, Any]]:
    """Read tree features while ignoring malformed non-feature entries."""

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    features = data.get("features")
    if not isinstance(features, list):
        raise ConfigError(f"GeoJSON feature collection missing features list: {path}")
    return [
        feature
        for feature in features
        if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)
    ]


def load_species_model_library(path: Path) -> list[TreeSpeciesModel]:
    """Load and validate the configured parametric tree-model catalogue."""

    if not path.exists():
        raise ConfigError(f"tree model library does not exist: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise ConfigError(f"tree model library must contain a models list: {path}")
    models: list[TreeSpeciesModel] = []
    for index, raw_model in enumerate(raw_models, start=1):
        if not isinstance(raw_model, dict):
            raise ConfigError(f"tree model library entry {index} must be an object: {path}")
        name = raw_model.get("name")
        aliases = raw_model.get("aliases", [])
        if not isinstance(name, str) or not name:
            raise ConfigError(f"tree model library entry {index} has no name: {path}")
        if not isinstance(aliases, list) or not all(isinstance(alias, str) and alias for alias in aliases):
            raise ConfigError(f"tree model library entry {name} aliases must be non-empty strings")
        models.append(
            TreeSpeciesModel(
                name=name,
                aliases=tuple(dict.fromkeys([name.lower(), *(alias.lower() for alias in aliases)])),
                default_height_m=_positive_float(raw_model, "default_height_m", name, path),
                default_crown_radius_m=_positive_float(raw_model, "default_crown_radius_m", name, path),
                default_trunk_radius_m=_positive_float(raw_model, "default_trunk_radius_m", name, path),
                crown_base_fraction=_fraction(raw_model, "crown_base_fraction", name, path),
                crown_shape=_optional_model_str(raw_model, "crown_shape", "ellipsoid"),
            )
        )
    return models


def configured_species_models(config: AppConfig) -> list[TreeSpeciesModel]:
    """Load the configured catalogue and require at least one usable model."""

    models = load_species_model_library(config.trees.model_library_path)
    if not models:
        raise ConfigError(
            f"tree model library must contain at least one model: {config.trees.model_library_path}"
        )
    return models


def match_category(
    category: str | None,
    models: list[TreeSpeciesModel],
) -> TreeSpeciesModel | None:
    """Resolve a model category or alias using normalized catalogue names."""

    if category is None:
        return None
    normalized = normalize_species_name(category)
    for model in models:
        if normalize_species_name(model.name) == normalized:
            return model
        if any(normalize_species_name(alias) == normalized for alias in model.aliases):
            return model
    return None


def _optional_model_str(raw_model: dict[str, Any], key: str, default: str) -> str:
    value = raw_model.get(key, default)
    if not isinstance(value, str) or not value:
        return default
    return value


def _positive_float(raw_model: dict[str, Any], key: str, name: str, path: Path) -> float:
    value = raw_model.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0.0:
        raise ConfigError(f"tree model {name} has invalid {key} in {path}")
    return float(value)


def _fraction(raw_model: dict[str, Any], key: str, name: str, path: Path) -> float:
    value = _positive_float(raw_model, key, name, path)
    if value >= 1.0:
        raise ConfigError(f"tree model {name} {key} must be less than 1.0 in {path}")
    return value


def species_category_mapping(config: AppConfig) -> dict[str, str]:
    """Load the optional normalized species-to-model-category mapping."""

    path = config.trees.category_mapping_path
    if path is None:
        return {}
    if not path.exists():
        raise ConfigError(f"tree species category mapping does not exist: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_mapping = payload.get("species_to_category")
    if not isinstance(raw_mapping, dict):
        raise ConfigError(f"tree species category mapping must contain species_to_category: {path}")
    mapping: dict[str, str] = {}
    for species, category in raw_mapping.items():
        if not isinstance(species, str) or not isinstance(category, str) or not category:
            raise ConfigError(f"invalid species/category entry in {path}")
        normalized_species = normalize_species_name(species)
        if normalized_species and normalized_species not in {
            "-",
            "--",
            "unknown",
            "sconosciuto",
            "non noto",
            "n/a",
            "da riconoscere",
        }:
            mapping[normalized_species] = category
    return mapping


def normalize_species_name(value: str) -> str:
    return " ".join(value.lower().replace('"', " ").replace("'", " ").split())
