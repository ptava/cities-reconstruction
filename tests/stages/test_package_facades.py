import ast
from importlib import import_module
from pathlib import Path

import pytest

from cities_reconstruction.stage_layout import StageId

STAGE_FACADES = (
    ("shapefiles", StageId.SHAPEFILES, ("ShapefilesStageOutput", "plan", "run")),
    (
        "visual_enrichment",
        StageId.VISUAL_ENRICHMENT,
        ("VisualEnrichmentStageOutput", "plan", "run"),
    ),
    ("point_cloud", StageId.POINT_CLOUD, ("PointCloudStageOutput", "plan", "run")),
    ("city_models", StageId.CITY_MODELS, ("CityModelsStageOutput", "plan", "run")),
    ("trees", StageId.TREES, ("TreesStageOutput", "plan", "run")),
    (
        "air_purifiers",
        StageId.AIR_PURIFIERS,
        ("AirPurifiersStageOutput", "plan", "run"),
    ),
    ("openfoam", StageId.OPENFOAM, ("plan",)),
)


def _private_stage_import_targets(test_path: Path) -> list[str]:
    tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
    targets: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(
                name.name
                for name in node.names
                if _is_private_stage_module(name.name)
            )
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if _is_private_stage_module(node.module):
                targets.append(node.module)
            elif any(name.name == "stage" for name in node.names):
                candidate = f"{node.module}.stage"
                if _is_private_stage_module(candidate):
                    targets.append(candidate)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and _is_private_stage_module(node.args[0].value)
        ):
            targets.append(node.args[0].value)

    return targets


def _is_private_stage_module(module_name: str) -> bool:
    parts = module_name.split(".")
    return len(parts) >= 4 and parts[:2] == ["cities_reconstruction", "stages"] and parts[3] == "stage"


@pytest.mark.parametrize(("module_name", "stage_id", "exports"), STAGE_FACADES)
def test_stage_is_package_with_public_facade(
    module_name: str,
    stage_id: StageId,
    exports: tuple[str, ...],
) -> None:
    stage = import_module(f"cities_reconstruction.stages.{module_name}")

    assert hasattr(stage, "__path__")
    assert stage.STAGE_ID is stage_id
    for export in exports:
        assert hasattr(stage, export)


def test_stage_tests_do_not_import_another_stages_private_stage_module() -> None:
    stage_test_root = Path(__file__).parent
    violations: list[str] = []

    for test_path in sorted(stage_test_root.glob("*/test_*.py")):
        owner_stage = test_path.parent.name
        for target in _private_stage_import_targets(test_path):
            target_stage = target.split(".")[2]
            if target_stage != owner_stage:
                violations.append(f"{test_path.relative_to(stage_test_root.parent)} -> {target}")

    assert violations == []
