"""Failure facts for malformed tool calls."""

from math import isfinite
from typing import Any

from bricklayer.diagnostics import DiagnosticError
from pydantic import JsonValue, ValidationError

from .diagnostics import Diagnostic

_MAX_ARGUMENT_ISSUES = 20
_RANGE_ERRORS = {
    "greater_than",
    "greater_than_equal",
    "less_than",
    "less_than_equal",
    "too_long",
    "too_short",
}


def argument_object(arguments: object) -> dict[str, Any]:
    """Require a JSON object before dispatch or schema validation."""
    if not isinstance(arguments, dict):
        raise DiagnosticError(
            "arguments must be a JSON object",
            code="tool.arguments_not_object",
            evidence={"actual_type": type(arguments).__name__},
            next_action="Send the tool parameters as a JSON object.",
        )
    return arguments


def argument_diagnostics(name: str, exc: ValidationError) -> list[Diagnostic]:
    """Translate schema validation facts without inspecting error text."""
    issues = []
    errors = exc.errors(include_url=False)
    for error in errors[:_MAX_ARGUMENT_ISSUES]:
        error_type = error["type"]
        context = error.get("ctx", {})
        parameter = context.get("parameter")
        if parameter is None and error["loc"]:
            parameter = ".".join(str(part) for part in error["loc"])
        evidence: dict[str, JsonValue] = {"tool": name, "constraint": error_type}
        if parameter is not None:
            evidence["parameter"] = parameter
        evidence.update(
            {
                key: value
                for key, value in context.items()
                if key != "parameter" and isinstance(value, (str, bool, int, float))
            }
        )
        if len(errors) > _MAX_ARGUMENT_ISSUES:
            evidence["error_count"] = len(errors)
        if error_type in {"missing", "missing_paired_argument"}:
            code = "tool.argument_missing"
        elif error_type in _RANGE_ERRORS:
            code = "tool.argument_out_of_range"
            value = error.get("input")
            if isinstance(value, int) or isinstance(value, float) and isfinite(value):
                evidence["actual"] = value
        else:
            code = "tool.invalid_argument"
        issues.append(
            Diagnostic(
                code=code,
                stage="tool_call",
                message=error["msg"],
                evidence=evidence,
                next_action="Correct the indicated parameter and call the tool again.",
            )
        )
    return issues
