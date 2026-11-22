"""Structured feedback shared by execution reports and tool messages."""

from __future__ import annotations

import linecache
import traceback
from pathlib import Path

from bricklayer.diagnostics import DiagnosticError
from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Diagnostic(BaseModel):
    """A stable reason, observed evidence, and an optional recovery action."""

    model_config = ConfigDict(frozen=True)

    code: str
    stage: str
    message: str
    evidence: dict[str, JsonValue] = Field(default_factory=dict)
    next_action: str | None = None


class DiagnosticReport(BaseModel):
    """Additive diagnostics for reports written before this field existed."""

    model_config = ConfigDict(frozen=True)

    diagnostics: list[Diagnostic] = Field(default_factory=list)


def exception_diagnostic(
    exc: Exception,
    stage: str,
    *,
    filename: str | None = None,
    source: str | None = None,
    code: str = "execution.exception",
    evidence: dict[str, JsonValue] | None = None,
    next_action: str | None = None,
) -> Diagnostic:
    """Describe an exception using its type and the actual executed source."""
    facts: dict[str, JsonValue] = {
        **(evidence or {}),
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
    }
    if isinstance(exc, DiagnosticError):
        code = exc.code
        facts.update(exc.evidence)
        next_action = exc.next_action
    elif isinstance(exc, SyntaxError):
        code = "source.syntax_error"
        if stage == "explore":
            next_action = "Correct the reported Python syntax and rerun the snippet."
        else:
            next_action = (
                "Correct the reported Python syntax in build.py and try again."
            )
    elif isinstance(exc, ImportError) and code == "execution.exception":
        code = "source.import_error"
        if exc.name is not None:
            facts["import"] = exc.name
        next_action = "Check that the reported module or name can be imported in this environment."
    if filename is not None:
        location = _source_location(exc, filename, source)
        if location is not None:
            facts["source"] = location
    return Diagnostic(
        code=code,
        stage=stage,
        message=exc.msg
        if isinstance(exc, SyntaxError)
        else str(exc) or type(exc).__name__,
        evidence=facts,
        next_action=next_action,
    )


def source_read_diagnostic(exc: Exception) -> Diagnostic:
    """Report failure to obtain the build.py snapshot for an operation."""
    return exception_diagnostic(
        exc,
        "load_build",
        code="source.read_failed",
        evidence={"path": "build.py"},
        next_action="Check that the record contains a readable UTF-8 build.py.",
    )


def captured_traceback(exc: Exception, filename: str, source: str | None) -> str:
    """Format the stack with lines from the executed snapshot when available."""
    if source is None:
        return "".join(traceback.format_exception(exc))
    previous = linecache.cache.get(filename)
    linecache.cache[filename] = (
        len(source),
        None,
        source.splitlines(keepends=True),
        filename,
    )
    try:
        return "".join(traceback.format_exception(exc))
    finally:
        if previous is None:
            linecache.cache.pop(filename, None)
        else:
            linecache.cache[filename] = previous


def _source_location(
    exc: Exception, filename: str, source: str | None
) -> dict[str, JsonValue] | None:
    line = None
    function = None
    column = None
    if isinstance(exc, SyntaxError) and exc.filename == filename:
        line = exc.lineno
        column = exc.offset
    else:
        for frame, number in traceback.walk_tb(exc.__traceback__):
            if frame.f_code.co_filename == filename:
                line, function = number, frame.f_code.co_name
    if line is None:
        return None
    location: dict[str, JsonValue] = {
        "file": Path(filename).name,
        "line": line,
    }
    if function is not None and function != "<module>":
        location["function"] = function
    if column is not None:
        location["column"] = column
    if source is not None:
        lines = source.splitlines()
        if 1 <= line <= len(lines):
            location["text"] = lines[line - 1]
    return location
