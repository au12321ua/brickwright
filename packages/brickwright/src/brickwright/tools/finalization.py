"""Run a record's build.py in a child process, compile it to LDraw, and render it."""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

from ..config import Config
from ..flow import flow_status
from ..flow.artifacts import atomic_write
from ..flow.state import delivery_stamp, summarize_flow
from ..record import Record, StopReason
from . import build_execution, rendering
from .diagnostics import Diagnostic, DiagnosticReport, exception_diagnostic
from .validation import (
    ValidationReport,
    build_and_validate,
    save_validation,
    source_read_failure,
    validate_flow,
)


class FinalizationReport(DiagnosticReport):
    """The result of finalizing a build."""

    ok: bool
    error: str | None = None  # the traceback/message when the build failed
    validation: ValidationReport | None = None
    flow: dict[str, Any] | None = None

    def agent_json(self, indent: int | None = None) -> str:
        """Serialize finalization status and a bounded view of the validation report."""
        if self.flow is None:
            return self.model_dump_json(indent=indent)
        data = self.model_dump(mode="json", exclude={"flow", "validation"})
        data["validation"] = (
            self.validation.agent_payload() if self.validation else None
        )
        data["flow"] = summarize_flow(self.flow)
        return json.dumps(data, indent=indent, ensure_ascii=False)


def finalize(record: Record, config: Config | None = None) -> FinalizationReport:
    """Validate and export one source snapshot and the one build it creates."""
    config = config or Config()
    try:
        source = record.read_build()
    except (OSError, UnicodeError) as exc:
        validation = source_read_failure(exc)
        record.clear_exports()
        return FinalizationReport(
            ok=False,
            validation=validation,
            error=validation.build_error,
            flow=flow_status(record) if record.read_meta().flow is not None else None,
        )
    if record.read_meta().flow is not None:
        return _finalize_flow(record, source, config)
    record.clear_exports()
    report = build_execution.run_guarded(
        _finalize,
        (record, source),
        config.render_timeout,
        lambda e: FinalizationReport(
            ok=False,
            error=f"Finalization failed: {e}",
            diagnostics=[
                build_execution.subprocess_diagnostic(
                    e, "finalize", config.render_timeout
                )
            ],
        ),
    )
    if report.validation is not None:
        validation = save_validation(record, source, report.validation)
        report = report.model_copy(update={"validation": validation})
        if validation.error_type == "SourceChanged":
            report = report.model_copy(
                update={"ok": False, "error": validation.build_error}
            )
    if report.ok:
        assert report.validation is not None
        record.update_meta(
            finalized=True,
            exported_source_sha256=report.validation.source_sha256,
            stop_reason=StopReason.FINALIZED,
        )
    else:
        record.clear_exports()
    return report


def _finalize_flow(record: Record, source: str, config: Config) -> FinalizationReport:
    record.clear_exports()
    validation = validate_flow(record, source, config)
    state = validation.flow or flow_status(record)
    if not validation.ok or not state["ready"]:
        return FinalizationReport(
            ok=False,
            validation=validation,
            flow=state,
            error=validation.build_error
            or "The current staged build has unmet delivery conditions.",
            diagnostics=[
                Diagnostic.model_validate(issue) for issue in state["diagnostics"]
            ],
        )
    path = Path(state["mpd_path"])
    report = build_execution.run_guarded(
        _publish_flow,
        (record, path),
        config.render_timeout,
        lambda exc: FinalizationReport(
            ok=False,
            error=f"Finalization failed: {exc}",
            diagnostics=[
                build_execution.subprocess_diagnostic(
                    exc, "finalize", config.render_timeout
                )
            ],
        ),
    )
    current = flow_status(record)
    if report.ok and (
        not current["ready"]
        or current["snapshot_id"] != state["snapshot_id"]
        or not _same_output(record, path)
    ):
        report = FinalizationReport(
            ok=False,
            error="Flow inputs changed during finalization.",
            diagnostics=[
                Diagnostic(
                    code="flow.changed",
                    stage="finalize",
                    message="Flow inputs changed during finalization.",
                    next_action=f"brickwright external status {record.id}",
                )
            ],
        )
    if report.ok:
        meta = record.read_meta()
        record.update_meta(
            finalized=True,
            exported_source_sha256=validation.source_sha256,
            finalized_flow_sha256=delivery_stamp(record, meta.prompt_image),
            stop_reason=StopReason.FINALIZED,
        )
    else:
        record.clear_exports()
    return report.model_copy(
        update={
            "validation": validation,
            "flow": flow_status(record) if report.ok else current,
        }
    )


def _same_output(record: Record, mpd: Path) -> bool:
    try:
        return record.ldr_path.read_bytes() == mpd.read_bytes()
    except OSError:
        return False


def _publish_flow(record: Record, mpd: Path) -> FinalizationReport:
    """Publish bytes from the structural snapshot, without re-executing source."""
    stage = "export"
    try:
        atomic_write(record.ldr_path, mpd.read_bytes())
        if rendering.render_available():
            stage = "render"
            rendering._convert(record.ldr_path, record.mesh_path)
            rendering.mesh2png(record.mesh_path, record.render_path)
    except Exception as exc:
        return FinalizationReport(
            ok=False,
            error=traceback.format_exc(),
            diagnostics=[exception_diagnostic(exc, stage)],
        )
    return FinalizationReport(ok=True)


def _finalize(record: Record, source: str) -> FinalizationReport:
    """Create the build, write the LDraw file, and (when able) render a PNG."""
    build, validation = build_and_validate(str(record.build_path), source)
    if not validation.ok:
        return FinalizationReport(
            ok=False,
            validation=validation,
            error=validation.build_error
            or f"Validation failed: {', '.join(validation.failed_checks)}",
        )
    assert build is not None
    stage = "render"
    try:
        if rendering.render_available():
            rendering.mesh2png(rendering.build_mesh(record, build), record.render_path)
        else:
            stage = "export"
            rendering.build_ldraw(record, build)
    except Exception as exc:
        return FinalizationReport(
            ok=False,
            error=traceback.format_exc(),
            validation=validation,
            diagnostics=[rendering.output_diagnostic(exc, stage, record)],
        )
    return FinalizationReport(ok=True, validation=validation)
