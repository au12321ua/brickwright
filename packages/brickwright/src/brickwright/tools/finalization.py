"""Run a record's build.py in a child process, compile it to LDraw, and render it."""

from __future__ import annotations

import json
import traceback
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..actions import RECORD, run
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


class DeliveryVerification(BaseModel):
    """Whether a submitted MPD matches the current finalized flow snapshot."""

    ok: bool
    error: str | None = None
    code: str | None = None
    snapshot_id: str | None = None
    mpd_sha256: str | None = None
    review_outcome: str | None = None
    review_dispatch: str | None = None


class _Rejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def verify_delivery(record: Record, file: Path) -> DeliveryVerification:
    """Check the current finalize marker and compare the submitted MPD byte for byte."""
    try:
        meta = record.read_meta()
        if (
            meta.flow is None
            or not meta.finalized
            or meta.stop_reason is not StopReason.FINALIZED
        ):
            raise _Rejected(
                "delivery.not_finalized",
                "The record has no current successful flow finalization.",
            )
        if not file.is_file():
            raise _Rejected(
                "delivery.missing_file",
                f"The submitted MPD is missing or not a file: {file}",
            )
        submitted = file.read_bytes()
        state = flow_status(record)
        if not state["ready"] or not state["finalized"]:
            raise _Rejected(
                "delivery.stale", "The finalized flow evidence is no longer current."
            )
        frozen = Path(state["mpd_path"]).read_bytes()
        digest = sha256(frozen).hexdigest()
        if digest != state["mpd_sha256"] or record.ldr_path.read_bytes() != frozen:
            raise _Rejected(
                "delivery.stale",
                "The finalized output differs from the verified snapshot.",
            )
        if submitted != frozen:
            raise _Rejected(
                "delivery.mismatch",
                "The submitted MPD differs from the finalized snapshot.",
            )
        current = record.read_meta()
        if (
            not current.finalized
            or current.stop_reason is not StopReason.FINALIZED
            or current.finalized_flow_sha256 != meta.finalized_flow_sha256
        ):
            raise _Rejected(
                "delivery.stale",
                "The finalization changed during delivery verification.",
            )
    except _Rejected as exc:
        return DeliveryVerification(ok=False, error=str(exc), code=exc.code)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return DeliveryVerification(ok=False, error=str(exc), code="delivery.stale")
    return DeliveryVerification(
        ok=True,
        snapshot_id=state["snapshot_id"],
        mpd_sha256=digest,
        review_outcome=state["review_outcome"],
        review_dispatch=state["review"].get("dispatch"),
    )


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
    structure = validation.structure
    digest = state["mpd_sha256"]
    if structure is None or not structure.passed or structure.mpd_sha256 != digest:
        return _flow_changed(record, validation, state)
    prompt_image = record.read_meta().prompt_image
    try:
        stamp = delivery_stamp(record, prompt_image, output_sha256=digest)
    except OSError:
        return _flow_changed(record, validation, state)
    report = build_execution.run_guarded(
        _publish_flow,
        (record, Path(state["mpd_path"]), digest),
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
        or current["artifacts"] != state["artifacts"]
        or current["review"] != state["review"]
        or not _same_delivery(record, prompt_image, stamp)
    ):
        report = _flow_changed(record, validation, current)
    if report.ok:
        record.update_meta(
            finalized=True,
            exported_source_sha256=validation.source_sha256,
            finalized_flow_sha256=stamp,
            stop_reason=StopReason.FINALIZED,
        )
        current = flow_status(record)
        if (
            not current["ready"]
            or not current["finalized"]
            or current["snapshot_id"] != state["snapshot_id"]
            or not _same_delivery(record, prompt_image, stamp)
        ):
            report = _flow_changed(record, validation, current)
    if not report.ok:
        record.clear_exports()
    return report.model_copy(update={"validation": validation, "flow": current})


def _flow_changed(
    record: Record, validation: ValidationReport, state: dict[str, Any]
) -> FinalizationReport:
    message = "Flow inputs changed during finalization."
    return FinalizationReport(
        ok=False,
        error=message,
        validation=validation,
        flow=state,
        diagnostics=[
            Diagnostic(
                code="flow.changed",
                stage="finalize",
                message=message,
                next_action="Check the record status and repeat the step it names.",
                action=run("Check the record status.", "external", "status", RECORD),
            )
        ],
    )


def _same_delivery(record: Record, prompt_image: str | None, stamp: str) -> bool:
    try:
        return delivery_stamp(record, prompt_image) == stamp
    except OSError:
        return False


def _publish_flow(record: Record, mpd: Path, digest: str) -> FinalizationReport:
    """Publish bytes from the structural snapshot, without re-executing source."""
    stage = "export"
    try:
        frozen = mpd.read_bytes()
        if sha256(frozen).hexdigest() != digest:
            raise ValueError("The verified MPD changed before publication.")
        atomic_write(record.ldr_path, frozen)
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
