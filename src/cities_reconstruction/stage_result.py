"""Shared pipeline stage result types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StageResult:
    stage: str
    summary: str
    planned_actions: tuple[str, ...]
    expected_outputs: tuple[Path, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "summary": self.summary,
            "planned_actions": list(self.planned_actions),
            "expected_outputs": [str(path) for path in self.expected_outputs],
        }
