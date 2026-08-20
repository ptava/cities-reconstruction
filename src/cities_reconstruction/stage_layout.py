"""Stable pipeline-stage identities and filesystem layout metadata."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final


class StageId(StrEnum):
    """Stable stage identity independent of presentation order and layout."""

    SHAPEFILES = "shapefiles"
    VISUAL_ENRICHMENT = "visual-enrichment"
    POINT_CLOUD = "point-cloud"
    CITY_MODELS = "city-models"
    TREES = "trees"
    AIR_PURIFIERS = "air-purifiers"
    OPENFOAM = "openfoam"


@dataclass(frozen=True)
class StageLayout:
    """Stable identity and presentation number for one stage."""

    stage_id: StageId
    number: int

    @property
    def number_name(self) -> str:
        """Compose the numbered output name from identity and sequence."""

        stage_name = self.stage_id.value.replace("-", "_")
        return f"{self.number:02d}_{stage_name}"


STAGE_LAYOUTS: Final = (
    StageLayout(StageId.SHAPEFILES, 1),
    StageLayout(StageId.VISUAL_ENRICHMENT, 2),
    StageLayout(StageId.POINT_CLOUD, 3),
    StageLayout(StageId.CITY_MODELS, 4),
    StageLayout(StageId.TREES, 5),
    StageLayout(StageId.AIR_PURIFIERS, 6),
    StageLayout(StageId.OPENFOAM, 7),
)

STAGE_LAYOUT_BY_ID: Final = MappingProxyType(
    {layout.stage_id: layout for layout in STAGE_LAYOUTS}
)


def stage_output_directory(root: Path, stage_id: StageId) -> Path:
    """Return one stage's output directory beneath an output root."""

    return root / STAGE_LAYOUT_BY_ID[stage_id].number_name
