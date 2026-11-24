"""Current flow state and immutable structural snapshot storage."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from brickwright.actions import RECORD, run
from brickwright.tools.diagnostics import Diagnostic

from .artifacts import (
    VERSION,
    Plan,
    artifact_error,
    atomic_write,
    document_bytes,
    file_sha256,
    load_artifact,
    read_conditions,
)
from .integrations import merge_state, review_status
from .structure import StructureReport, catalog_sha256, sdk_version

if TYPE_CHECKING:
    from brickwright.record import Record


class StructureInputs(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_sha256: str
    plan_sha256: str | None
    manifest_sha256: str | None
    catalog_sha256: str
    sdk_version: str

    @classmethod
    def capture(cls, record: Record, source_sha256: str) -> StructureInputs:
        return cls(
            source_sha256=source_sha256,
            plan_sha256=file_sha256(record.path / "plan.json"),
            manifest_sha256=file_sha256(record.path / "merge-manifest.json"),
            catalog_sha256=catalog_sha256(),
            sdk_version=sdk_version(),
        )

    def current(self, record: Record) -> bool:
        try:
            return self.capture(record, record.source_sha256) == self
        except OSError:
            return False


class PublishedStructure(BaseModel):
    schema_version: int = VERSION
    snapshot_id: str
    inputs: StructureInputs
    mpd_sha256: str | None
    structure_sha256: str
    native_passed: bool


def _pointer(record: Record) -> Path:
    return record.path / "flow" / "current.json"


def _snapshot_dir(record: Record, snapshot_id: str) -> Path:
    if len(snapshot_id) != 64 or any(
        char not in "0123456789abcdef" for char in snapshot_id
    ):
        raise ValueError("Invalid flow snapshot ID")
    return record.path / "flow" / "snapshots" / snapshot_id


def _snapshot_id(inputs: StructureInputs, mpd_sha256: str | None) -> str:
    key = {"inputs": inputs.model_dump(mode="json"), "mpd": mpd_sha256}
    return sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


def save_structure(
    record: Record,
    inputs: StructureInputs,
    structure: StructureReport,
    mpd: bytes | None,
    native_passed: bool,
) -> None:
    if not inputs.current(record):
        return
    snapshot_id = _snapshot_id(inputs, structure.mpd_sha256)
    directory = _snapshot_dir(record, snapshot_id)
    if mpd is not None:
        atomic_write(directory / "build.ldr", mpd)
    report_bytes = document_bytes(structure)
    atomic_write(directory / "structure.json", report_bytes)
    pointer = PublishedStructure(
        snapshot_id=snapshot_id,
        inputs=inputs,
        mpd_sha256=structure.mpd_sha256,
        structure_sha256=sha256(report_bytes).hexdigest(),
        native_passed=native_passed,
    )
    if inputs.current(record):
        atomic_write(_pointer(record), document_bytes(pointer))


def _current_structure(
    record: Record,
) -> tuple[PublishedStructure | None, StructureReport | None, Path | None]:
    try:
        pointer = PublishedStructure.model_validate_json(_pointer(record).read_bytes())
        if (
            pointer.schema_version != VERSION
            or pointer.snapshot_id != _snapshot_id(pointer.inputs, pointer.mpd_sha256)
            or not pointer.inputs.current(record)
        ):
            return pointer, None, None
        directory = _snapshot_dir(record, pointer.snapshot_id)
        mpd = directory / "build.ldr"
        report_bytes = (directory / "structure.json").read_bytes()
        if sha256(report_bytes).hexdigest() != pointer.structure_sha256:
            return pointer, None, None
        if file_sha256(mpd) != pointer.mpd_sha256:
            return pointer, None, None
        report = StructureReport.model_validate_json(report_bytes)
        if report.mpd_sha256 != pointer.mpd_sha256:
            return pointer, None, None
        return pointer, report, mpd if pointer.mpd_sha256 else None
    except (OSError, ValueError):
        return None, None, None


def delivery_stamp(
    record: Record, prompt_image: str | None, *, output_sha256: str | None = None
) -> str:
    """Hash the delivery evidence, optionally using the intended output digest."""
    paths = [
        "task.json",
        "conditions.json",
        "features.json",
        "plan.json",
        "merge-manifest.json",
        "flow/current.json",
        "review.json",
    ]
    if prompt_image:
        paths.append(prompt_image)
    manifest = {name: file_sha256(record.path / name) for name in paths}
    manifest["out/build.ldr"] = output_sha256 or file_sha256(record.ldr_path)
    return sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def flow_status(record: Record, *, include_review: bool = True) -> dict[str, Any]:
    meta = record.read_meta()
    empty_review = {
        "passed": False,
        "rounds": 0,
        "remaining_items": [],
        "current": False,
        "dispatch": None,
    }
    if meta.flow is None:
        return {
            "enabled": False,
            "ready": meta.finalized,
            "diagnostics": [],
            "snapshot_id": None,
            "mpd_path": None,
            "mpd_sha256": None,
            "structural_passed": False,
            "artifacts": {},
            "review": empty_review,
            "limitations": [],
            "finalized": meta.finalized,
            "review_outcome": None,
            "structure": None,
        }

    diagnostics = []
    try:
        read_conditions(record)
    except (OSError, ValueError) as exc:
        diagnostics.append(
            artifact_error(record, "conditions", str(exc), path="$.checks")
        )
    features, feature_issues = load_artifact(record, "features")
    plan, plan_issues = load_artifact(record, "plan")
    diagnostics.extend(feature_issues)
    diagnostics.extend(plan_issues)
    module_ok, module_issues = merge_state(
        record,
        needed=bool(isinstance(plan, Plan) and (plan.modules or plan.interfaces)),
    )
    diagnostics.extend(module_issues)
    pointer, structure, mpd = _current_structure(record)
    structural_passed = bool(
        isinstance(plan, Plan)
        and module_ok
        and pointer
        and structure
        and mpd
        and pointer.native_passed
        and structure.passed
    )
    if not structural_passed:
        diagnostics.append(
            Diagnostic(
                code="flow.structure",
                stage="flow",
                message="The current source and plan require a passing two-column structure check.",
                next_action="Validate the current build.py.",
                action=run(
                    "Validate the current build.py.", "external", "validate", RECORD
                ),
            )
        )
    if structure is not None:
        diagnostics.extend(structure.diagnostics)

    snapshot_id = pointer.snapshot_id if pointer and structure else None
    review = empty_review
    review_ok = False
    review_outcome = None
    if include_review:
        review_snapshot = (
            snapshot_id if structural_passed and features is not None else None
        )
        try:
            review = review_status(record, review_snapshot or "") or empty_review
        except (OSError, ValueError) as exc:
            diagnostics.append(artifact_error(record, "review", str(exc)))
        if review_snapshot is None:
            review = empty_review | {"rounds": review.get("rounds", 0)}
        budget = meta.flow_review_rounds
        remaining = review.get("remaining_items")
        rounds = review.get("rounds")
        if review.get("current") is True and type(rounds) is int and rounds > 0:
            if review.get("passed") is True:
                review_outcome = "passed"
            elif (
                review.get("passed") is False
                and type(budget) is int
                and budget > 0
                and rounds >= budget
                and isinstance(remaining, list)
                and bool(remaining)
                and all(isinstance(item, str) and item.strip() for item in remaining)
            ):
                review_outcome = "rounds_exhausted"
        review_ok = review_outcome is not None
        if not review_ok:
            diagnostics.append(
                Diagnostic(
                    code="flow.review",
                    stage="flow",
                    message="Review the current structural snapshot in the builder session or with a separate reviewer.",
                    next_action=(
                        "Render, compare and review the current snapshot; status gives "
                        "the step that is due."
                    ),
                )
            )
    artifacts = {}
    for name, valid, current in (
        (
            "conditions",
            not any(issue.code == "flow.conditions" for issue in diagnostics),
            True,
        ),
        ("features", features is not None, features is not None),
        ("plan", plan is not None, plan is not None),
        ("review", review_ok, review.get("current") is True),
        ("structure", structure is not None, structure is not None),
    ):
        path = _pointer(record) if name == "structure" else record.path / f"{name}.json"
        try:
            digest = file_sha256(path)
        except OSError:
            digest = None
        artifacts[name] = {
            "exists": path.is_file(),
            "valid": valid,
            "current": current and path.is_file(),
            "sha256": digest,
        }
    if meta.flow != VERSION:
        diagnostics.append(
            Diagnostic(
                code="flow.version",
                stage="flow",
                message=f"Unsupported flow version {meta.flow}.",
            )
        )
    return {
        "enabled": True,
        "ready": bool(
            structural_passed
            and review_ok
            and features
            and artifacts["conditions"]["valid"]
            and meta.flow == VERSION
        ),
        "diagnostics": [issue.model_dump(mode="json") for issue in diagnostics],
        "snapshot_id": snapshot_id,
        "mpd_path": str(mpd.resolve()) if mpd else None,
        "mpd_sha256": pointer.mpd_sha256 if pointer and mpd else None,
        "structural_passed": structural_passed,
        "artifacts": artifacts,
        "review": review,
        "limitations": structure.limitations if structure else [],
        "finalized": meta.finalized,
        "review_outcome": review_outcome,
        "structure": {
            "complete": structure.complete,
            "counts": structure.differences,
            "report_path": f"flow/snapshots/{snapshot_id}/structure.json",
        }
        if structure
        else None,
    }


def summarize_flow(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep agent-facing status small while preserving every declared limitation ID."""
    if state is None:
        return None
    return state | {
        "limitations": [
            {
                "interface_id": item["interface_id"],
                "gap": item["gap"],
                "reason": item["reason"],
                "reference_id": item["reference"]["id"],
            }
            for item in state["limitations"]
        ]
    }
