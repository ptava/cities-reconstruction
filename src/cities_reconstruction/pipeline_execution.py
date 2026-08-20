"""Resolve and execute dependency-aware multi-stage pipeline runs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .config import AppConfig, ConfigError
from .pipeline import STAGE_BY_NAME, STAGE_SPECS, StageSelection
from .stage_contract import JsonValue, StageOutput, StageStatus
from .stage_layout import StageId
from .stage_runtime import StageRunOptions


@dataclass(frozen=True)
class ExecutionPlan:
    """An immutable dependency-ordered collection of pipeline stages."""

    stage_ids: tuple[StageId, ...]

    @property
    def stage_names(self) -> tuple[str, ...]:
        """Return stable stage names in execution order."""

        return tuple(stage_id.value for stage_id in self.stage_ids)


@dataclass(frozen=True)
class PipelineExecution:
    """Typed results produced while executing one resolved plan."""

    plan: ExecutionPlan
    results: tuple[StageOutput, ...]

    @property
    def completed(self) -> bool:
        """Return whether every planned stage completed successfully."""

        return len(self.results) == len(self.plan.stage_ids) and all(
            result.status is StageStatus.COMPLETED for result in self.results
        )

    def to_dict(self) -> dict[str, JsonValue]:
        """Return the aggregate execution summary as JSON-safe values."""

        return {
            "plan": list(self.plan.stage_names),
            "results": [result.to_dict() for result in self.results],
        }


def resolve_execution_plan(
    *,
    target: str | None = None,
    includes: Iterable[str] = (),
    supplied_overrides: Iterable[str] = (),
) -> ExecutionPlan:
    """Resolve default or targeted work into dependency-safe execution order."""

    include_names = tuple(includes)
    override_names = frozenset(supplied_overrides)
    roots = _execution_roots(target=target, includes=include_names)
    resolved: list[StageId] = []
    resolved_set: set[StageId] = set()
    visiting: list[StageId] = []

    def visit(stage_id: StageId) -> None:
        if stage_id in resolved_set:
            return
        if stage_id in visiting:
            cycle = visiting[visiting.index(stage_id) :] + [stage_id]
            names = " -> ".join(item.value for item in cycle)
            raise ConfigError(f"pipeline dependency cycle: {names}")

        spec = STAGE_BY_NAME[stage_id.value]
        if not spec.executable:
            raise ConfigError(f"pipeline stage is not executable: {spec.name}")

        visiting.append(stage_id)
        dependencies = list(spec.hard_dependencies)
        dependencies.extend(
            item.default_producer
            for item in spec.inputs
            if item.required
            and item.default_producer is not None
            and item.override not in override_names
        )
        for dependency in dependencies:
            visit(dependency)
        visiting.pop()
        resolved.append(stage_id)
        resolved_set.add(stage_id)

    for root in roots:
        visit(root)
    return ExecutionPlan(tuple(resolved))


def execute_pipeline(
    config: AppConfig,
    plan: ExecutionPlan,
    options: StageRunOptions,
) -> PipelineExecution:
    """Execute a resolved plan sequentially and stop at the first failure."""

    results: list[StageOutput] = []
    for stage_id in plan.stage_ids:
        spec = STAGE_BY_NAME[stage_id.value]
        runner = spec.runner
        if runner is None:
            raise ConfigError(f"pipeline stage is not executable: {spec.name}")
        result = runner(config, options)
        results.append(result)
        if result.status is not StageStatus.COMPLETED:
            break
    return PipelineExecution(plan=plan, results=tuple(results))


def _execution_roots(*, target: str | None, includes: tuple[str, ...]) -> tuple[StageId, ...]:
    if target is None:
        roots = [
            spec.stage_id
            for spec in STAGE_SPECS
            if spec.selection is StageSelection.DEFAULT and spec.executable
        ]
    else:
        target_spec = STAGE_BY_NAME.get(target)
        if target_spec is None:
            raise ConfigError(f"unknown pipeline stage: {target}")
        if not target_spec.executable:
            raise ConfigError(f"pipeline stage is not executable: {target}")
        roots = [target_spec.stage_id]

    for name in includes:
        spec = STAGE_BY_NAME.get(name)
        if spec is None:
            raise ConfigError(f"unknown optional pipeline stage: {name}")
        if spec.selection is not StageSelection.OPTIONAL:
            raise ConfigError(f"pipeline stage is not optional: {name}")
        if not spec.executable:
            raise ConfigError(f"pipeline stage is not executable: {name}")
        roots.append(spec.stage_id)
    return tuple(roots)
