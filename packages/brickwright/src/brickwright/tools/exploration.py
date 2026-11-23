"""Run a Python inspection snippet in a child process and capture its output."""

from __future__ import annotations

import ast
import io
from contextlib import nullcontext, redirect_stdout
from hashlib import sha256
from pathlib import Path

from ..config import Config
from ..record import Record
from . import build_execution
from .diagnostics import (
    DiagnosticReport,
    captured_traceback,
    exception_diagnostic,
    source_read_diagnostic,
)


class SnippetResult(DiagnosticReport):
    """The outcome of one inspection snippet: did it run, and what it printed."""

    ok: bool
    output: str  # captured stdout on success; a labeled traceback on failure
    source_sha256: str | None = None  # build.py digest when the snippet reads build


def explore(record: Record, code: str, config: Config | None = None) -> SnippetResult:
    """Run ``code`` for inspection with ``bricklayer`` in scope.

    The record's build is created and bound to ``build`` only if ``code`` reads
    that name, so catalog lookups still run while build.py is broken. Runs in a child
    process under the build timeout.
    """
    config = config or Config()
    return build_execution.run_guarded(
        _explore,
        (str(record.build_path), code),
        config.build_timeout,
        lambda e: SnippetResult(
            ok=False,
            output=f"Explore failed: {e}",
            diagnostics=[
                build_execution.subprocess_diagnostic(
                    e, "explore", config.build_timeout
                )
            ],
        ),
    )


def _explore(build_path: str, code: str) -> SnippetResult:
    if not _reads_build(code):
        return _run_snippet(code, {})

    try:
        source = Path(build_path).read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return SnippetResult(
            ok=False,
            output=f"build error:\n{exc}",
            diagnostics=[source_read_diagnostic(exc)],
        )
    digest = sha256(source.encode()).hexdigest()
    stage = "load_build"
    try:
        from ..modules import load_merge, merge_context

        record = Record(Path(build_path).parent)
        context = (
            merge_context(load_merge(record))
            if (record.path / "merge-manifest.json").is_file()
            else nullcontext()
        )
        with context, redirect_stdout(io.StringIO()):
            module = build_execution.load_build_module(build_path, source)
            stage = "create_build"
            build = module.create_build()
    except Exception as exc:
        return SnippetResult(
            ok=False,
            output=f"build error:\n{captured_traceback(exc, build_path, source)}",
            source_sha256=digest,
            diagnostics=[
                exception_diagnostic(exc, stage, filename=build_path, source=source)
            ],
        )
    result = _run_snippet(code, {"build": build})
    return result.model_copy(update={"source_sha256": digest})


def _reads_build(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False  # _run_snippet reports it
    return any(
        isinstance(node, ast.Name)
        and node.id == "build"
        and isinstance(node.ctx, ast.Load)
        for node in ast.walk(tree)
    )


def _run_snippet(code: str, scope: dict) -> SnippetResult:
    """Exec ``code`` with ``bricklayer`` plus ``scope`` available, capturing stdout."""
    import bricklayer

    namespace = {"bricklayer": bricklayer, **scope}
    buf = io.StringIO()
    filename = "<explore>"
    try:
        with redirect_stdout(buf):
            exec(compile(code, filename, "exec"), namespace)
    except Exception as exc:
        printed = build_execution.cap_output(buf.getvalue())
        if printed and not printed.endswith("\n"):
            printed += "\n"
        return SnippetResult(
            ok=False,
            output=f"{printed}explore error:\n{captured_traceback(exc, filename, code)}",
            diagnostics=[
                exception_diagnostic(exc, "explore", filename=filename, source=code)
            ],
        )

    out = build_execution.cap_output(buf.getvalue())
    return SnippetResult(ok=True, output=out or "(explore produced no output)")
