"""Command-line interface to generate LEGO builds using BrickWright.

Top-level commands are for a human orchestrating runs; the ``external`` group
holds the operations an external agent (e.g. Claude Code pointed at the
package) drives to iterate on a build.
"""

import json
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from rich.console import Console

from .config import (
    DEFAULT_RECORDS_DIR,
    RECORDS_DIR_ENV,
    Config,
    CritiqueMode,
    Effort,
)
from .driver import run_agent
from .driver.external_critique import (
    DEFAULT_CRITIQUE_ROUNDS,
    external_critique_instructions,
)
from .driver.gallery import print_gallery
from .driver.tracking import tally_failure
from .knowledge import read_knowledge, search_knowledge
from .prompts import system_prompt
from .record import Record, Timing, ValidationFailures, list_records
from .tools import FinalizationReport, RenderReport, SnippetResult, ValidationReport
from .tools import explore as explore_record
from .tools import finalize as finalize_record
from .tools import render as render_record
from .tools import render_angle as render_angle_record
from .tools import validate as validate_record
from .tools.software_rendering import MAX_ELEVATION

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
        "--review-rounds", min=0, help="valid independent review rounds in this flow"
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
Model = Annotated[
    str | None,
    typer.Option(envvar="BRICKWRIGHT_GENERATION_MODEL", help="generation model id"),
]
Effort_ = Annotated[Effort | None, typer.Option(help="reasoning effort")]
MaxValidations = Annotated[
    int, typer.Option(min=0, help="cap on validate calls (0: unlimited)")
]
MaxTurns = Annotated[int, typer.Option(min=0, help="cap on model turns (0: unlimited)")]
MaxCost = Annotated[float, typer.Option(help="cap on USD spent per run")]
BuildTimeout = Annotated[float, typer.Option(help="max seconds per build")]
RenderTimeout = Annotated[float, typer.Option(help="max seconds per render")]
RequestTimeout = Annotated[
    float | None,
    typer.Option(help="max seconds per LLM request (unset: the client's own default)"),
]
Subagents = Annotated[
    bool,
    typer.Option(
        "--use-subagents/--no-use-subagents",
        help="allow dispatching subagents to complete subtasks in parallel",
    ),
]
MaxParallelSubagents = Annotated[
    int, typer.Option(help="cap on concurrently running subagents")
]
SubagentMaxTurns = Annotated[int, typer.Option(help="cap on model turns per subagent")]
SubagentMaxCost = Annotated[float, typer.Option(help="cap on USD spent per subagent")]
CritiqueRounds = Annotated[
    int, typer.Option(help="outer visual-critique rounds (0 disables the loop)")
]
CritiqueMode_ = Annotated[
    CritiqueMode, typer.Option(help="how the builder re-enters after a critique")
]
CritiqueModel = Annotated[
    str | None, typer.Option(help="critic model id (defaults to --model)")
]
CritiqueEffort = Annotated[
    Effort | None, typer.Option(help="critic reasoning effort (defaults to --effort)")
]
ViewpointIterations = Annotated[
    int,
    typer.Option(
        help="viewpoint-matching iterations per critique round (0 disables the critique)"
    ),
]
ViewpointModel = Annotated[
    str | None, typer.Option(help="viewpoint agent model id (defaults to --model)")
]
ViewpointEffort = Annotated[
    Effort | None, typer.Option(help="viewpoint agent effort (defaults to --effort)")
]

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
        str, typer.Argument(help="build task, part ID, or diagnostic code")
    ],
    limit: Annotated[int, typer.Option(min=1, help="maximum number of entries")] = 3,
) -> None:
    """Find applicable building guides and print their summaries as JSON."""
    print(search_knowledge(query, limit=limit).model_dump_json(indent=2))


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
def generate(
    prompt: Prompt = None,
    image: Image = None,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    model: Model = _DEFAULTS.model,
    effort: Effort_ = _DEFAULTS.effort,
    max_validations: MaxValidations = _DEFAULTS.max_validations,
    max_turns: MaxTurns = _DEFAULTS.max_turns,
    max_cost: MaxCost = _DEFAULTS.max_cost,
    build_timeout: BuildTimeout = _DEFAULTS.build_timeout,
    render_timeout: RenderTimeout = _DEFAULTS.render_timeout,
    request_timeout: RequestTimeout = _DEFAULTS.request_timeout,
    use_subagents: Subagents = _DEFAULTS.subagents_enabled,
    max_parallel_subagents: MaxParallelSubagents = _DEFAULTS.max_parallel_subagents,
    subagent_max_turns: SubagentMaxTurns = _DEFAULTS.subagent_max_turns,
    subagent_max_cost: SubagentMaxCost = _DEFAULTS.subagent_max_cost,
    critique_rounds: CritiqueRounds = _DEFAULTS.max_critique_rounds,
    critique_mode: CritiqueMode_ = _DEFAULTS.critique_mode,
    critique_model: CritiqueModel = _DEFAULTS.critique_model,
    critique_effort: CritiqueEffort = _DEFAULTS.critique_effort,
    viewpoint_iterations: ViewpointIterations = _DEFAULTS.max_viewpoint_iterations,
    viewpoint_model: ViewpointModel = _DEFAULTS.viewpoint_model,
    viewpoint_effort: ViewpointEffort = _DEFAULTS.viewpoint_effort,
    from_id: FromId = None,
    flow: Flow = False,
    task_json: TaskJSON = None,
    review_rounds: ReviewRounds = 2,
) -> None:
    """Generate a new LEGO build record from a text or image prompt."""
    config = Config(
        records_dir=records_dir,
        flow=flow,
        flow_review_rounds=review_rounds,
        model=model,
        effort=effort,
        max_validations=max_validations,
        max_turns=max_turns,
        max_cost=max_cost,
        build_timeout=build_timeout,
        render_timeout=render_timeout,
        request_timeout=request_timeout,
        subagents_enabled=use_subagents,
        max_parallel_subagents=max_parallel_subagents,
        subagent_max_turns=subagent_max_turns,
        subagent_max_cost=subagent_max_cost,
        max_critique_rounds=critique_rounds,
        critique_mode=critique_mode,
        critique_model=critique_model,
        critique_effort=critique_effort,
        max_viewpoint_iterations=viewpoint_iterations,
        viewpoint_model=viewpoint_model,
        viewpoint_effort=viewpoint_effort,
    )
    rec = _create_record(
        config, prompt=prompt, image=image, from_id=from_id, task_json=task_json
    )
    if not run_agent(rec, config).succeeded:
        raise typer.Exit(1)


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


@app.command(hidden=True)
def gallery() -> None:
    """Print a style guide of every narration output (for testing purposes)."""
    print_gallery()


@external_app.command()
def init(
    prompt: Prompt = None,
    image: Image = None,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    model: Model = None,
    effort: Effort_ = None,
    from_id: FromId = None,
    flow: Flow = False,
    task_json: TaskJSON = None,
    review_rounds: ReviewRounds = 2,
) -> None:
    """Initialize a new record.

    Prints the new record's id and path to the build.py file to be edited.
    An external agent should edit the build.py file directly, then run
    ``brickwright external validate <record_id>`` to validate it.
    """
    config = Config(
        records_dir=records_dir,
        model=model,
        effort=effort,
        flow=flow,
        flow_review_rounds=review_rounds,
    )
    rec = _create_record(
        config,
        prompt=prompt,
        image=image,
        from_id=from_id,
        task_json=task_json,
        external=True,
    )
    print(f"record_id={rec.id}")
    print(f"build_path={rec.build_path.absolute()}")


@external_app.command()
def instructions(flow: Flow = False) -> None:
    print(system_prompt(external=True, config=Config(flow=flow)))


@external_app.command()
def validate(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    timeout: BuildTimeout = _DEFAULTS.build_timeout,
    max_validations: MaxValidations = _DEFAULTS.max_validations,
) -> None:
    """Verify a record's correctness."""
    config = Config(
        records_dir=records_dir,
        build_timeout=timeout,
        max_validations=max_validations,
    )
    rec = Record.load(config, record_id)

    validations = rec.read_meta().validations
    # Each call is one validation; refuse once the budget is spent.
    if config.max_validations and validations >= config.max_validations:
        typer.echo(
            f"validation budget of {config.max_validations} reached; stop validating",
            err=True,
        )
        raise typer.Exit(1)
    with _track_command(rec, "validate"):
        report = validate_record(rec, config)
    failures = rec.read_meta().validation_failures or ValidationFailures()
    rec.update_meta(
        validations=validations + 1,
        validation_failures=tally_failure(failures, report),
    )

    _print_report(report)


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
    config = Config(records_dir=records_dir, build_timeout=timeout)
    record = Record.load(config, record_id)
    with _track_command(record, "explore"):
        result = explore_record(record, code, config)
    _print_report(result)


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
    config = Config(records_dir=records_dir, render_timeout=timeout)
    rec = Record.load(config, record_id)

    with _track_command(rec, "finalize"):
        report = finalize_record(rec, config)

    # Close out the run: total wall-clock from creation, remainder attributed to llm.
    meta = rec.read_meta()
    created = meta.created
    if created is not None:
        timing = meta.timing or Timing()
        total = round((datetime.now(created.tzinfo) - created).total_seconds(), 3)
        llm = round(max(0.0, total - sum(timing.tools.values())), 3)
        rec.update_meta(timing=timing.model_copy(update={"total": total, "llm": llm}))

    _print_report(report)


@external_app.command()
def render(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    timeout: RenderTimeout = _DEFAULTS.render_timeout,
    cameras: Annotated[
        Path | None, typer.Option(help="render the frozen MPD at these task cameras")
    ] = None,
) -> None:
    """Render a record's current build to a multi-view image.

    Writes ``out/build.png`` and prints its path. Open that file to see how the
    build currently looks (front, side, top, and isometric views).
    """
    config = Config(records_dir=records_dir, render_timeout=timeout)
    rec = Record.load(config, record_id)

    if cameras is not None:
        from .aligned_review import render_cameras

        with _flow_errors(), _track_command(rec, "render"):
            _print_json(render_cameras(rec, cameras, config))
        return
    with _track_command(rec, "render"):
        report = render_record(rec, config)
    _print_image_and_report(report)


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

    Writes the image (default: the record's ``out/viewpoint.png``) and prints its path.
    """
    config = Config(records_dir=records_dir, render_timeout=timeout)
    rec = Record.load(config, record_id)

    with _track_command(rec, "render-angle"):
        report = render_angle_record(rec, azimuth, elevation, config, out_path=out)
    _print_image_and_report(report)


@external_app.command(name="critique-instructions")
def critique_instructions(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    critique_rounds: CritiqueInstructionsRounds = DEFAULT_CRITIQUE_ROUNDS,
) -> None:
    """Print the visual-critique playbook for this record.

    The external counterpart of the driver's ``viewpoint`` critique loop. It tells you to
    dispatch two fresh subagents -- one to match the camera viewpoint (via
    ``render-angle``), one to critique the render against the reference image -- each with
    the same system prompt the driver path uses, then to revise ``build.py`` from the
    critic's verdict. Requires the record to have a prompt image.
    """
    rec = Record.load(Config(records_dir=records_dir), record_id)
    try:
        print(external_critique_instructions(rec, rounds=critique_rounds))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@contextmanager
def _flow_errors() -> Generator[None]:
    """Return machine-readable input errors to the external agent."""
    from bricklayer.diagnostics import DiagnosticError

    from .tools.diagnostics import exception_diagnostic

    try:
        yield
    except (ValueError, OSError, RuntimeError, DiagnosticError) as exc:
        issue = exception_diagnostic(exc, "flow", code="flow.invalid_input")
        print(
            json.dumps(
                {"error": str(exc), "diagnostics": [issue.model_dump(mode="json")]},
                ensure_ascii=False,
            )
        )
        raise typer.Exit(1) from exc


def _print_json(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _record(record_id: str, records_dir: Path) -> Record:
    return Record.load(Config(records_dir=records_dir), record_id)


def _artifact(
    record_id: str,
    records_dir: Path,
    name: Literal["features", "plan", "review"],
    path: Path | None,
) -> None:
    from .aligned_review import submit_review
    from .flow import read_artifact, set_artifact

    with _flow_errors():
        record = _record(record_id, records_dir)
        if path is None:
            result = read_artifact(record, name)
        elif name == "review":
            result = submit_review(record, path)
        else:
            try:
                data = json.loads(path.read_bytes())
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            result = set_artifact(record, name, data)
        _print_json(result)
        if result.get("diagnostics") or result.get("ok") is False:
            raise typer.Exit(1)


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
    """Inspect the current review, or submit the independent session's report."""
    _artifact(record_id, records_dir, "review", set_file)


@external_app.command()
def status(
    record_id: RecordId, records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR)
) -> None:
    """Show current snapshot, release gates and next actions."""
    from .flow import flow_status

    with _flow_errors():
        _print_json(flow_status(_record(record_id, records_dir)))


@external_app.command()
def compare(
    record_id: RecordId,
    reference_dir: Annotated[Path, typer.Option()],
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
) -> None:
    """Compare each fixed-camera view against the same named reference image."""
    from .aligned_review import compare_record

    with _flow_errors():
        _print_json(compare_record(_record(record_id, records_dir), reference_dir))


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
    """Freeze the inputs and print the task for a fresh image review session."""
    from .aligned_review import review_instructions

    with _flow_errors():
        if (azimuth is None) != (elevation is None):
            raise ValueError("Pass both --azimuth and --elevation for a matched view.")
        record = _record(record_id, records_dir)
        if azimuth is None or elevation is None:
            print(review_instructions(record, parent_session_id=parent_session_id))
        else:
            print(
                review_instructions(
                    record,
                    parent_session_id=parent_session_id,
                    matched_angle=(azimuth, elevation),
                )
            )


@module_app.command(name="init")
def module_init(
    record_id: RecordId,
    module_id: str,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
) -> None:
    """Create a child record in the parent's module coordinate system."""
    from .modules import init_module

    with _flow_errors():
        child = init_module(
            _record(record_id, records_dir), module_id, Config(records_dir=records_dir)
        )
        print(f"record_id={child.path.relative_to(records_dir)}")
        print(f"build_path={child.build_path.absolute()}")


@module_app.command(name="status")
def module_status_command(
    record_id: RecordId, records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR)
) -> None:
    """List child records, interface checks and stale contracts."""
    from .modules import module_status

    with _flow_errors():
        _print_json(module_status(_record(record_id, records_dir)))


@module_app.command(name="instructions")
def module_instruction_command(
    record_id: RecordId,
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
    session_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Show the module contract and record a builder session when supplied."""
    from .modules import module_instructions

    with _flow_errors():
        print(
            module_instructions(_record(record_id, records_dir), session_id=session_id)
        )


@module_app.command(name="finish")
def module_finish(
    record_id: RecordId,
    session_id: Annotated[str, typer.Option()],
    records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR),
) -> None:
    """Record the end of the independent module session."""
    from .modules import finish_module_session

    with _flow_errors():
        _print_json(finish_module_session(_record(record_id, records_dir), session_id))


@module_app.command(name="merge")
def module_merge(
    record_id: RecordId, records_dir: RecordsDir = Path(DEFAULT_RECORDS_DIR)
) -> None:
    """Select and freeze all checked module snapshots for the parent."""
    from .modules import prepare_merge

    with _flow_errors():
        merge = prepare_merge(_record(record_id, records_dir))
        if merge is None:
            raise ValueError("The plan contains no modules.")
        _print_json({"manifest_sha256": merge.manifest_sha256})


def _create_record(
    config: Config,
    *,
    prompt: str | None,
    image: Path | None,
    from_id: str | None,
    external: bool = False,
    task_json: Path | None = None,
) -> Record:
    if not prompt and image is None and from_id is None:
        raise typer.BadParameter("provide a prompt, --image, or --from")
    with _flow_errors():
        return Record.create(
            config,
            prompt=prompt or "",
            image=image,
            from_id=from_id,
            external=external,
            task_path=task_json,
        )


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


def _print_image_and_report(report: RenderReport) -> None:
    """Print the rendered image's path (when there is one), then the report."""
    if report.ok and report.image_path is not None:
        print(str(report.image_path.absolute()))
    _print_report(report)


def _print_report(
    report: ValidationReport | FinalizationReport | RenderReport | SnippetResult,
) -> None:
    """Print the report as JSON and exit nonzero if the operation failed."""
    if isinstance(report, (ValidationReport, FinalizationReport)):
        print(report.agent_json(indent=2))
    else:
        print(report.model_dump_json(indent=2))
    if not report.ok:
        raise typer.Exit(1)


def main(argv: list[str] | None = None) -> None:
    app(args=argv)


if __name__ == "__main__":
    main()
