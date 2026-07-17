from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = ROOT / "tools" / "audit_supplemental_planning_migration.py"


def _run_audit(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT), "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_audit_fails_when_an_active_file_contains_a_removed_contract(tmp_path: Path) -> None:
    assert AUDIT_SCRIPT.is_file()
    source = tmp_path / "src"
    source.mkdir()
    removed_contract = "shapefiles.user_" + "trees"
    (source / "stale.py").write_text(
        f"removed_table = {removed_contract!r}\n",
        encoding="utf-8",
    )

    result = _run_audit(tmp_path)

    assert result.returncode == 1
    assert "src/stale.py:1" in result.stdout


@pytest.mark.parametrize(
    "removed_term",
    [
        "trees_shapefile_" + "path",
        "trees_shapefile_" + "crs",
        "user_" + "shapefile",
        "user_" + "green",
    ],
)
def test_audit_fails_for_every_removed_runtime_identifier(tmp_path: Path, removed_term: str) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "stale.py").write_text(f"value = {removed_term!r}\n", encoding="utf-8")

    result = _run_audit(tmp_path)

    assert result.returncode == 1
    assert removed_term in result.stdout


def test_audit_passes_for_current_terms_and_documented_negative_assertions(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Use urban_planning.inputs.\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    removed_property = "planning_" + "status"
    (tests / "test_air_purifiers_stage.py").write_text(
        f'assert {removed_property!r} not in properties\n',
        encoding="utf-8",
    )
    removed_table = "shapefiles.user_" + "trees"
    (tests / "test_config.py").write_text(
        "import pytest\n\n"
        f"@pytest.mark.parametrize('legacy', [{removed_table!r}])\n"
        "def test_rejects_removed_shapefile_configuration(legacy):\n"
        "    assert legacy\n",
        encoding="utf-8",
    )

    result = _run_audit(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no stale supplemental-planning terms" in result.stdout.lower()
