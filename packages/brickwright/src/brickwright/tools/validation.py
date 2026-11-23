"""Run a record's build.py in a child process and validate the build."""

from __future__ import annotations

import io
import json
from collections.abc import Iterable
from contextlib import redirect_stdout
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from ..config import Config
from ..flow import flow_status
from ..flow.artifacts import Plan, parse_artifact
from ..flow.integrations import (
    check_module,
    merge_context,
    merge_evidence,
    prepare_merge,
)
from ..flow.state import StructureInputs, save_structure, summarize_flow
from ..flow.structure import (
    Column,
    StructureReport,
    classify_structure,
    inspect_build,
    inspect_mpd,
)
from ..record import Record, Status
from . import build_execution
from .diagnostics import (
    Diagnostic,
    DiagnosticReport,
    captured_traceback,
    exception_diagnostic,
    source_read_diagnostic,
)
from .testing import CheckResult, TestContext

if TYPE_CHECKING:
    from bricklayer import LegoBuild


class BuildDimensions(BaseModel):
    """A build's bounding-box size: width/depth in studs, height in bricks."""

    model_config = ConfigDict(frozen=True)

    width: float  # extent along X (studs)
    depth: float  # extent along Y (studs)
    height: float  # extent along Z (bricks; a plate is 1/3)

    @classmethod
    def of(cls, build: LegoBuild) -> BuildDimensions:
        return cls(
            width=float(build.width),
            depth=float(build.depth),
            height=float(build.height),
        )


class ValidationReport(DiagnosticReport):
    """The full result of validating a record's build.py."""

    ok: bool
    source_sha256: str | None = None
    report_path: str | None = None  # snapshot report relative to the record
    build_error: str | None = None
    error_type: str | None = None  # exception type that caused a build error
    checks: list[CheckResult] = Field(default_factory=list)
    dimensions: BuildDimensions | None = None  # bounding-box size of the build
    brick_count: int | None = None  # total number of parts in the build
    stdout: str = ""  # everything printed to stdout while creating the build
    structure: StructureReport | None = None
    flow: dict[str, Any] | None = None
    module_contract_sha256: str | None = None
    module_mpd_sha256: str | None = None

    def agent_payload(self) -> dict[str, Any]:
        """Serialize bounded diagnostics and counts, with pointers to full evidence."""
        data = self.model_dump(mode="json", exclude={"flow", "structure"})
        if self.flow is not None:
            data["flow"] = summarize_flow(self.flow)
        return data

    def agent_json(self, indent: int | None = None) -> str:
        return json.dumps(self.agent_payload(), indent=indent, ensure_ascii=False)

    @property
    def failed_checks(self) -> list[str]:
        """The names of the checks that failed, in run order."""
        return [c.name for c in self.checks if not c.passed]

    @classmethod
    def from_checks(
        cls,
        checks: Iterable[CheckResult],
        stdout: str = "",
        *,
        build: LegoBuild | None = None,
    ) -> ValidationReport:
        """A report for a build that was created; ``ok`` is true iff every check passed."""
        checks = list(checks)
        diagnostics = [
            issue
            for check in checks
            if not check.passed
            for issue in (check.diagnostics or [_custom_check_diagnostic(check)])
        ]
        return cls(
            ok=all(c.passed for c in checks),
            checks=[
                check.model_copy(update={"diagnostics": []})
                if check.diagnostics
                else check
                for check in checks
            ],
            diagnostics=diagnostics,
            stdout=stdout,
            dimensions=BuildDimensions.of(build) if build is not None else None,
            brick_count=build.brick_count if build is not None else None,
        )

    @classmethod
    def error(
        cls,
        message: str,
        *,
        stdout: str = "",
        error_type: str | None = None,
        diagnostics: list[Diagnostic] | None = None,
    ) -> ValidationReport:
        """A report for a build that raised or timed out before its checks ran."""
        return cls(
            ok=False,
            build_error=message,
            error_type=error_type,
            stdout=stdout,
            diagnostics=diagnostics or [],
        )


def _custom_check_diagnostic(check: CheckResult) -> Diagnostic:
    return Diagnostic(
        code="validation.check_failed",
        stage="run_tests",
        message=check.detail or f"Check {check.name!r} failed.",
        evidence={"check": check.name},
    )


def validate(record: Record, config: Config | None = None) -> ValidationReport:
    """Build and validate a record; persist the report and return it.

    Writes ``out/report.json``, sets the record's status to ``valid`` /
    ``invalid`` / ``error``, and snapshots the validated source and its report
    under ``snapshots/`` so the build's history across iterations is kept.
    """
    config = config or Config()
    try:
        source = record.read_build()
    except (OSError, UnicodeError) as exc:
        report = source_read_failure(exc)
        if record.read_meta().flow is not None:
            report = report.model_copy(update={"flow": flow_status(record)})
        _write_report(record, report)
        record.update_meta(status=Status.ERROR, validated_source_sha256=None)
        return report

    if record.read_meta().flow is not None:
        return validate_flow(record, source, config)
    report = build_execution.run_guarded(
        _validate,
        (str(record.build_path), source),
        config.build_timeout,
        lambda e: ValidationReport.error(
            f"Validation failed: {e}",
            error_type="RuntimeError"
            if isinstance(e, build_execution.ChildExited)
            else type(e).__name__,
            diagnostics=[
                build_execution.subprocess_diagnostic(
                    e, "validate", config.build_timeout
                )
            ],
        ),
    )
    return save_validation(record, source, report)


def validate_flow(record: Record, source: str, config: Config) -> ValidationReport:
    """Capture native geometry and run the standard catalog in a separate worker."""
    try:
        merge = prepare_merge(record)
        plan_path = record.path / "plan.json"
        try:
            raw_plan = plan_path.read_bytes()
        except FileNotFoundError:
            raw_plan = None
        inputs = StructureInputs.capture(record, sha256(source.encode()).hexdigest())
        if (
            raw_plan is not None
            and sha256(raw_plan).hexdigest() != inputs.plan_sha256
            or raw_plan is None
            and inputs.plan_sha256 is not None
        ):
            raise ValueError("The plan changed during validation.")
        if (
            merge is not None
            and sha256(merge.manifest_bytes).hexdigest() != inputs.manifest_sha256
        ):
            raise ValueError("The merge manifest changed during validation.")
    except (OSError, ValueError) as exc:
        report = ValidationReport.error(
            str(exc),
            error_type=type(exc).__name__,
            diagnostics=[exception_diagnostic(exc, "validate", code="flow.modules")],
        )
        return save_validation(
            record, source, report.model_copy(update={"flow": flow_status(record)})
        )
    plan, _ = (
        parse_artifact(record, "plan", raw_plan) if raw_plan is not None else (None, [])
    )
    plan = plan if isinstance(plan, Plan) else None
    result = build_execution.run_guarded(
        _validate_native_structure,
        (str(record.build_path), source, plan, merge),
        config.build_timeout,
        lambda exc: (
            ValidationReport.error(
                f"Validation failed: {exc}",
                error_type=type(exc).__name__,
                diagnostics=[
                    build_execution.subprocess_diagnostic(
                        exc, "validate", config.build_timeout
                    )
                ],
            ),
            None,
            None,
            [],
        ),
    )
    report, mpd, native, native_issues = result
    standard, standard_issues = (None, [])
    if mpd is not None:
        standard, standard_issues = build_execution.run_guarded(
            inspect_mpd,
            (mpd, plan, merge),
            config.build_timeout,
            lambda exc: (
                None,
                [
                    build_execution.subprocess_diagnostic(
                        exc, "structure", config.build_timeout
                    )
                ],
            ),
        )
    structure = classify_structure(
        native, standard, mpd, plan, [*native_issues, *standard_issues]
    )
    save_structure(record, inputs, structure, mpd, report.ok)
    state = flow_status(record)
    report = report.model_copy(update={"flow": state, "structure": structure})
    return save_validation(record, source, report)


def _validate_native_structure(
    build_path: str, source: str, plan: Plan | None, merge: Any
) -> tuple[ValidationReport, bytes | None, Column | None, list[Diagnostic]]:
    from bricklayer import to_ldraw

    with merge_context(merge):
        build, report = build_and_validate(build_path, source)
        if build is None:
            return report, None, None, []
        try:
            mpd = to_ldraw(build).encode("utf-8")
            column = inspect_build(build, plan)
            column.modules, diagnostics = merge_evidence(build, merge)
        except Exception as exc:
            return report, None, None, [exception_diagnostic(exc, "structure")]
    return report, mpd, column, diagnostics


def save_validation(
    record: Record, source: str, report: ValidationReport
) -> ValidationReport:
    """Persist a report and snapshot bound to the exact source that was checked."""
    digest = sha256(source.encode()).hexdigest()
    try:
        current_digest = record.source_sha256
    except FileNotFoundError:
        current_digest = None
    if current_digest != digest:
        message = "build.py changed during validation/export; validate the current source again"
        report = ValidationReport.error(
            f"{message}\n\n{report.build_error}" if report.build_error else message,
            error_type="SourceChanged",
            stdout=report.stdout,
            diagnostics=[
                Diagnostic(
                    code="source.changed",
                    stage="validate",
                    message=message,
                    evidence={
                        "checked_sha256": digest,
                        "current_sha256": current_digest,
                    },
                    next_action="Validate the current build.py before finalizing it.",
                ),
                *report.diagnostics,
            ],
        )
    if report.flow is None and record.read_meta().flow is not None:
        report = report.model_copy(update={"flow": flow_status(record)})
    snapshot = record.snapshot_source(source)
    report = report.model_copy(
        update={
            "source_sha256": digest,
            "report_path": str(snapshot.report_path.relative_to(record.path)),
        }
    )
    snapshot.report_path.write_text(_write_report(record, report))
    if report.ok:
        status = Status.VALID
    elif report.build_error:
        status = Status.ERROR
    else:
        status = Status.INVALID
    record.update_meta(status=status, validated_source_sha256=digest)
    return report


def _write_report(record: Record, report: ValidationReport) -> str:
    serialized = report.model_dump_json(indent=2) + "\n"
    record.out_dir.mkdir(parents=True, exist_ok=True)
    record.report_path.write_text(serialized)
    return serialized


def source_read_failure(exc: Exception) -> ValidationReport:
    """Report a build.py read error, before any source could be executed."""
    return ValidationReport.error(
        str(exc),
        error_type=type(exc).__name__,
        diagnostics=[source_read_diagnostic(exc)],
    )


def _validate(build_path: str, source: str | None = None) -> ValidationReport:
    """Create the build, run its tests, and return the verification report."""
    return build_and_validate(build_path, source)[1]


def build_and_validate(
    build_path: str, source: str | None = None
) -> tuple[LegoBuild | None, ValidationReport]:
    """Create the build once, run custom tests, and enforce collision and support checks.

    No-argument create_build calls in tests reuse the build being exported.
    Connectivity remains a custom check so multipart builds can choose its policy.
    """
    buf = io.StringIO()
    stage = "load_build"
    try:
        with redirect_stdout(buf):
            module = build_execution.load_build_module(build_path, source)
            stage = "create_build"
            create = module.create_build
            build = create()
            module.create_build = lambda *args, **kwargs: (
                create(*args, **kwargs) if args or kwargs else build
            )
            stage = "run_tests"
            custom = list(module.run_tests())
            stage = "validate"
            physical = TestContext(build)
            physical.check_no_floating()
            physical.check_no_collisions()
            checks = physical.report()
            required = {check.name for check in checks}
            checks.extend(check for check in custom if check.name not in required)
        digest, module_issues = check_module(build, Path(build_path).parent)
        if digest is not None or module_issues:
            checks.append(
                CheckResult(
                    name="module_contract",
                    passed=not module_issues,
                    detail=module_issues[0].message if module_issues else None,
                    diagnostics=module_issues,
                )
            )
        report = ValidationReport.from_checks(checks, stdout=_stdout(buf), build=build)
        if digest is not None:
            from bricklayer import to_ldraw

            report = report.model_copy(
                update={
                    "module_contract_sha256": digest,
                    "module_mpd_sha256": sha256(
                        to_ldraw(build).encode("utf-8")
                    ).hexdigest(),
                }
            )
        return build, report
    except Exception as exc:
        return None, ValidationReport.error(
            captured_traceback(exc, build_path, source),
            stdout=_stdout(buf),
            error_type=type(exc).__name__,
            diagnostics=[
                exception_diagnostic(exc, stage, filename=build_path, source=source)
            ],
        )


def _stdout(buf: io.StringIO) -> str:
    return build_execution.cap_output(buf.getvalue())
