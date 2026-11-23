"""Adapters for the separately implemented module and review workflows."""

from __future__ import annotations

from contextlib import nullcontext
from importlib import import_module
from typing import TYPE_CHECKING, Any

from brickwright.tools.diagnostics import Diagnostic, exception_diagnostic

if TYPE_CHECKING:
    from pathlib import Path

    from bricklayer import LegoBuild

    from brickwright.record import Record


def _extension(name: str):
    module = f"brickwright.{name}"
    try:
        return import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name != module:
            raise
        return None


def _unavailable(record: Record) -> Diagnostic:
    return Diagnostic(
        code="flow.modules",
        stage="flow",
        message="Module validation is unavailable for this build.",
        next_action=f"Check the module setup for record {record.id}.",
    )


def validate_contracts(record: Record, plan: dict) -> list[Diagnostic]:
    modules = _extension("modules")
    if modules is None:
        return [_unavailable(record)]
    try:
        modules.validate_contracts(plan)
    except ValueError as exc:
        return [exception_diagnostic(exc, "plan", code="flow.modules")]
    return []


def prepare_merge(record: Record) -> Any:
    modules = _extension("modules")
    return modules.prepare_merge(record) if modules is not None else None


def merge_context(merge: Any):
    if merge is None:
        return nullcontext()
    modules = _extension("modules")
    if modules is None:
        raise RuntimeError("The module workflow is unavailable.")
    return modules.merge_context(merge)


def check_module(
    build: LegoBuild, record_path: Path
) -> tuple[str | None, list[Diagnostic]]:
    if (
        record_path.parent.name != "modules"
        or not (record_path.parent.parent / "meta.json").is_file()
    ):
        return None, []
    modules = _extension("modules")
    if modules is None:
        return None, [
            Diagnostic(
                code="flow.modules",
                stage="validate",
                message="Module validation is unavailable.",
            )
        ]
    return modules.check_module(build, record_path)


def merge_evidence(
    build: LegoBuild, merge: Any
) -> tuple[list[dict[str, Any]], list[Diagnostic]]:
    if merge is None:
        return [], []
    modules = _extension("modules")
    if modules is None:
        return [], [
            Diagnostic(
                code="flow.modules",
                stage="structure",
                message="Module validation is unavailable.",
            )
        ]
    return modules.merge_evidence(build, merge)


def merge_state(record: Record, *, needed: bool) -> tuple[bool, list[Diagnostic]]:
    modules = _extension("modules")
    if modules is None:
        return (False, [_unavailable(record)]) if needed else (True, [])
    try:
        return modules.merge_state(record)
    except (OSError, ValueError) as exc:
        return False, [exception_diagnostic(exc, "flow", code="flow.modules")]


def review_status(record: Record, snapshot_id: str) -> dict | None:
    review = _extension("aligned_review")
    return review.review_status(record, snapshot_id) if review is not None else None


def delivery_review_current(record: Record) -> bool:
    """Verify live review references while resolving the persisted finalization marker."""
    review = _extension("aligned_review")
    if review is None:
        return False
    import json

    try:
        report = json.loads((record.path / "review.json").read_bytes())
        return review.review_inputs_current(record, report["round_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
