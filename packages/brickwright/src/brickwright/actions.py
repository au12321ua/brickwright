"""Structured next steps that the CLI resolves into runnable commands.

Library code describes a step with BrickWright arguments and placeholders. Only
the CLI knows the interpreter, the records directory and the file that the host
actually passed, so it resolves each step into an ``argv`` the host can run.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any, Literal, cast

from bricklayer.diagnostics import DiagnosticError
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

RECORD = "{record}"  # the record ID as the host addressed it
FILE = "{file}"  # the file submitted by the current invocation
_RECORD_COMMANDS = frozenset({"external", "ls", "show"})


class Action(BaseModel):
    """One step: run a command, edit a file then run, or provide an input then run."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["run", "edit", "provide"]
    summary: str
    args: tuple[str, ...] = ()
    path: str | None = None
    fields: tuple[str, ...] = ()
    placeholders: tuple[str, ...] = ()
    then: Action | None = None
    argv: tuple[str, ...] | None = None
    command: str | None = None


def run(summary: str, *args: str) -> Action:
    return Action(kind="run", summary=summary, args=args)


def edit(
    summary: str, path: str | Path, then: Action, *, fields: tuple[str, ...] = ()
) -> Action:
    return Action(
        kind="edit", summary=summary, path=str(path), fields=fields, then=then
    )


def provide(
    summary: str,
    then: Action,
    *,
    placeholders: tuple[str, ...] = (),
    path: str | Path | None = None,
) -> Action:
    return Action(
        kind="provide",
        summary=summary,
        then=then,
        placeholders=placeholders,
        path=None if path is None else str(path),
    )


class FlowError(DiagnosticError):
    """A workflow input or state error with a stable code and a next step."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        action: Action | None = None,
        evidence: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            evidence=cast(dict[str, JsonValue], evidence),
            next_action=action.summary if action else None,
        )
        self.action = action
        self.extra = extra or {}


def validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    """Field paths and reasons, without links or echoed input."""
    errors = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"]) or "$"
        errors.append({"path": location, "reason": error["msg"]})
    return errors


def validation_message(label: str, errors: list[dict[str, str]]) -> str:
    shown = "; ".join(f"{item['path']}: {item['reason']}" for item in errors[:5])
    more = f"; and {len(errors) - 5} more" if len(errors) > 5 else ""
    noun = "field" if len(errors) == 1 else "fields"
    return f"{label} has {len(errors)} invalid {noun}: {shown}{more}."


class Invocation(BaseModel):
    """The context of one CLI call used to resolve placeholders."""

    records_dir: Path
    record_id: str | None = None
    file: Path | None = None
    command: str = ""

    def resolve(self, action: Action) -> Action:
        then = self.resolve(action.then) if action.then is not None else None
        path = action.path
        placeholders = list(action.placeholders)
        if path == FILE:
            if self.file is not None:
                path = str(self.file.absolute())
            else:
                placeholders.append("JSON_FILE")
                path = "JSON_FILE"
        argv = command = None
        if action.args:
            args = [self._substitute(part, placeholders) for part in action.args]
            if args[0] in _RECORD_COMMANDS:
                args += ["--records-dir", str(self.records_dir.absolute())]
            argv = (sys.executable, "-m", "brickwright", *args)
            command = shlex.join(argv)
        if then is not None:
            placeholders.extend(
                item for item in then.placeholders if item not in placeholders
            )
        return action.model_copy(
            update={
                "path": path,
                "then": then,
                "argv": argv,
                "command": command,
                "placeholders": tuple(dict.fromkeys(placeholders)),
            }
        )

    def _substitute(self, part: str, placeholders: list[str]) -> str:
        if part == RECORD and self.record_id is not None:
            return self.record_id
        if part == FILE:
            if self.file is not None:
                return str(self.file.absolute())
            placeholders.append("JSON_FILE")
            return "JSON_FILE"
        return part


def describe(action: Action) -> str:
    """A one-line rendering for readers of the legacy ``next_action`` string."""
    if action.kind == "run" and action.command:
        return action.command
    text = action.summary
    if action.then is not None:
        text += f" Then run: {action.then.command or action.then.summary}"
    return text
