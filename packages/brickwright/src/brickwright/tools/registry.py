"""Agent tool registry, schema exposure, and dispatch to tool operations.

The schemas are the litellm/OpenAI function-calling format. :func:`execute_tool`
runs a single tool call against a record and returns a :class:`ToolOutcome`. Tool
*failures* (a bad edit, an unknown tool) are returned as content for the model to
read and recover from -- they do not raise.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from bricklayer.diagnostics import DiagnosticError
from pydantic import ValidationError

from ..config import Config
from ..knowledge import read_knowledge, search_knowledge
from ..record import Record, Usage, UsageTracker
from .diagnostics import Diagnostic, exception_diagnostic
from .editing import (
    EditError,
    ViewError,
    edit_build,
    insert_build,
    multi_edit_build,
    view,
)
from .exploration import explore
from .finalization import finalize
from .rendering import (
    render,
    render_angle,
    render_available,
)
from .schemas import (
    DispatchArgs,
    EditBuildArgs,
    ExploreArgs,
    FinalizeArgs,
    InsertBuildArgs,
    MultiEditBuildArgs,
    ReadBuildArgs,
    ReadBuildingKnowledgeArgs,
    RenderArgs,
    SearchBuildingKnowledgeArgs,
    ToolArgs,
    ValidateArgs,
    WriteBuildArgs,
    function_tool,
)
from .subagents import SubagentResult, dispatch_subagents
from .tool_arguments import argument_diagnostics, argument_object
from .validation import ValidationReport, validate


def available_tools(config: Config | None = None) -> list[dict[str, Any]]:
    """The tools offered to the agent, gated by environment and ``config``."""
    excluded = set()
    if config is None or not config.flow or config.is_subagent:
        excluded.update(_FLOW_TOOLS)
    if not render_available():
        excluded.add("render")
    if config is not None and config.is_subagent:
        excluded.add("finalize")
    if config is None or not config.can_dispatch_subagents:
        excluded.add("dispatch_subagents")
    return [tool for tool in _ALL_TOOLS if tool["function"]["name"] not in excluded]


@dataclass
class ToolOutcome:
    """The result of one tool call, fed back to the model as the tool message."""

    content: str
    ok: bool = True
    done: bool = False
    report: ValidationReport | None = None
    image: Path | None = None  # an image to show the model next, e.g. a fresh render
    usage: Usage | None = None  # token usage to fold into the run (e.g. from subagents)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @classmethod
    def failure(cls, message: str, diagnostics: list[Diagnostic]) -> ToolOutcome:
        """Encode a tool error and its diagnostic facts for the model."""
        return cls(
            json.dumps(
                {
                    "ok": False,
                    "error": message,
                    "diagnostics": [
                        issue.model_dump(mode="json") for issue in diagnostics
                    ],
                }
            ),
            ok=False,
            diagnostics=diagnostics,
        )


def tool_call_failure(name: str, exc: DiagnosticError) -> ToolOutcome:
    """Return a diagnostic for an error before a tool handler could run."""
    issue = Diagnostic(
        code=exc.code,
        stage="tool_call",
        message=str(exc),
        evidence={"tool": name, **exc.evidence},
        next_action=exc.next_action,
    )
    return ToolOutcome.failure(f"{name} failed: {exc}", [issue])


def unknown_tool(name: str) -> ToolOutcome:
    return tool_call_failure(
        name,
        DiagnosticError(
            f"unknown tool {name!r}",
            code="tool.unknown",
            next_action="Choose a tool from the available tool descriptions.",
        ),
    )


def execute_tool(
    record: Record, config: Config, name: str, arguments: dict[str, Any]
) -> ToolOutcome:
    """Run a single tool call against ``record`` and return its outcome."""
    tool = _TOOLS.get(name)
    if tool is None:
        return unknown_tool(name)
    try:
        parsed = tool.arguments.model_validate(argument_object(arguments))
    except DiagnosticError as exc:
        return tool_call_failure(name, exc)
    except ValidationError as exc:
        return ToolOutcome.failure(
            f"{name} failed: invalid arguments", argument_diagnostics(name, exc)
        )
    try:
        return tool.handler(record, config, parsed)
    except (OSError, ValueError, DiagnosticError, RuntimeError) as exc:
        if name not in _FLOW_TOOLS:
            raise
        issue = exception_diagnostic(exc, "flow", code="flow.invalid_input")
        return ToolOutcome.failure(str(exc), [issue])


def validation_outcome(report: ValidationReport) -> ToolOutcome:
    """The reply for a ``validate`` call that produced ``report``."""
    return ToolOutcome(
        report.agent_json(),
        ok=report.ok,
        report=report,
        diagnostics=report.diagnostics,
    )


def subagent_outcome(results: list[SubagentResult]) -> ToolOutcome:
    """The reply for a ``dispatch_subagents`` call that produced ``results``."""
    return ToolOutcome(
        format_subagent_results(results),
        ok=all(r.ok for r in results),
        usage=_sum_usages(r.usage for r in results),
    )


def format_subagent_results(results: list[SubagentResult]) -> str:
    """Serialize subagent results into the JSON the model reads."""
    return json.dumps(
        {
            "results": [
                {
                    "task": i,
                    "ok": r.ok,
                    "valid": r.valid,
                    "failed_checks": r.failed_checks,
                    "finalized": r.finalized,
                    "result": r.text,
                    "reason": r.reason,
                    "record_id": r.record_id,
                    **r.model_dump(
                        include={
                            "module",
                            "session_id",
                            "snapshot_id",
                            "contract_sha256",
                            "elapsed_seconds",
                        },
                        exclude_none=True,
                    ),
                }
                for i, r in enumerate(results)
            ]
        }
    )


_Args = TypeVar("_Args", bound=ToolArgs)


@dataclass(frozen=True)
class _Tool(Generic[_Args]):
    arguments: type[_Args]
    handler: Callable[[Record, Config, _Args], ToolOutcome]


def _read_build(record: Record, config: Config, args: ReadBuildArgs) -> ToolOutcome:
    try:
        return ToolOutcome(view(record.read_build(), args.view_range))
    except ViewError as exc:
        return _edit_failure("read failed", exc)


def _write_build(record: Record, config: Config, args: WriteBuildArgs) -> ToolOutcome:
    record.write_build(args.content)
    return ToolOutcome("ok: build.py written")


def _insert_build(record: Record, config: Config, args: InsertBuildArgs) -> ToolOutcome:
    try:
        snippet = insert_build(record, args.insert_line, args.insert_text)
    except EditError as exc:
        return _edit_failure("insert failed", exc)
    return _edited(snippet)


def _edit_build(record: Record, config: Config, args: EditBuildArgs) -> ToolOutcome:
    try:
        snippet = edit_build(record, args.old_str, args.new_str, args.replace_all)
    except EditError as exc:
        return _edit_failure("edit failed", exc)
    return _edited(snippet)


def _multi_edit_build(
    record: Record, config: Config, args: MultiEditBuildArgs
) -> ToolOutcome:
    try:
        applied = multi_edit_build(record, list(args.edits))
    except EditError as exc:
        return _edit_failure("multi-edit failed (build.py unchanged)", exc)
    return ToolOutcome(f"ok: applied {applied} edits to build.py")


def _validate(record: Record, config: Config, args: ValidateArgs) -> ToolOutcome:
    return validation_outcome(validate(record, config))


def _explore(record: Record, config: Config, args: ExploreArgs) -> ToolOutcome:
    result = explore(record, args.code, config)
    return ToolOutcome(
        result.model_dump_json() if result.diagnostics else result.output,
        ok=result.ok,
        diagnostics=result.diagnostics,
    )


def _search_building_knowledge(
    record: Record,
    config: Config,
    args: SearchBuildingKnowledgeArgs,
) -> ToolOutcome:
    result = search_knowledge(args.query, limit=args.limit)
    return ToolOutcome(result.model_dump_json())


def _read_building_knowledge(
    record: Record,
    config: Config,
    args: ReadBuildingKnowledgeArgs,
) -> ToolOutcome:
    try:
        entry = read_knowledge(args.entry_id)
    except KeyError:
        return ToolOutcome(f"Unknown knowledge entry {args.entry_id!r}.", ok=False)
    return ToolOutcome(entry.model_dump_json())


def _render(record: Record, config: Config, args: RenderArgs) -> ToolOutcome:
    """Render the contact sheet, or a single view when a camera angle is given."""
    if args.azimuth is None:
        report = render(record, config)
        rendered = "a multi-view contact sheet"
    else:
        azimuth, elevation = args.azimuth, args.elevation
        assert elevation is not None  # RenderArgs validates the pair.
        report = render_angle(
            record, azimuth, elevation, config, out_path=record.angle_render_path
        )
        rendered = f"a single view at azimuth {azimuth:g}, elevation {elevation:g}"
    if report.ok:
        return ToolOutcome(
            f"ok: rendered the build to {rendered}; the image follows.",
            image=report.image_path,
        )
    return ToolOutcome.failure(f"render failed:\n{report.error}", report.diagnostics)


def _finalize(record: Record, config: Config, args: FinalizeArgs) -> ToolOutcome:
    report = finalize(record, config)
    diagnostics = report.diagnostics + (
        report.validation.diagnostics if report.validation is not None else []
    )
    if report.ok:
        message = (
            report.agent_json()
            if report.flow
            else "ok: build compiled to out/build.ldr"
        )
    else:
        message = report.model_copy(
            update={"error": f"compile failed:\n{report.error}"}
        ).agent_json()
    return ToolOutcome(
        message,
        ok=report.ok,
        done=report.ok,
        report=report.validation,
        diagnostics=diagnostics,
    )


def _dispatch(record: Record, config: Config, args: DispatchArgs) -> ToolOutcome:
    return subagent_outcome(dispatch_subagents(record, args.tasks, config))


class ReadArtifactArgs(ToolArgs):
    """Read the stored checklist or plan, including its JSON schema."""


class WriteArtifactArgs(ToolArgs):
    """Validate and save a complete artifact against the original task and plan."""

    payload: dict[str, Any]


class AlignedReviewArgs(ToolArgs):
    """Render the structurally checked MPD at the supplied cameras, compare all goal images and ask an independent model for an image review."""

    cameras: str | None = None
    reference_dir: str | None = None


class FreezeModulesArgs(ToolArgs):
    """Freeze the current checked child snapshots for use by place_module in the parent."""


def _artifact_outcome(result: dict[str, Any]) -> ToolOutcome:
    diagnostics = [
        Diagnostic.model_validate(issue) for issue in result.get("diagnostics", [])
    ]
    return ToolOutcome(
        json.dumps(result, ensure_ascii=False),
        ok=not diagnostics and result.get("ok", True),
        diagnostics=diagnostics,
    )


def _read_artifact_tool(name: Literal["features", "plan"]) -> _Tool[ReadArtifactArgs]:
    def read(record: Record, config: Config, args: ToolArgs) -> ToolOutcome:
        from ..flow import read_artifact

        return _artifact_outcome(read_artifact(record, name))

    return _Tool(ReadArtifactArgs, read)


def _write_artifact_tool(name: Literal["features", "plan"]) -> _Tool[WriteArtifactArgs]:
    def write(record: Record, config: Config, args: WriteArtifactArgs) -> ToolOutcome:
        from ..flow import set_artifact

        return _artifact_outcome(set_artifact(record, name, args.payload))

    return _Tool(WriteArtifactArgs, write)


def _aligned_review(
    record: Record, config: Config, args: AlignedReviewArgs
) -> ToolOutcome:
    from ..aligned_review import compare_record, render_cameras, run_independent_review
    from ..driver.tracking import RunTracker

    if (args.cameras is None) != (args.reference_dir is None):
        raise ValueError(
            "Supply both cameras and reference_dir, or omit both to match the prompt image."
        )
    if args.cameras is not None and args.reference_dir is not None:
        render_cameras(record, Path(args.cameras), config)
        compare_record(record, Path(args.reference_dir))
    tracker = RunTracker()
    try:
        if args.cameras is None:
            result = run_independent_review(
                record, config, on_response=tracker.record, use_viewpoint=True
            )
        else:
            result = run_independent_review(record, config, on_response=tracker.record)
        outcome = _artifact_outcome(result)
    except Exception as exc:
        issue = exception_diagnostic(exc, "review", code="flow.review_failed")
        outcome = ToolOutcome.failure(str(exc), [issue])
    if tracker.cost_known() or tracker.has_unknown_cost():
        outcome.usage = tracker.usage()
    return outcome


def _freeze_modules(
    record: Record, config: Config, args: FreezeModulesArgs
) -> ToolOutcome:
    from ..modules import prepare_merge

    merge = prepare_merge(record)
    if merge is None:
        return ToolOutcome("The plan contains no modules.", ok=False)
    return ToolOutcome(json.dumps({"manifest_sha256": merge.manifest_sha256}))


_FLOW_TOOLS: dict[str, _Tool[Any]] = {
    "read_features": _read_artifact_tool("features"),
    "set_features": _write_artifact_tool("features"),
    "read_plan": _read_artifact_tool("plan"),
    "set_plan": _write_artifact_tool("plan"),
    "request_aligned_review": _Tool(AlignedReviewArgs, _aligned_review),
    "freeze_modules": _Tool(FreezeModulesArgs, _freeze_modules),
}


_TOOLS: dict[str, _Tool[Any]] = {
    **_FLOW_TOOLS,
    "read_build": _Tool(ReadBuildArgs, _read_build),
    "write_build": _Tool(WriteBuildArgs, _write_build),
    "insert_build": _Tool(InsertBuildArgs, _insert_build),
    "edit_build": _Tool(EditBuildArgs, _edit_build),
    "multi_edit_build": _Tool(MultiEditBuildArgs, _multi_edit_build),
    "validate": _Tool(ValidateArgs, _validate),
    "explore": _Tool(ExploreArgs, _explore),
    "render": _Tool(RenderArgs, _render),
    "finalize": _Tool(FinalizeArgs, _finalize),
    "dispatch_subagents": _Tool(DispatchArgs, _dispatch),
    "search_building_knowledge": _Tool(
        SearchBuildingKnowledgeArgs, _search_building_knowledge
    ),
    "read_building_knowledge": _Tool(
        ReadBuildingKnowledgeArgs, _read_building_knowledge
    ),
}
_ALL_TOOLS = [function_tool(name, tool.arguments) for name, tool in _TOOLS.items()]


def _edit_failure(message: str, exc: DiagnosticError) -> ToolOutcome:
    return ToolOutcome.failure(f"{message}: {exc}", [exception_diagnostic(exc, "edit")])


def _edited(snippet: str) -> ToolOutcome:
    """The reply for an edit that applied, showing the region it touched."""
    return ToolOutcome(f"ok: build.py edited. Edited region:\n{snippet}")


def _sum_usages(usages: Iterable[Usage]) -> Usage:
    tracker = UsageTracker()
    for u in usages:
        tracker.add(u)
    return tracker.usage()
