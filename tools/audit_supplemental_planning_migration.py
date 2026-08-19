#!/usr/bin/env python3
"""Fail when removed supplemental-planning contracts remain in active files."""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

TEXT_SUFFIXES = frozenset({".html", ".json", ".md", ".py", ".toml"})
SCANNED_DIRECTORIES = ("src", "tests", "tools", "config", "docs", "outputs")
SCANNED_TOP_LEVEL_FILES = ("README.md",)
HISTORICAL_DIRECTORY_EXCLUSIONS = {
    Path("docs/superpowers"): "approved design and implementation history, not active user documentation",
    Path(".superpowers"): "task briefs, reports, and review evidence retained as historical records",
}
REMOVED_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        "user_" + "surfaces",
        "user_" + "trees",
        "user_air_" + "purifiers",
        "planned_" + "trees",
        "planned_air_" + "purifiers",
        "build_mercato_centrale_(tree|air_purifier)_" + "plan",
        "planning " + "generator",
        r"layout\." + "json",
        "planning_" + "status",
        "planning-" + "status",
        "existing/" + "planned",
        "user_" + "surface:",
        "trees_shapefile_" + "path",
        "trees_shapefile_" + "crs",
        "user_" + "shapefile",
        "user_" + "green",
    )
)
NEGATIVE_CONFIG_TESTS = frozenset(
    {
        "test_rejects_removed_shapefile_configuration",
        "test_rejects_removed_air_purifier_input_or_invalid_stage_configuration",
        "test_rejects_combined_removed_legacy_surface_configuration",
        "test_rejects_removed_legacy_tree_input",
    }
)


def _candidate_paths(root: Path) -> list[Path]:
    candidates = [root / name for name in SCANNED_TOP_LEVEL_FILES]
    for directory in SCANNED_DIRECTORIES:
        base = root / directory
        if base.is_dir():
            candidates.extend(path for path in base.rglob("*") if path.is_file())
    return sorted(
        path
        for path in candidates
        if path.is_file()
        and path.suffix.lower() in TEXT_SUFFIXES
        and not _is_historical_exclusion(path.relative_to(root))
    )


def _is_historical_exclusion(relative_path: Path) -> bool:
    return any(relative_path == excluded or excluded in relative_path.parents for excluded in HISTORICAL_DIRECTORY_EXCLUSIONS)


def _function_spans(text: str) -> list[tuple[str, int, int]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return [
        (
            node.name,
            min((decorator.lineno for decorator in node.decorator_list), default=node.lineno),
            node.end_lineno or node.lineno,
        )
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _allowed_test_evidence(relative_path: Path, line_number: int, line: str, spans: list[tuple[str, int, int]]) -> bool:
    if relative_path == Path("tests/test_config.py"):
        return any(name in NEGATIVE_CONFIG_TESTS and start <= line_number <= end for name, start, end in spans)
    if relative_path in {
        Path("tests/test_air_purifiers_stage.py"),
        Path("tests/test_shapefiles_stage.py"),
    }:
        stripped = line.strip()
        removed_property = "planning_" + "status"
        return (
            bool(re.search(rf"assert .*['\"]{removed_property}['\"] not in", stripped))
            or (stripped.startswith("def test_") and f"without_{removed_property}" in stripped)
        )
    return False


def audit_repository(root: Path) -> list[str]:
    findings: list[str] = []
    for path in _candidate_paths(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        relative_path = path.relative_to(root)
        spans = _function_spans(text) if path.suffix == ".py" else []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not any(pattern.search(line) for pattern in REMOVED_PATTERNS):
                continue
            if _allowed_test_evidence(relative_path, line_number, line, spans):
                continue
            findings.append(f"{relative_path}:{line_number}:{line.strip()}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    findings = audit_repository(root)
    if findings:
        print("Stale supplemental-planning terms found in active files:")
        print("\n".join(findings))
        return 1
    print("PASS: no stale supplemental-planning terms in active source, tests, config, docs, or maintained outputs.")
    print("Explicit exclusions:")
    for path, rationale in HISTORICAL_DIRECTORY_EXCLUSIONS.items():
        print(f"- {path}: {rationale}")
    print("- narrow negative-test evidence: removed config keys and assertions that removed status metadata is absent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
