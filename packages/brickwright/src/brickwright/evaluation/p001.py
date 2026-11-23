"""Freeze and run the paired connector-placement evaluation for P-001."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from math import isclose, isfinite
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ARMS = ("old", "new")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
CAPTURED_FILES = ("build.py", "out/build.ldr", "out/build.png", "out/report.json")
DISABLED = {
    "subagents_enabled": False,
    "is_subagent": False,
    "max_critique_rounds": 0,
    "max_viewpoint_iterations": 0,
}
TASKS = (
    {
        "id": "side-sign",
        "prompt": (
            "Build a tabletop LEGO wayfinding sign in at most 35 pieces. Give it a dark "
            "upright pedestal with a stable base. A white or yellow vertical sign face "
            "should face outward from one side of the pedestal, clear of the tabletop. "
            "Keep the whole model within a 6 by 6 stud footprint."
        ),
        "requirements": (
            "Stable dark pedestal and distinct white or yellow vertical side sign. "
            "The sign sits clear of the tabletop and stays with the pedestal when lifted. "
            "At most 35 pieces and 6 by 6 studs."
        ),
    },
    {
        "id": "lifting-hatch",
        "prompt": (
            "Build a small LEGO cargo box with a contrasting hatch over a top opening, "
            "using at most 45 pieces. When shut, the hatch lies flat across the opening. "
            "It should open upward from shut to at least 60 degrees, exposing the opening "
            "while the box stays on the table and the hatch stays in place on the box."
        ),
        "requirements": (
            "A fixed cargo box with a distinct, contrasting hatch covering its opening "
            "when horizontal. Opens upward through 0, 15, 30, 45 and 60 degrees without "
            "interference or detachment. At most 45 pieces."
        ),
    },
    {
        "id": "spinning-wheel",
        "prompt": (
            "Build a small LEGO tabletop display in at most 35 pieces. Show a red wheel "
            "upright above the table in a dark frame. Mark one point on the wheel with a "
            "visible detail. The wheel should turn a full revolution in place without "
            "rubbing or coming loose while the frame stays still."
        ),
        "requirements": (
            "Upright red wheel above the table in a stable dark frame with a visible "
            "reference mark. Wheel turns 360 degrees in place without interference or "
            "detachment. At most 35 pieces."
        ),
    },
    {
        "id": "adjustable-lamp",
        "prompt": (
            "Build a small LEGO desk lamp in at most 40 pieces. Give it a stable stand "
            "and a distinct bright head. The head should be able to point straight "
            "ahead and tilt at least 30 degrees up and 30 degrees down, with the stand "
            "staying put and the head staying in place on the lamp."
        ),
        "requirements": (
            "Recognizable lamp with a stable stand and distinct bright head. Head can aim "
            "forward and tilt 30 degrees each way without interference or detachment. "
            "At most 40 pieces."
        ),
    },
    {
        "id": "brick-wall",
        "prompt": (
            "Build a freestanding light gray LEGO wall 8 studs long, 2 studs deep, and "
            "3 standard bricks tall, with a neat dark top accent included in that height. "
            "Stagger the visible vertical seams on successive rows. Use at most 30 pieces."
        ),
        "requirements": (
            "One stable wall measuring 8 by 2 studs and 3 bricks tall, in light gray "
            "with a dark top accent and staggered vertical seams. At most 30 pieces."
        ),
    },
)
RUBRIC = {
    "structure": (
        "0: serious collision, unsupported parts, or major gaps in construction. "
        "1: stands but has a weak or partly detached assembly. "
        "2: supported, collision-free, and coherent under intended use."
    ),
    "appearance": (
        "0: hard to identify or visibly unfinished. 1: recognizable with rough shaping "
        "or finish. 2: clear silhouette, deliberate color placement, and clean finish."
    ),
    "function": (
        "0: the core function is absent or physically impossible. 1: it works partly or "
        "the full required range cannot be established. 2: all requested functions work. "
        "Judge physical geometry and equivalent pose evidence. A declared motion range "
        "alone is insufficient, and a well-supported physical inference is acceptable."
    ),
    "complete": (
        "True only when the final product meets every task requirement, including its "
        "stated constraints, with structure and function both scored 2."
    ),
    "quality": (
        "Add the three 0–2 dimensions for each final artifact and compare the mean "
        "over final artifacts in each arm. Completion uses every frozen trial as its "
        "denominator, with missing products incomplete."
    ),
}


@dataclass(frozen=True)
class Slot:
    arm: str
    task: dict[str, str]
    repetition: int

    @property
    def key(self) -> str:
        return f"{self.arm}/{self.task['id']}/{self.repetition}"

    def directory(self, output: Path) -> Path:
        return output / "private" / "runs" / self.key


def make_plan(
    *,
    model: str,
    effort: str,
    repetitions: int,
    max_turns: int,
    max_validations: int,
    max_cost: float | None,
    build_timeout: float,
    render_timeout: float,
    request_timeout: float,
    trial_timeout: float | None = None,
) -> dict[str, Any]:
    if not model.strip():
        raise ValueError("model must be explicit")
    if effort not in EFFORTS:
        raise ValueError(f"invalid effort: {effort!r}")
    for name, value in (
        ("repetitions", repetitions),
        ("max_turns", max_turns),
        ("max_validations", max_validations),
        ("build_timeout", build_timeout),
        ("render_timeout", render_timeout),
        ("request_timeout", request_timeout),
    ):
        if not isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if max_cost is not None and (not isfinite(max_cost) or max_cost <= 0):
        raise ValueError("max_cost must be finite and positive")
    if max_cost is None and trial_timeout is None:
        raise ValueError("unpriced evaluation requires a fixed trial_timeout")
    if trial_timeout is not None and (
        not isfinite(trial_timeout) or trial_timeout <= 0
    ):
        raise ValueError("trial_timeout must be finite and positive")
    return {
        "protocol": "P-001/U-05/v1",
        **({"trial_timeout": trial_timeout} if trial_timeout is not None else {}),
        "repetitions": repetitions,
        "settings": {
            "model": model,
            "effort": effort,
            "max_turns": max_turns,
            "max_validations": max_validations,
            "max_cost": max_cost,
            "build_timeout": build_timeout,
            "render_timeout": render_timeout,
            "request_timeout": request_timeout,
            **DISABLED,
        },
        "tasks": [dict(task) for task in TASKS],
        "rubric": dict(RUBRIC),
        "placement_metric": (
            "Count final leaf occurrences whose pose was solved through successful "
            "connector placement, including the descendants of a connected subassembly "
            "and counting each occurrence once. Divide the sum by all final leaves."
        ),
    }


def slots(plan: dict[str, Any]) -> Iterator[Slot]:
    for index, (repetition, task) in enumerate(
        (repetition, task)
        for repetition in range(1, plan["repetitions"] + 1)
        for task in plan["tasks"]
    ):
        for arm in ARMS if index % 2 == 0 else ARMS[::-1]:
            yield Slot(arm, task, repetition)


def json_data(path: Path) -> Any:
    return json.loads(path.read_bytes())


def write_json(path: Path, value: Any) -> None:
    data = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(data, encoding="utf-8")
    temporary.replace(path)


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def freeze_plan(output: Path, plan: dict[str, Any]) -> None:
    private = output / "private"
    private.mkdir(mode=0o700, parents=True)
    plan_path = private / "plan.json"
    write_json(plan_path, plan)
    (private / "plan.sha256").write_text(digest(plan_path) + "\n")


def load_plan(output: Path) -> dict[str, Any]:
    plan_path = output / "private" / "plan.json"
    saved_sha = (plan_path.parent / "plan.sha256").read_text().strip()
    if digest(plan_path) != saved_sha:
        raise ValueError("frozen plan has changed")
    return json_data(plan_path)


@dataclass(frozen=True)
class Provider:
    """An explicit OpenAI Responses endpoint, paired with its key file."""

    api_base: str
    key_file: Path

    def __post_init__(self) -> None:
        url = urlsplit(self.api_base)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError("evaluation provider must be an HTTPS base URL")
        if not self.key_file.is_file():
            raise ValueError("evaluation key file does not exist")


def generate_command(
    directory: Path,
    checkout: Path,
    plan: dict[str, Any],
    task: dict[str, str],
    provider: Provider | None = None,
) -> list[str]:
    settings = plan["settings"]
    command = [
        str(checkout / ".venv" / "bin" / "python"),
        "-I",
        str(Path(__file__).with_name("p001_native.py").resolve()),
        str(directory / "responses.jsonl"),
    ]
    if settings["max_cost"] is None:
        command.append("--unpriced")
    if provider is not None:
        command.extend(
            ("--api-base", provider.api_base, "--api-key-file", str(provider.key_file))
        )
    command.extend(
        (
            "generate",
            "--records-dir",
            str(directory / "records"),
        )
    )
    for name, value in settings.items():
        if name not in DISABLED:
            command.extend(
                (f"--{name.replace('_', '-')}", "inf" if value is None else str(value))
            )
    return command + [
        "--no-use-subagents",
        "--critique-rounds",
        "0",
        "--viewpoint-iterations",
        "0",
        "--",
        task["prompt"],
    ]


def check_generation(
    slot: Slot, plan: dict[str, Any], metadata: dict[str, Any]
) -> None:
    if metadata.get("prompt") != slot.task["prompt"]:
        raise ValueError(f"{slot.key}: recorded prompt differs from the frozen task")
    generation = metadata.get("generation") or {}
    config = generation.get("config") or {}
    if generation.get("external") is not False or any(
        metadata.get(name) is not None for name in ("forked_from", "prompt_image")
    ):
        raise ValueError(f"{slot.key}: expected a fresh native text run")
    for name, expected in plan["settings"].items():
        if config.get(name) != expected or (
            name in {"model", "effort"} and generation.get(name) != expected
        ):
            raise ValueError(
                f"{slot.key}: recorded {name} differs from the frozen plan"
            )


_PROBE = """
import json
import sys
from pathlib import Path

import bricklayer
import brickwright
from brickwright.config import Config
from brickwright.driver.completion import _route
from brickwright.driver.messages import request_kwargs
from ldraw2mesh.library import LDrawLibraryNotFound, resolve_library

for module in (bricklayer, brickwright):
    if not Path(module.__file__).resolve().is_relative_to(Path.cwd() / 'packages'):
        raise SystemExit(f'{module.__name__} is not installed from this checkout')
try:
    library = str(resolve_library().resolve())
except LDrawLibraryNotFound:
    library = None
config = Config.model_validate_json(sys.argv[1])
print(json.dumps({
    'request': _route(request_kwargs(config)),
    'python': sys.version,
    'ldraw_library': library,
}))
"""


def _git(checkout: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=checkout, text=True).strip()


def checkout_commit(checkout: Path) -> str:
    if Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout:
        raise ValueError(f"expected checkout root: {checkout}")
    if _git(checkout, "--no-optional-locks", "status", "--porcelain"):
        raise ValueError(f"working tree must be clean: {checkout}")
    return _git(checkout, "rev-parse", "HEAD")


def _arm_path(output: Path, arm: str) -> Path:
    return output / "private" / f"{arm}.json"


def _bind_arm(
    output: Path,
    arm: str,
    checkout: Path,
    plan: dict[str, Any],
    provider: Provider | None = None,
) -> dict:
    checkout = checkout.resolve()
    commit = checkout_commit(checkout)
    python = checkout / ".venv" / "bin" / "python"
    if not python.is_file():
        raise ValueError(f"run uv sync --locked in {checkout} first")
    probe_settings = dict(plan["settings"])
    if probe_settings["max_cost"] is None:
        del probe_settings["max_cost"]
    probe = subprocess.check_output(
        [str(python), "-I", "-c", _PROBE, json.dumps(probe_settings)],
        cwd=checkout,
        text=True,
        env=_provider_env(provider),
    )
    runtime = json.loads(probe)
    if runtime["ldraw_library"]:
        colors = Path(runtime["ldraw_library"]) / "LDConfig.ldr"
        runtime["ldraw_colors_sha256"] = digest(colors) if colors.is_file() else None
    if provider is not None:
        if not runtime["request"]["model"].startswith("openai/responses/"):
            raise ValueError(
                "explicit evaluation provider requires the native OpenAI Responses route"
            )
        runtime["provider"] = {
            "api_base": provider.api_base,
            "key_file": str(provider.key_file),
        }
    runtime["provider_settings_sha256"] = sha256(
        json.dumps(
            {
                name: (_provider_env(provider) or os.environ).get(name)
                for name in (
                    "OPENAI_API_BASE",
                    "OPENAI_BASE_URL",
                    "ANTHROPIC_API_BASE",
                    "AZURE_API_BASE",
                    "AWS_REGION_NAME",
                    "LITELLM_LOCAL_MODEL_COST_MAP",
                )
            }
        ).encode()
    ).hexdigest()
    bound = {
        "checkout": str(checkout),
        "commit": commit,
        "lock_sha256": digest(checkout / "uv.lock"),
        "runtime": runtime,
    }
    path = _arm_path(output, arm)
    if path.is_file() and json_data(path) != bound:
        raise ValueError(f"{arm} checkout, revision, or runtime changed since the run")
    other = _arm_path(output, "new" if arm == "old" else "old")
    if other.is_file():
        paired = json_data(other)
        if paired["commit"] == commit:
            raise ValueError("the two arms must use different commits")
        if paired["runtime"] != runtime:
            raise ValueError("the paired runtime or request settings differ")
    write_json(path, bound)
    return bound


def _provider_env(provider: Provider | None) -> dict[str, str] | None:
    if provider is None:
        return None
    # Tool subprocesses need the parts library and local executables. The
    # named key is read inside the request wrapper and never added to their environment.
    allowed = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "LDRAW_LIBRARY_PATH")
    return {name: os.environ[name] for name in allowed if name in os.environ} | {
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }


TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def usage_problem(
    metadata: dict[str, Any] | None,
    responses: list[dict[str, Any]] | None = None,
    *,
    allow_unpriced: bool = False,
) -> str | None:
    if metadata is None:
        return "native generator produced no record"
    usage = metadata.get("usage") or {}
    tokens = usage.get("total_tokens", 0)
    if not isinstance(tokens, int) or tokens <= 0:
        return "no model response with token usage was recorded"
    cost = usage.get("cost_usd")
    if allow_unpriced:
        if cost is not None:
            return "unpriced evaluation reported a dollar total"
    elif not isinstance(cost, (float, int)) or not isfinite(cost) or cost <= 0:
        return "spend budget could not be verified from model usage and pricing"
    if responses is not None:
        breakdown = [response["usage"] for response in responses]
        models = {response["model"] for response in responses}
        if not models or any(
            not isinstance(model, str) or not model for model in models
        ):
            return "responding model could not be verified"
        if len(models) != 1:
            return "the provider returned different model versions during the run"
        if any(row["total_tokens"] <= 0 for row in breakdown):
            return "a model response did not report token usage"
        if allow_unpriced:
            if any(row["cost_usd"] is not None for row in breakdown):
                return "unpriced evaluation reported a response dollar cost"
        else:
            costs = [row["cost_usd"] for row in breakdown]
            if any(
                value is None or not isfinite(value) or value <= 0 for value in costs
            ):
                return "spend budget could not be verified for every model response"
            if cost is None or not isclose(sum(costs), cost):
                return "native usage totals differ from the response log"
        if any(
            sum(row[field] for row in breakdown) != usage.get(field)
            for field in TOKEN_FIELDS
        ):
            return "native usage totals differ from the response log"
    if metadata.get("stop_reason") == "error":
        return "native driver error after a model response"
    return None


def budget_problem(
    metadata: dict[str, Any], responses: list[dict[str, Any]], settings: dict[str, Any]
) -> str | None:
    if (
        metadata.get("turns") != len(responses)
        or len(responses) > settings["max_turns"]
    ):
        return "native turn counts do not match the recorded responses or budget"
    if not 0 <= metadata.get("validations", -1) <= settings["max_validations"]:
        return "native validation count exceeds the frozen budget or is missing"
    # Each response's price is known only after its call returns.
    cap = settings["max_cost"]
    if (
        cap is not None
        and sum(row["usage"]["cost_usd"] for row in responses[:-1]) >= cap
    ):
        return "a model response was requested after reaching the frozen spend cap"
    return None


def _match_responding_model(output: Path, model: str) -> str | None:
    path = output / "private" / "responding_model.json"
    if not path.is_file():
        write_json(path, model)
    elif json_data(path) != model:
        return "the provider returned a different model version from an earlier trial"
    return None


def _attempt(
    output: Path,
    slot: Slot,
    plan: dict[str, Any],
    arm: dict[str, Any],
    provider: Provider | None = None,
) -> None:
    directory = slot.directory(output)
    attempt_path = directory / "attempt.json"
    if directory.exists():
        if not attempt_path.is_file():
            print(f"{slot.key}: interrupted attempt retained", flush=True)
        elif problem := json_data(attempt_path).get("usage_problem"):
            raise ValueError(f"{slot.key}: {problem}. See {directory / 'native.log'}")
        return
    checkout = Path(arm["checkout"])
    if checkout_commit(checkout) != arm["commit"]:
        raise ValueError(f"{slot.arm} checkout changed since it was bound")
    directory.mkdir(parents=True)
    records = directory / "records"
    command = generate_command(directory, checkout, plan, slot.task, provider)
    print(f"{slot.key}: running native generator", flush=True)
    with (directory / "native.log").open("wb") as log:
        try:
            exit_code = subprocess.run(
                command,
                cwd=checkout,
                env=_provider_env(provider),
                check=False,
                stdout=log,
                stderr=log,
                timeout=plan.get("trial_timeout"),
            ).returncode
        except subprocess.TimeoutExpired:
            exit_code = None
    if checkout_commit(checkout) != arm["commit"]:
        raise ValueError(f"{slot.arm} checkout changed during {slot.key}")
    found = sorted(records.glob("*/meta.json"))
    if len(found) > 1:
        raise ValueError(f"{slot.key}: expected exactly one native record")
    record = found[0] if found else None
    metadata = json_data(record) if record else None
    if metadata is not None:
        check_generation(slot, plan, metadata)
    response_log = directory / "responses.jsonl"
    responses = read_responses(response_log)
    problem = (
        "native trial exceeded the frozen wall-clock limit"
        if exit_code is None
        else usage_problem(
            metadata, responses, allow_unpriced=plan["settings"]["max_cost"] is None
        )
    )
    if problem is None and metadata is not None:
        problem = budget_problem(metadata, responses, plan["settings"])
        if problem is None:
            problem = _match_responding_model(output, responses[0]["model"])
    files = {}
    if record is not None:
        for name in CAPTURED_FILES:
            path = record.parent / name
            files[name] = digest(path) if path.is_file() else None
    write_json(
        attempt_path,
        {
            "slot": slot.key,
            "plan_sha256": digest(output / "private" / "plan.json"),
            "commit": arm["commit"],
            "exit_code": exit_code,
            "record_id": record.parent.name if record else None,
            "meta_sha256": digest(record) if record else None,
            "files": files,
            "usage_problem": problem,
            "responses_sha256": digest(response_log)
            if response_log.is_file()
            else None,
        },
    )
    print(f"{slot.key}: exit {exit_code}", flush=True)
    if problem:
        raise ValueError(f"{slot.key}: {problem}. See {directory / 'native.log'}")


def read_responses(log: Path) -> list[dict[str, Any]]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def run(
    output: Path, checkouts: dict[str, Path], provider: Provider | None = None
) -> None:
    plan = load_plan(output)
    arms = {
        arm: _bind_arm(output, arm, path, plan, provider)
        for arm, path in checkouts.items()
    }
    for slot in slots(plan):
        if slot.arm in arms:
            _attempt(output, slot, plan, arms[slot.arm], provider)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="freeze the tasks, rubric and model budgets"
    )
    init.add_argument("output", type=Path)
    init.add_argument("--model", required=True)
    init.add_argument("--effort", choices=EFFORTS, required=True)
    init.add_argument("--repetitions", type=int, required=True)
    init.add_argument("--max-turns", type=int, required=True)
    init.add_argument("--max-validations", type=int, required=True)
    spend = init.add_mutually_exclusive_group(required=True)
    spend.add_argument("--max-cost", type=float)
    spend.add_argument(
        "--unpriced",
        dest="max_cost",
        action="store_const",
        const=None,
        help="use the fixed non-dollar budgets and report dollar cost as unknown",
    )
    init.add_argument(
        "--trial-timeout", type=float, help="wall-clock seconds per trial"
    )
    init.add_argument("--build-timeout", type=float, default=30)
    init.add_argument("--render-timeout", type=float, default=60)
    init.add_argument("--request-timeout", type=float, default=120)
    execute = commands.add_parser("run", help="invoke the native CLI at each revision")
    execute.add_argument("output", type=Path)
    execute.add_argument("--old", type=Path)
    execute.add_argument("--new", type=Path)
    execute.add_argument("--api-base", help="bound HTTPS OpenAI Responses base URL")
    execute.add_argument(
        "--api-key-file", type=Path, help="read this bearer token in memory"
    )
    review = commands.add_parser(
        "review", help="replay artifacts and create the blind review bundle"
    )
    review.add_argument("output", type=Path)
    report = commands.add_parser(
        "report", help="aggregate paired outcomes and blinded scores"
    )
    report.add_argument("output", type=Path)
    report.add_argument("--scores", type=Path)
    args = vars(parser.parse_args())
    output = args.pop("output").resolve()
    command = args.pop("command")
    try:
        if command == "init":
            freeze_plan(output, make_plan(**args))
        elif command == "run":
            api_base = args.pop("api_base")
            key_file = args.pop("api_key_file")
            if (api_base is None) != (key_file is None):
                parser.error("--api-base and --api-key-file require one another")
            provider = (
                Provider(api_base.rstrip("/"), key_file.expanduser().resolve())
                if api_base is not None and key_file is not None
                else None
            )
            checkouts = {arm: path for arm, path in args.items() if path is not None}
            if not checkouts:
                parser.error("run needs --old, --new, or both checkouts")
            run(output, checkouts, provider)
        else:
            from brickwright.evaluation.p001_review import build_report, prepare_review

            if command == "review":
                prepare_review(output)
            else:
                result = build_report(output, args["scores"])
                write_json(output / "private" / "report.json", result)
                print(json.dumps(result, indent=2, ensure_ascii=False))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"P-001: {exc}{os.linesep}")


if __name__ == "__main__":
    main()
