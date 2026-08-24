"""Deterministic count summaries for air-purifier diagnostics."""

from __future__ import annotations

from collections.abc import Iterable


def counts(values: Iterable[object]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[str(value)] = result.get(str(value), 0) + 1
    return dict(sorted(result.items()))
