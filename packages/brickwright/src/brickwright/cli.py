"""Create, validate, review, and export LEGO builds through BrickWright."""

import json
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from rich.console import Console

from .actions import (
    FILE,
    RECORD,
    Action,
    FlowError,
    Invocation,
    describe,
    edit,
    provide,
    run,
)
from .config import DEFAULT_RECORDS_DIR, RECORDS_DIR_ENV, Config
from .external_critique import DEFAULT_CRITIQUE_ROUNDS, external_critique_instructions
from .knowledge import read_knowledge, search_knowledge
from .record import Record, RecordNotFound, Timing, ValidationFailures, list_records
from .skill import app as skill_app
from .skill import external_instructions
from .tools import explore as explore_record
from .tools import finalize as finalize_record
from .tools import render as render_record
from .tools import render_angle as render_angle_record
from .tools import validate as validate_record
from .tools.diagnostics import Diagnostic
from .tools.software_rendering import MAX_ELEVATION
from .tools.validation import tally_failure

app = typer.Typer(
    name="brickwright",
    help=__doc__,
    add_completion=False,
    no_args_is_help=True,
)

external_app = typer.Typer(
    help="Operations meant for an external agent.",
    no_args_is_help=True,
)
app.add_typer(external_app, name="external")

knowledge_app = typer.Typer(
    help="Search and read local building knowledge.",
    no_args_is_help=True,
)
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(skill_app, name="skill")

RecordsDir = Annotated[
    Path,
    typer.Option(
        "--records-dir", envvar=RECORDS_DIR_ENV, help="where record directories live"
    ),
]
Prompt = Annotated[str | None, typer.Argument(help="text prompt")]
RecordId = Annotated[str, typer.Argument(help="record id")]
Image = Annotated[Path | None, typer.Option(help="image prompt")]

Flow = Annotated[
    bool,
    typer.Option(
        "--flow/--no-flow",
        envvar="BRICKWRIGHT_FLOW",
        help="use the frozen build and review workflow",
    ),
]
TaskJSON = Annotated[
    Path | None,
    typer.Option(
        "--task-json",
        envvar="BRICKWRIGHT_TASK_JSON",
        help="original task with its ordered checks",
    ),
]
ReviewRounds = Annotated[
    int,
    typer.Option(
        "--review-rounds", min=0, help="valid image review rounds in this flow"
    ),
]
SetFile = Annotated[
    Path | None, typer.Option("--set", help="read a JSON artifact from this file")
]
module_app = typer.Typer(
    help="Build and freeze the modules in a record's plan.", no_args_is_help=True
)
external_app.add_typer(module_app, name="module")


# Defaults for the Config-backed options, so Typer can display them in --help.
_DEFAULTS = Config()
MaxValidations = Annotated[
    int, typer.Option(min=0, help="cap on validate calls (0: unlimited)")
]
BuildTimeout = Annotated[float, typer.Option(help="max seconds per build")]
RenderTimeout = Annotated[float, typer.Option(help="max seconds per render")]
FromId = Annotated[
    str | None, typer.Option("--from", help="fork a new record from this record id")
]

Azimuth = Annotated[
    float,
    typer.Option(help="horizontal camera angle in degrees (-180..180; 0 = front)"),
]
Elevation = Annotated[
    float,
    typer.Option(
        help=(
            f"vertical camera angle in degrees (-{MAX_ELEVATION:g}..{MAX_ELEVATION:g};"
            " 0 = level)"
        )
    ),
]
AngleOut = Annotated[
    Path | None,
    typer.Option("--out", help="output PNG path (default: the record's viewpoint.png)"),
]
CritiqueInstructionsRounds = Annotated[
    int,
    typer.Option(
        "--critique-rounds", help="rounds the printed critique playbook advises"
    ),
]


@knowledge_app.command(name="search")
def knowledge_search(
    query: Annotated[
        list[str], typer.Argument(help="build task, part ID, or diagnostic code")
    ],
    limit: Annotated[int, typer.Option(min=1, help="maximum number of entries")] = 3,
) -> None:
    """Find applicable building guides and print their summaries as JSON."""
    print(search_knowledge(" ".join(query), limit=limit).model_dump_json(indent=2))


@knowledge_app.command(name="show")
def knowledge_show(
    entry_id: Annotated[str, typer.Argument(help="knowledge entry ID")],
) -> None:
    """Print a guide with its example and sources as JSON."""
    try:
        entry = read_knowledge(entry_id)
    except KeyError as exc:
        typer.echo(f"Unknown knowledge entry {entry_id!r}.", err=True)
        raise typer.Exit(1) from exc
    print(entry.model_dump_json(indent=2))


@app.command()
def show(
    record_id: RecordId, records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR)
) -> None:
    """Print a record's metadata."""
    meta = Record.load(Config(records_dir=records_dir), record_id).read_meta()
    Console().print_json(meta.model_dump_json(indent=2))


@app.command()
def ls(records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR)) -> None:
    """List records."""
    console = Console()
    for rec in list_records(Config(records_dir=records_dir)):
        try:
            meta = rec.read_meta()
        except (OSError, ValueError):
            console.print(f"{rec.id}\tunreadable", markup=False, soft_wrap=True)
            continue
        summary = (meta.prompt or "").partition("\n")[0][:48]
        console.print(
            f"{rec.id}\t{meta.status.value}\t{summary}",
            markup=False,  # don't break if summary text has special characters
            soft_wrap=True,  # one line per record even with long records
        )


@external_app.command()
def init(
    prompt: Prompt = None,
    image: Image = None,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    from_id: FromId = None,
    flow: Flow = False,
    task_json: TaskJSON = None,
    review_rounds: ReviewRounds = 2,
) -> None:
    """Initialize a new record and print its ID, build.py path and next step."""
    from .flow.artifacts import write_drafts
    from .flow.guide import flow_next, repair_action

    call = Invocation(records_dir=records_dir, command="init")
    with _flow_errors(call):
        if not prompt and image is None and from_id is None:
            raise FlowError(
                "Provide a prompt, --image, or --from.", code="flow.invalid_input"
            )
        config = Config(
            records_dir=records_dir, flow=flow, flow_review_rounds=review_rounds
        )
        rec = Record.create(
            config,
            prompt=prompt or "",
            image=image,
            from_id=from_id,
            task_path=task_json,
        )
        call = call.model_copy(update={"record_id": rec.id})
        if rec.read_meta().flow:
            write_drafts(rec)
            step = flow_next(rec)
        else:
            step = repair_action(rec, "Write the model in build.py.")
        _emit(
            call,
            {
                "ok": True,
                "record_id": rec.id,
                "build_path": str(rec.build_path.absolute()),
            },
            step=step,
        )


@external_app.command()
def instructions(flow: Flow = False) -> None:
    """Show how to install the host skill and start a record."""
    typer.echo(external_instructions(), nl=False)


@external_app.command()
def validate(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    timeout: BuildTimeout = _DEFAULTS.build_timeout,
    max_validations: MaxValidations = _DEFAULTS.max_validations,
) -> None:
    """Verify a record's correctness."""
    call = Invocation(records_dir=records_dir, record_id=record_id, command="validate")
    with _flow_errors(call):
        config = Config(
            records_dir=records_dir,
            build_timeout=timeout,
            max_validations=max_validations,
        )
        rec = Record.load(config, record_id)
        validations = rec.read_meta().validations
        # Each call is one validation; refuse once the budget is spent.
        if config.max_validations and validations >= config.max_validations:
            raise FlowError(
                f"The validation budget of {config.max_validations} calls is spent. "
                "Stop validating.",
                code="validate.budget_exhausted",
                extra={"next": None},
            )
        with _track_command(rec, "validate"):
            report = validate_record(rec, config)
        failures = rec.read_meta().validation_failures or ValidationFailures()
        rec.update_meta(
            validations=validations + 1,
            validation_failures=tally_failure(failures, report),
        )
        _emit(
            call,
            json.loads(report.agent_json()),
            ok=report.ok,
            step=_after_build(rec, report.ok, report.diagnostics),
        )


@external_app.command()
def explore(
    record_id: RecordId,
    code: Annotated[
        str,
        typer.Option(
            help="Python snippet to run with bricklayer and, when referenced, build"
        ),
    ],
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    timeout: BuildTimeout = _DEFAULTS.build_timeout,
) -> None:
    """Inspect the catalog or the record's build and print its JSON result."""
    call = Invocation(records_dir=records_dir, record_id=record_id, command="explore")
    with _flow_errors(call):
        config = Config(records_dir=records_dir, build_timeout=timeout)
        record = Record.load(config, record_id)
        with _track_command(record, "explore"):
            result = explore_record(record, code, config)
        _emit(call, result.model_dump(mode="json"), ok=result.ok, step=None)


@external_app.command()
def finalize(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    timeout: RenderTimeout = _DEFAULTS.render_timeout,
) -> None:
    """Finalize a record.

    Run this once the build is valid (per ``validate``) and you are finished.
    The finished build is compiled into an LDraw file and, when a parts library
    is available, rendered to a multi-view PNG.
    """
    call = Invocation(records_dir=records_dir, record_id=record_id, command="finalize")
    with _flow_errors(call):
        config = Config(records_dir=records_dir, render_timeout=timeout)
        rec = Record.load(config, record_id)
        with _track_command(rec, "finalize"):
            report = finalize_record(rec, config)
        # Record the elapsed time since the record was created.
        meta = rec.read_meta()
        created = meta.created
        if created is not None:
            timing = meta.timing or Timing()
            total = round((datetime.now(created.tzinfo) - created).total_seconds(), 3)
            rec.update_meta(timing=timing.model_copy(update={"total": total}))
        _emit(
            call,
            json.loads(report.agent_json()),
            ok=report.ok,
            step=_after_build(rec, report.ok, report.diagnostics),
        )


@external_app.command()
def verify_delivery(
    record_id: RecordId,
    file: Annotated[Path, typer.Argument(help="submitted MPD file")],
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
) -> None:
    """Check that an MPD is byte-identical to the current finalized flow snapshot."""
    from .flow.guide import flow_next
    from .tools import finalization

    call = Invocation(
        records_dir=records_dir, record_id=record_id, command="verify-delivery"
    )
    with _flow_errors(call):
        record = _record(record_id, records_dir)
        report = finalization.verify_delivery(record, file)
        result = report.model_dump(mode="json")
        if report.ok:
            _emit(call, result, step=None)
            return
        assert report.error is not None and report.code is not None
        result["diagnostics"] = [
            Diagnostic(
                code=report.code, stage="delivery", message=report.error
            ).model_dump(mode="json")
        ]
        if report.code in ("delivery.mismatch", "delivery.missing_file"):
            step = provide(
                f"Copy {record.ldr_path.resolve()} byte for byte to {file.absolute()}, "
                "then verify it again.",
                run(
                    "Verify the submitted file against the finalized snapshot.",
                    "external",
                    "verify-delivery",
                    RECORD,
                    str(file.absolute()),
                ),
                path=file.absolute(),
            )
        else:
            step = flow_next(record)
        _emit(call, result, ok=False, step=step)


@external_app.command()
def render(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    timeout: RenderTimeout = _DEFAULTS.render_timeout,
    cameras: Annotated[
        Path | None, typer.Option(help="render the frozen MPD at these task cameras")
    ] = None,
) -> None:
    """Render a record's current build.

    Without ``--cameras``, writes the multi-view preview ``out/build.png``.
    With ``--cameras``, renders the checked snapshot at each task camera.
    """
    from .flow.guide import flow_next

    call = Invocation(records_dir=records_dir, record_id=record_id, command="render")
    with _flow_errors(call):
        config = Config(records_dir=records_dir, render_timeout=timeout)
        rec = Record.load(config, record_id)
        flow = rec.read_meta().flow is not None
        if cameras is not None:
            from .aligned_review import render_cameras

            with _track_command(rec, "render"):
                result = render_cameras(rec, cameras, config)
            _emit(call, {"ok": True, **result}, step=flow_next(rec) if flow else None)
            return
        with _track_command(rec, "render"):
            report = render_record(rec, config)
        _emit(
            call,
            report.model_dump(mode="json"),
            ok=report.ok,
            step=flow_next(rec) if flow and report.ok else None,
        )


@external_app.command(name="render-angle")
def render_angle(
    record_id: RecordId,
    azimuth: Azimuth = 0.0,
    elevation: Elevation = 0.0,
    out: AngleOut = None,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    timeout: RenderTimeout = _DEFAULTS.render_timeout,
) -> None:
    """Render the current build from a single camera angle.

    Writes the image (default: the record's ``out/viewpoint.png``) and reports its path.
    """
    call = Invocation(
        records_dir=records_dir, record_id=record_id, command="render-angle"
    )
    with _flow_errors(call):
        config = Config(records_dir=records_dir, render_timeout=timeout)
        rec = Record.load(config, record_id)
        with _track_command(rec, "render-angle"):
            report = render_angle_record(rec, azimuth, elevation, config, out_path=out)
        _emit(call, report.model_dump(mode="json"), ok=report.ok, step=None)


@external_app.command(name="critique-instructions")
def critique_instructions(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    critique_rounds: CritiqueInstructionsRounds = DEFAULT_CRITIQUE_ROUNDS,
) -> None:
    """Print how to match a target image and review the rendered build."""
    rec = Record.load(Config(records_dir=records_dir), record_id)
    try:
        print(external_critique_instructions(rec, rounds=critique_rounds))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _after_build(rec: Record, ok: bool, diagnostics: list[Diagnostic]) -> Action | None:
    """The next step after validating or finalizing a record."""
    from .flow.guide import flow_next, repair_action

    if rec.read_meta().flow is not None:
        return flow_next(rec)
    if ok:
        return flow_next(rec)
    first = next(iter(diagnostics), None)
    summary = (
        (first.next_action or first.message)
        if first is not None
        else "Fix the reported problems in build.py."
    )
    return repair_action(rec, summary)


@contextmanager
def _flow_errors(call: Invocation) -> Generator[None]:
    """Report any workflow failure as one JSON result with a next step."""
    try:
        yield
    except typer.Exit:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        _emit_error(call, exc)
        raise typer.Exit(1) from exc


def _emit_error(call: Invocation, exc: Exception) -> None:
    from pydantic import ValidationError

    from .actions import validation_errors, validation_message
    from .tools.diagnostics import exception_diagnostic

    extra = dict(getattr(exc, "extra", {}) or {})
    if isinstance(exc, RecordNotFound):
        issue = Diagnostic(
            code="record.not_found",
            stage="flow",
            message=f"{exc}. Pass --records-dir or set {RECORDS_DIR_ENV} to the "
            "directory that holds the record.",
            evidence={
                "record_id": exc.record_id,
                "records_dir": str(Path(exc.records_dir).absolute()),
            },
            action=run("List the records in this directory.", "ls"),
        )
    elif isinstance(exc, ValidationError):
        errors = validation_errors(exc)
        issue = Diagnostic(
            code="flow.invalid_input",
            stage="flow",
            message=validation_message(exc.title, errors),
            evidence={"errors": errors},
        )
    else:
        issue = exception_diagnostic(exc, "flow", code="flow.invalid_input")
    if "next" in extra:
        step = extra.pop("next")
    elif issue.action is not None:
        step = issue.action
    else:
        step = _fallback_step(call)
    result = {"ok": False, "error": issue.message, **extra}
    result["diagnostics"] = [issue.model_dump(mode="json")]
    _emit(call, result, ok=False, step=step)


def _fallback_step(call: Invocation) -> Action | None:
    """Use the record's current next step when the error names none."""
    from .flow.guide import flow_next

    if call.record_id is None or call.command == "status":
        return None
    try:
        return flow_next(_record(call.record_id, call.records_dir))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _resolve_tree(value: Any, call: Invocation) -> Any:
    """Resolve each diagnostic's action and drop text repeated in its evidence."""
    if isinstance(value, list):
        return [_resolve_tree(item, call) for item in value]
    if not isinstance(value, dict):
        return value
    value = dict(value)
    evidence = value.get("evidence")
    if (
        "code" in value
        and isinstance(evidence, dict)
        and evidence.get("exception_message") == value.get("message")
    ):
        value["evidence"] = {
            key: item for key, item in evidence.items() if key != "exception_message"
        }
    if (
        "code" in value
        and isinstance(value.get("evidence"), dict)
        and value["evidence"].get("exception_type") == "FlowError"
    ):
        value["evidence"] = {
            key: item
            for key, item in value["evidence"].items()
            if key != "exception_type"
        }
    action = value.get("action")
    if "code" in value and isinstance(action, dict):
        resolved = call.resolve(Action.model_validate(action))
        value["action"] = _action_json(resolved)
        value["next_action"] = describe(resolved)
        return {
            key: item if key in ("action", "next_action") else _resolve_tree(item, call)
            for key, item in value.items()
        }
    return {key: _resolve_tree(item, call) for key, item in value.items()}


def _action_json(action: Action) -> dict[str, Any]:
    """An action as hosts read it: resolved fields only, no internal arguments."""
    data = action.model_dump(mode="json", exclude={"args", "then"}, exclude_none=True)
    data = {key: value for key, value in data.items() if value != []}
    if action.then is not None:
        data["then"] = _action_json(action.then)
    return data


def _emit(
    call: Invocation,
    result: dict[str, Any],
    *,
    ok: bool | None = None,
    step: Action | None = None,
) -> None:
    """Print one JSON result with ``ok`` and ``next``; exit 1 when not ok."""
    data = _resolve_tree(result, call)
    data["ok"] = data.get("ok", True) if ok is None else ok
    if step is None and not data["ok"]:
        step = next(
            (
                Action.model_validate(issue["action"])
                for issue in result.get("diagnostics") or []
                if isinstance(issue, dict) and isinstance(issue.get("action"), dict)
            ),
            None,
        )
    data["next"] = _action_json(call.resolve(step)) if step is not None else None
    for issue in data.get("diagnostics") or []:
        # The chosen step appears once, as next; the diagnostic keeps its summary.
        if (
            data["next"] is not None
            and isinstance(issue, dict)
            and issue.get("action") == data["next"]
        ):
            issue["action"] = None
            issue["next_action"] = data["next"]["summary"]
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if not data["ok"]:
        raise typer.Exit(1)


def _record(record_id: str, records_dir: Path) -> Record:
    return Record.load(Config(records_dir=records_dir), record_id)


def _read_json(path: Path, name: str) -> dict:
    retry = edit(
        f"Write valid JSON in the {name} file and submit it again.",
        FILE,
        run(f"Submit the {name}.", "external", name, RECORD, "--set", FILE),
    )
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        raise FlowError(
            f"The {name} file {path} does not exist.",
            code=f"flow.{name}",
            action=retry,
        ) from None
    except (ValueError, UnicodeError) as exc:
        raise FlowError(
            f"Invalid JSON in {path}: {exc}", code=f"flow.{name}", action=retry
        ) from None
    if not isinstance(data, dict):
        raise FlowError(
            f"The {name} file must hold one JSON object.",
            code=f"flow.{name}",
            action=retry,
        )
    return data


def _artifact(
    record_id: str,
    records_dir: Path,
    name: Literal["features", "plan", "review"],
    path: Path | None,
) -> None:
    from .aligned_review import submit_review
    from .flow import read_artifact, set_artifact

    call = Invocation(
        records_dir=records_dir, record_id=record_id, file=path, command=name
    )
    with _flow_errors(call):
        record = _record(record_id, records_dir)
        if path is None:
            result = read_artifact(record, name)
            _emit(call, result, ok=not result.get("diagnostics"), step=None)
        elif name == "review":
            _emit_review(call, record, submit_review(record, path))
        else:
            from .flow.guide import flow_next

            result = set_artifact(record, name, _read_json(path, name))
            result.pop("schema", None)
            if result.get("diagnostics"):
                result.pop("content", None)
                _emit(call, result, ok=False)
            else:
                _emit(call, result, step=flow_next(record))


def _emit_review(call: Invocation, record: Record, receipt: dict) -> None:
    from .aligned_review import _new_round_action
    from .flow import flow_status
    from .flow.guide import flow_next

    state = flow_status(record)
    budget = record.read_meta().flow_review_rounds
    used = state["review"]["rounds"]
    status = receipt["status"]
    result = {key: value for key, value in receipt.items() if key != "report"}
    result.update(
        receipt=status,
        counted=status == "accepted",
        rounds={"used": used, "budget": budget, "remaining": max(budget - used, 0)},
        deliverable=bool(state["ready"]),
    )
    if status == "failed":
        _emit(call, result, step=_new_round_action())
    elif status == "stale":
        result["error"] = (
            "The report was archived as stale: the snapshot or its evidence changed "
            "after this round was prepared. It does not count as a review round."
        )
        result["diagnostics"] = [
            Diagnostic(
                code="review.round_stale", stage="review", message=result["error"]
            ).model_dump(mode="json")
        ]
        _emit(call, result, ok=False, step=flow_next(record, state))
    else:
        _emit(call, result, step=flow_next(record, state))


@external_app.command()
def features(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    set_file: SetFile = None,
) -> None:
    """Read the feature schema and current checklist, or submit a new checklist."""
    _artifact(record_id, records_dir, "features", set_file)


@external_app.command()
def plan(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    set_file: SetFile = None,
) -> None:
    """Read the plan schema and current plan, or submit a new plan."""
    _artifact(record_id, records_dir, "plan", set_file)


@external_app.command()
def review(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    set_file: SetFile = None,
) -> None:
    """Inspect the current review or submit a self-review or independent report."""
    _artifact(record_id, records_dir, "review", set_file)


@external_app.command()
def status(
    record_id: RecordId, records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR)
) -> None:
    """Show the current snapshot, every release gate and the one next step."""
    from .flow import flow_status
    from .flow.guide import flow_next

    call = Invocation(records_dir=records_dir, record_id=record_id, command="status")
    with _flow_errors(call):
        record = _record(record_id, records_dir)
        state = flow_status(record)
        _emit(call, {"ok": True, **state}, step=flow_next(record, state))


@external_app.command()
def compare(
    record_id: RecordId,
    reference_dir: Annotated[Path, typer.Option()],
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
) -> None:
    """Compare each fixed-camera view with goal-<camera>.png in the reference directory."""
    from .aligned_review import compare_record
    from .flow.guide import flow_next

    call = Invocation(records_dir=records_dir, record_id=record_id, command="compare")
    with _flow_errors(call):
        record = _record(record_id, records_dir)
        result = compare_record(record, reference_dir)
        _emit(call, {"ok": True, **result}, step=flow_next(record))


@external_app.command(name="review-instructions")
def aligned_instructions(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    parent_session_id: Annotated[str | None, typer.Option()] = None,
    azimuth: Annotated[
        float | None, typer.Option(help="externally matched horizontal angle")
    ] = None,
    elevation: Annotated[
        float | None, typer.Option(help="externally matched vertical angle")
    ] = None,
) -> None:
    """Freeze a review round and report its packet, report template and instructions."""
    from .aligned_review import prepare_review

    call = Invocation(
        records_dir=records_dir, record_id=record_id, command="review-instructions"
    )
    with _flow_errors(call):
        if (azimuth is None) != (elevation is None):
            raise FlowError(
                "Pass both --azimuth and --elevation for a matched view.",
                code="flow.invalid_input",
            )
        record = _record(record_id, records_dir)
        angle = None if azimuth is None or elevation is None else (azimuth, elevation)
        prepared = prepare_review(
            record, parent_session_id=parent_session_id, matched_angle=angle
        )
        step = prepared.pop("next")
        if prepared["mode"] == "review":
            prepared.pop("instructions")
        _emit(call, {"ok": True, **prepared}, step=step)


@module_app.command(name="init")
def module_init(
    record_id: RecordId,
    module_id: str,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
) -> None:
    """Create a child record in the parent's module coordinate system."""
    from .modules import init_module

    call = Invocation(records_dir=records_dir, record_id=record_id, command="module")
    with _flow_errors(call):
        child = init_module(_record(record_id, records_dir), module_id)
        child_id = str(child.path.relative_to(records_dir))
        step = edit(
            "Build the module in its build.py, then validate it.",
            child.build_path.absolute(),
            run("Validate the module.", "external", "validate", child_id),
        )
        _emit(
            call,
            {
                "ok": True,
                "record_id": child_id,
                "build_path": str(child.build_path.absolute()),
            },
            step=step,
        )


@module_app.command(name="status")
def module_status_command(
    record_id: RecordId, records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR)
) -> None:
    """List child records, interface checks and stale contracts."""
    from .modules import module_status

    call = Invocation(records_dir=records_dir, record_id=record_id, command="module")
    with _flow_errors(call):
        _emit(call, {"ok": True, **module_status(_record(record_id, records_dir))})


@module_app.command(name="instructions")
def module_instruction_command(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    session_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Show the module contract and record a builder session when supplied."""
    from .modules import module_instructions

    call = Invocation(records_dir=records_dir, record_id=record_id, command="module")
    with _flow_errors(call):
        text = module_instructions(
            _record(record_id, records_dir), session_id=session_id
        )
        _emit(call, {"ok": True, "instructions": text})


@module_app.command(name="finish")
def module_finish(
    record_id: RecordId,
    session_id: Annotated[str, typer.Option()],
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
) -> None:
    """Record the end of the independent module session."""
    from .modules import finish_module_session

    call = Invocation(records_dir=records_dir, record_id=record_id, command="module")
    with _flow_errors(call):
        result = finish_module_session(_record(record_id, records_dir), session_id)
        _emit(call, {"ok": True, **result})


@module_app.command(name="merge")
def module_merge(
    record_id: RecordId, records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR)
) -> None:
    """Select and freeze all checked module snapshots for the parent."""
    from .modules import prepare_merge

    call = Invocation(records_dir=records_dir, record_id=record_id, command="module")
    with _flow_errors(call):
        merge = prepare_merge(_record(record_id, records_dir))
        if merge is None:
            raise FlowError("The plan contains no modules.", code="flow.invalid_input")
        _emit(call, {"ok": True, "manifest_sha256": merge.manifest_sha256})


@contextmanager
def _track_command(rec: Record, name: str) -> Generator[None]:
    """Time the wrapped command and tally it on the record's meta.

    The wall-clock is added to this command's entry in the timing breakdown, and
    the call is counted toward ``tool_calls`` under ``name``.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        dt = time.perf_counter() - start
        meta = rec.read_meta()
        timing = meta.timing or Timing()
        tools = {**timing.tools, name: round(timing.tools.get(name, 0.0) + dt, 3)}
        rec.update_meta(
            timing=timing.model_copy(update={"tools": tools}),
            tool_calls={**meta.tool_calls, name: meta.tool_calls.get(name, 0) + 1},
        )


def main(argv: list[str] | None = None) -> None:
    """Run the CLI; usage errors also print one JSON result."""
    import sys

    try:
        from typer._click import ClickException
    except ImportError:  # Typer releases that use Click directly
        from click import ClickException

    try:
        code = app(args=argv, standalone_mode=False)
    except ClickException as exc:
        if type(exc).__name__ == "NoArgsIsHelpError":
            exc.show()
            sys.exit(2)
        context = getattr(exc, "ctx", None)
        step = None
        if context is not None:
            path = context.command_path.split()[1:]
            step = run("Read this command's arguments.", *path, "--help")
        issue = Diagnostic(code="cli.usage", stage="cli", message=exc.format_message())
        payload = {
            "ok": False,
            "error": issue.message,
            "diagnostics": [issue.model_dump(mode="json")],
            "next": None
            if step is None
            else Invocation(records_dir=Path(DEFAULT_RECORDS_DIR))
            .resolve(step)
            .model_dump(mode="json"),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        sys.exit(1)
    except typer.Abort:
        sys.exit(1)
    if isinstance(code, int) and code:
        sys.exit(code)


if __name__ == "__main__":
    main()
