"""Stage, submit and archive the frozen OMR36 runs with Maestro SDK 0.7.2."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import time
import zipfile
from datetime import datetime
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
ARMS = ("baseline", "candidate", "bus-modular")
Json = dict[str, Any]


def read(path: Path) -> Json:
    return json.loads(path.read_bytes())


def write(path: Path, value: Json) -> None:
    interim = path.with_suffix(".tmp")
    interim.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    interim.replace(path)


PROTOCOL = read(HERE / "protocol.json")


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy(source: Path, target: Path) -> None:
    shutil.copyfile(source, target)
    target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)


def build(repo: Path, revision: str, output: Path) -> str:
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{revision}^{{commit}}"],
        text=True,
    ).strip()
    old_lock = subprocess.check_output(
        ["git", "-C", str(repo), "show", f"{PROTOCOL['baseline_revision']}:uv.lock"]
    )
    output.mkdir(parents=True)
    with TemporaryDirectory() as temporary:
        source = Path(temporary)
        tar = source / "source.tar"
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "archive",
                "--output",
                str(tar),
                commit,
                "pyproject.toml",
                "uv.lock",
                "packages",
            ],
            check=True,
        )
        with tarfile.open(tar) as archive:
            archive.extractall(source, filter="data")
        lock = source / "uv.lock"
        if lock.read_bytes() != old_lock:
            raise ValueError(
                "candidate dependency lock differs from the frozen baseline"
            )
        subprocess.run(
            [
                "uv",
                "build",
                "--offline",
                "--all-packages",
                "--wheel",
                "--out-dir",
                str(output / "wheels"),
            ],
            cwd=source,
            check=True,
        )
        wheels = sorted((output / "wheels").glob("*.whl"))
        if len(wheels) != 2:
            raise RuntimeError("expected one BrickLayer and one BrickWright wheel")
        (output / "wheels/.gitignore").unlink(missing_ok=True)
        copy(
            source / "packages/brickwright/EXTERNAL_AGENT_INSTRUCTIONS.md",
            output / "BRICKWRIGHT.md",
        )
        write(
            output / "binding.json",
            {
                "revision": commit,
                "uv_lock_sha256": file_digest(lock),
                "wheel_sha256": {wheel.name: file_digest(wheel) for wheel in wheels},
            },
        )
    copy(TEMPLATES / "candidate.md", output / "flow.md")
    copy(TEMPLATES / "attachment-bootstrap.sh", output / "bootstrap.sh")
    write(
        output / "manifest.json",
        {
            "schema_version": 1,
            "name": "brickwright-p001-sdk",
            "description": f"OMR36 BrickWright flow, CLI and BrickLayer SDK from {commit}.",
            "effects": ["agent_tools"],
            "compatible_harnesses": ["codex"],
            "provides": {"tools": ["brickwright"]},
        },
    )
    return commit


def stage_bus(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    copy(TEMPLATES / "bus-modular.md", directory / "bus-modular.md")
    script = directory / "bootstrap.sh"
    script.write_text(
        '#!/bin/sh\nset -eu\ncat "$MAESTRO_ATTACHMENT_DIR/bus-modular.md" >> /app/AGENTS.md\n'
    )
    script.chmod(0o755)
    write(
        directory / "manifest.json",
        {
            "schema_version": 1,
            "name": "brickwright-omr36-bus-modular",
            "description": "Task mode for the R-10258 modular arm.",
            "effects": ["agent_tools"],
            "compatible_harnesses": ["codex"],
        },
    )


def stage_control(frozen_task: Path, prior: Path, output: Path) -> None:
    environment = output / "review-dispatch/environment"
    shutil.copytree(frozen_task / "environment", environment)
    harness = output / "review-dispatch"
    shutil.copyfile(frozen_task / "task.toml", harness / "task.toml")
    config = harness / "task.toml"
    config.write_text(
        config.read_text()
        .replace("timeout_sec = 3600", "timeout_sec = 600")
        .replace("timeout_sec = 300", "timeout_sec = 120")
    )
    copy(TEMPLATES / "control.md", harness / "instruction.md")
    tests = harness / "tests"
    tests.mkdir()
    copy(TEMPLATES / "control-test.sh", tests / "test.sh")
    for view in ("end", "face"):
        copy(prior / f"submission-{view}.png", environment / f"candidate-{view}.png")
    with (environment / "Dockerfile").open("a") as stream:
        stream.write("COPY candidate-end.png candidate-face.png /app/control/\n")


def comparison_request(
    baseline: Json,
    arm: str,
    product: str | None = None,
    bus_task: str | None = None,
    bus_attachment: str | None = None,
) -> Json:
    if arm == "baseline":
        return baseline.copy()
    if arm not in ARMS or not product:
        raise ValueError("candidate product attachment is required")
    request = dict(baseline, attachments=[product, *baseline["attachments"][1:]])
    if arm.startswith("bus-"):
        if not bus_task:
            raise ValueError("a frozen bus task ID is required")
        request["task_selection"] = {"mode": "explicit", "task_ids": [bus_task]}
    if arm == "bus-modular":
        if not bus_attachment:
            raise ValueError("a modular task attachment is required")
        request["attachments"].append(bus_attachment)
    return request


def verify_resolution(resolved: Json, wanted_tasks: set[str]) -> None:
    config = resolved["effective_config"]
    model = config["model"]
    harness = config["harness"]
    expected = PROTOCOL
    if (
        model["config_id"] != expected["model_config"]
        or model["revision"] != expected["model_revision"]
        or model["model_id"] != expected["model"]
        or model["params"]["reasoning_effort"] != expected["effort"]
        or harness["version_id"] != expected["harness"]
        or harness["upstream_version"] != expected["codex_version"]
        or harness["content_digest"] != expected["harness_digest"]
        or config["dataset"]["resolved_digest"] != expected["dataset_digest"]
        or config["repetitions"] != expected["repetitions"]
        or config["concurrency"] != expected["concurrency"]
        or config["bootstrap_timeout_sec"] != expected["bootstrap_timeout_sec"]
        or config["run_deadline_hours"] != expected["run_deadline_hours"]
        or resolved["planned_trials"] != len(wanted_tasks) * expected["repetitions"]
        or resolved["excluded_tasks"]
    ):
        raise ValueError("Maestro resolution disagrees with the frozen protocol")
    tasks = {task["task_key"]: task for task in config["tasks"]}
    if tasks.keys() != wanted_tasks:
        raise ValueError(
            f"task selection was {list(tasks)} and must be {sorted(wanted_tasks)}"
        )
    for key, task in tasks.items():
        environments = task["environments"]
        if (
            task["agent_timeout_sec"] != expected["agent_timeouts"][key]
            or task["verifier_timeout_sec"] != expected["verifier_timeout_sec"]
            or {env["role"] for env in environments} != {"environment/main", "verifier"}
            or any(
                env["cpus"] != expected["cpus_per_environment"]
                or env["memory_mb"] != expected["memory_mb_per_environment"]
                for env in environments
            )
        ):
            raise ValueError(f"frozen resources or time budget drifted for {key}")


def verify_calibration(report: Json) -> None:
    limits = PROTOCOL["calibration"]
    thresholds = {
        "archive_png_delta": limits["png_absolute_difference_max"],
        "reference_minimum": limits["reference_min"],
        "baseline_rerender_delta": limits["baseline_absolute_difference_max"],
    }
    if (
        report["passed"] is not True
        or report["failures"]
        or report["thresholds"] != thresholds
        or set(report["tasks"]) != set(PROTOCOL["agent_timeouts"])
    ):
        raise ValueError(
            "candidate renderer calibration did not meet the frozen protocol"
        )
    views = {"angle", "face", "end", "top", "reverse"}
    for task, evidence in report["tasks"].items():
        if set(evidence["views"]) != views:
            raise ValueError(f"incomplete calibration for {task}")
        for view, scores in evidence["views"].items():
            for key in ("silhouette_iou", "rgb_agreement_over_union"):
                exact = scores["archived"][key]
                if not (
                    abs(scores["recomputed"][key] - exact)
                    <= thresholds["archive_png_delta"]
                    and scores["reference"][key] >= thresholds["reference_minimum"]
                    and abs(scores["baseline"][key] - exact)
                    <= thresholds["baseline_rerender_delta"]
                ):
                    raise ValueError(
                        f"frozen calibration threshold missed for {task}/{view}/{key}"
                    )


def publish_product(
    client: Any, state: Json, directory: Path, calibration: Path
) -> str:
    verify_calibration(read(calibration))
    binding = read(directory / "binding.json")
    if "product" in state:
        if binding != state["product"]["binding"]:
            raise ValueError(
                "candidate directory does not match the already frozen product"
            )
    else:
        revision = binding["revision"]
        attachment = client.attachments.publish(
            directory, label=f"{revision[:12]}-omr36-flow", parent=PROTOCOL["baseline"]
        )
        state["product"] = {
            "id": attachment.id,
            "digest": attachment.content_digest,
            "revision": revision,
            "binding": binding,
            "calibration_sha256": file_digest(calibration),
        }
    return str(state["product"]["id"])


def oracle_smoke(client: Any, work: Path, directory: Path, calibration: Path) -> Json:
    state_path = work / "state.json"
    state = read(state_path)
    product = publish_product(client, state, directory, calibration)
    write(state_path, state)
    if "oracle" in state:
        return state["oracle"]
    tasks = client.catalog.tasks(PROTOCOL["dataset"])
    task = next(task.id for task in tasks if task.task_key == "R-21011")
    request = {
        "dataset_version_id": PROTOCOL["dataset"],
        "execution_mode": "oracle",
        "attachments": [product, state["common"]["id"]],
        "task_selection": {"mode": "explicit", "task_ids": [task]},
        "repetitions": 1,
        "concurrency": 1,
    }
    resolved = client.configs.resolve(request).raise_for_blockers()
    if resolved.planned_trials != 1 or resolved.excluded_tasks:
        raise RuntimeError(
            "expected only the frozen Brandenburg Gate Oracle smoke trial"
        )
    key = f"omr36-candidate-oracle-smoke-{state['product']['revision'][:12]}"
    accepted = client.runs.submit(
        name="OMR36 · candidate installation and verifier Oracle control",
        resolution_id=resolved.resolution_id,
        client_request_id=key,
        idempotency_key=key,
    )
    smoke = {
        "run": accepted.run_id,
        "operation": accepted.operation_id,
        "config_digest": resolved.config_digest,
        "attachment": product,
    }
    state["oracle"] = smoke
    write(state_path, state)
    return smoke


def submit(
    client: Any, work: Path, product_dir: Path, arm: str, calibration: Path
) -> Json:
    state_path = work / "state.json"
    state = read(state_path) if state_path.exists() else {}
    if arm in state:
        return state[arm]
    if not state.get("common"):
        package = client.attachments.publish(
            HERE / "common", label="dispatch-authorized-v2"
        )
        state["common"] = {"id": package.id, "digest": package.content_digest}
    if not state.get("baseline_request"):
        request = client.runs.request(PROTOCOL["historical_run"]).model_dump(
            mode="json", exclude_none=True
        )
        state["baseline_request"] = dict(
            request,
            attachments=[PROTOCOL["baseline"], state["common"]["id"]],
            repetitions=PROTOCOL["repetitions"],
        )
    product = (
        None
        if arm == "baseline"
        else publish_product(client, state, product_dir, calibration)
    )
    bus_task = None
    supplement = None
    if arm.startswith("bus-"):
        tasks = client.catalog.tasks(PROTOCOL["dataset"])
        bus_task = next(task.id for task in tasks if task.task_key == "R-10258")
    if arm == "bus-modular":
        if "bus_supplement" not in state:
            stage_bus(work / "modular")
            attachment = client.attachments.publish(
                work / "modular", label="modular-v1"
            )
            state["bus_supplement"] = {
                "id": attachment.id,
                "digest": attachment.content_digest,
            }
        supplement = state["bus_supplement"]["id"]
    request = comparison_request(
        state["baseline_request"], arm, product, bus_task, supplement
    )
    resolved = client.configs.resolve(request).raise_for_blockers()
    evidence = resolved.model_dump(mode="json")
    wanted = {"R-10258"} if bus_task else set(PROTOCOL["agent_timeouts"])
    verify_resolution(evidence, wanted)
    state[f"{arm}_resolution"] = evidence
    write(state_path, state)
    key = f"omr36-p004-p006-formal-{arm}-r2-v1"
    existing = client.runs.find(key)
    if len(existing) > 1:
        raise RuntimeError(f"multiple submissions found for {key}")
    if existing:
        accepted = client.runs.get(existing[0].id)
        if accepted.snapshot.config_digest != resolved.config_digest:
            raise RuntimeError(
                f"existing submission {accepted.id} has a different config"
            )
        run_id, operation = accepted.id, accepted.operation_id
    else:
        accepted = client.runs.submit(
            name=f"OMR36 · P-004–006 · {arm} ×2",
            resolution_id=resolved.resolution_id,
            source_run_id=PROTOCOL["historical_run"]
            if arm == "baseline"
            else state["baseline"]["run"],
            client_request_id=key,
            idempotency_key=key,
        )
        run_id, operation = accepted.run_id, accepted.operation_id
    result = {
        "run": run_id,
        "operation": operation,
        "config_digest": resolved.config_digest,
    }
    state[arm] = result
    write(state_path, state)
    return result


def session_usage(events: list[Json]) -> Json:
    meta: Json = {}
    tokens = None
    contexts: list[Json] = []
    image_calls: set[str] = set()
    delivered_images = 0
    for event in events:
        kind, payload = event.get("type"), event.get("payload") or {}
        if kind == "session_meta":
            meta = payload
        elif kind == "turn_context":
            contexts.append(payload)
        elif kind == "event_msg" and payload.get("type") == "token_count":
            current = (payload.get("info") or {}).get("total_token_usage")
            if current:
                tokens = current
        elif kind == "response_item" and payload.get("type") in {
            "function_call",
            "custom_tool_call",
        }:
            if "view_image" in (payload.get("name", "") + (payload.get("input") or "")):
                image_calls.add(payload["call_id"])
        elif kind == "response_item" and payload.get("call_id") in image_calls:
            output = payload.get("output")
            if isinstance(output, list):
                delivered_images += sum(
                    item.get("type") == "input_image" for item in output
                )
    return {
        "session_id": meta.get("id"),
        "source": meta.get("source"),
        "codex_version": meta.get("cli_version"),
        "model": sorted({ctx["model"] for ctx in contexts if ctx.get("model")}),
        "effort": sorted({ctx["effort"] for ctx in contexts if ctx.get("effort")}),
        "tokens": tokens,
        "image_tool_call_ids": sorted(image_calls),
        "image_responses": delivered_images,
    }


def session_log(path: Path) -> Json:
    evidence = session_usage(
        [json.loads(line) for line in path.read_bytes().splitlines() if line]
    )
    return evidence | {"rollout": path.name, "rollout_sha256": file_digest(path)}


def seconds(start: str | None, end: str | None) -> float | None:
    if start and end:
        return (
            datetime.fromisoformat(end) - datetime.fromisoformat(start)
        ).total_seconds()
    return None


def record_evidence(record: Path, attempt: Path) -> Json:
    native_checks = [
        read(path) for path in sorted(record.glob("snapshots/*/report.json"))
    ]
    rounds = [read(path) for path in sorted(record.glob("reviews/*/review.json"))]
    result: Json = {
        "path": str(record.relative_to(attempt)),
        "finalized": read(record / "meta.json").get("finalized"),
        "structure_checks": len(native_checks),
        "native_failed_checks": sum(
            check.get("ok") is False for check in native_checks
        ),
        "review_attempts": [
            {
                key: review.get(key)
                for key in (
                    "round_id",
                    "status",
                    "passed",
                    "remaining_items",
                    "snapshot_id",
                )
            }
            | {"reviewer": review.get("report", {}).get("reviewer")}
            for review in rounds
        ],
        "module_sessions": [
            read(path) for path in sorted(record.glob("modules/*/sessions/*.json"))
        ],
        "module_manifests": [
            {"sha256": path.stem, "selections": read(path)["selections"]}
            for path in sorted(record.glob("merge-manifests/*.json"))
        ],
    }
    pointer = record / "flow/current.json"
    if pointer.is_file():
        snapshot = read(pointer)["snapshot_id"]
        path = record / "flow/snapshots" / snapshot / "structure.json"
    else:
        path = None
    if path and path.is_file():
        structure = read(path)
        result["structure"] = {
            "sha256": file_digest(path),
            "mpd_sha256": structure.get("mpd_sha256"),
            "complete": structure.get("complete"),
            "passed": structure.get("passed"),
            "limitations": structure.get("limitations"),
            "classifications": structure.get("classifications"),
            **{
                key: {
                    "component_sizes": [
                        item["part_count"] for item in col["components"]
                    ],
                    "collisions": len(col["collisions"]),
                    "floating": len(col["floating"]),
                    "contact_only_parts": len(col["contact_only_parts"]),
                    "modules": col["modules"],
                }
                for key in ("native", "standard")
                if (col := structure.get(key))
            },
        }
    final_review = record / "review.json"
    if final_review.is_file() and result["finalized"]:
        review = read(final_review)
        result["review_delivery"] = (
            "passed" if review.get("passed") else "delivered_with_open_items"
        )
    return result


def trial_row(archive: Path, files: list[Json], trial: Json) -> Json:
    result = trial.get("result") or {}
    attempt_id = result.get("attempt_id")
    if attempt_id is None and trial.get("attempts"):
        attempt_id = max(trial["attempts"], key=lambda item: item["attempt_index"])[
            "id"
        ]
    layout = next(
        (
            item["path"]
            for item in files
            if item["trial_id"] == trial["id"] and item["attempt_id"] == attempt_id
        ),
        None,
    )
    row: Json = {
        "trial_id": trial["id"],
        "task_id": trial["task_key"],
        "trial_index": trial["repetition_index"],
        "state": trial["state"],
        "reason_code": trial.get("reason_code"),
        "attempt_id": attempt_id,
        "score": result.get("score"),
        "delivered": None,
    }
    if not layout:
        return row
    relative = Path(*Path(layout).parts[:4])
    attempt = archive / relative
    report = attempt / "verifier/score-details.json"
    product = attempt / "artifacts/logs/artifacts"
    model = product / "model.mpd"
    row["attempt_path"] = str(relative)
    if report.is_file():
        score = read(report)
        row["delivered"] = (
            score.get("native_finalization") == "pass"
            and score.get("quality") is not None
        )
        row["score_details"] = score
        row["score_details_sha256"] = file_digest(report)
    if model.is_file():
        row["model_sha256"] = file_digest(model)
    attempt_record = attempt / "result.json"
    if attempt_record.is_file():
        result_data = read(attempt_record)
        execution = result_data.get("agent_execution") or {}
        row["agent_wall_sec"] = seconds(
            execution.get("started_at"), execution.get("finished_at")
        )
        row["attempt_wall_sec"] = seconds(
            result_data.get("started_at"), result_data.get("finished_at")
        )
        row["harbor_usage"] = result_data.get("agent_result")
    row["sessions"] = [
        session_log(path)
        for path in sorted((attempt / "agent/sessions").rglob("rollout-*.jsonl"))
    ]
    row["record_artifacts"] = [
        str(path.relative_to(attempt))
        for path in sorted((product / "records").rglob("*.json"))
    ]
    row["records"] = [
        record_evidence(meta.parent, attempt)
        for meta in sorted((product / "records").glob("*/meta.json"))
    ]
    return row


def extract_run(archive_path: Path, destination: Path) -> None:
    with (
        TemporaryDirectory(dir=destination) as temporary,
        zipfile.ZipFile(archive_path) as archive,
    ):
        staged = Path(temporary)
        archive.extractall(staged)
        manifests = [
            path
            for path in (staged / "manifest.json", *staged.glob("*/manifest.json"))
            if path.is_file()
        ]
        if len(manifests) != 1:
            raise RuntimeError("expected one top-level run archive manifest")
        manifest = manifests[0]
        for path in manifest.parent.iterdir():
            if path != manifest:
                path.rename(destination / path.name)
        manifest.rename(destination / "manifest.json")


def collect(client: Any, work: Path, arm: str, run_id: str) -> Json:
    root = work / "runs" / arm
    run = client.runs.get(run_id)
    if run.state not in {
        "finished",
        "cancelled",
        "interrupted",
    } or run.artifact_state in {"pending", "saving"}:
        return {
            "run_id": run_id,
            "state": run.state,
            "counts": run.counts.model_dump(mode="json"),
        }
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "manifest.json").is_file():
        contents = root / "run.zip"
        if not contents.is_file():
            client.runs.download_archive(run_id, contents)
        extract_run(contents, root)
    manifest = read(root / "manifest.json")
    trials = client.runs.trials(run_id)
    result = {
        "arm": arm,
        "run_id": run_id,
        "state": run.state,
        "config_digest": run.snapshot.config_digest,
        "summary": client.runs.summary(run_id).model_dump(mode="json"),
        "archive_omissions": manifest["missing"],
        "archive_warnings": manifest["warnings"],
        "trials": [
            trial_row(root, manifest["files"], trial.model_dump(mode="json"))
            for trial in trials
        ],
    }
    write(root / "index.json", result)
    return {
        "run_id": run_id,
        "state": run.state,
        "index": str(root / "index.json"),
        "counts": run.counts.model_dump(mode="json"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("build")
    publish.add_argument("repo", type=Path)
    publish.add_argument("revision")
    publish.add_argument("output", type=Path)
    control = commands.add_parser("stage-control")
    control.add_argument("frozen_task", type=Path)
    control.add_argument("prior_verifier", type=Path)
    control.add_argument("output", type=Path)
    start = commands.add_parser("submit")
    start.add_argument("work", type=Path)
    start.add_argument("product", type=Path)
    start.add_argument("calibration", type=Path)
    start.add_argument("arms", nargs="+", choices=ARMS)
    smoke = commands.add_parser("oracle-smoke")
    smoke.add_argument("work", type=Path)
    smoke.add_argument("product", type=Path)
    smoke.add_argument("calibration", type=Path)
    harvest = commands.add_parser("collect")
    harvest.add_argument("work", type=Path)
    harvest.add_argument("arms", nargs="+")
    harvest.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        print(build(args.repo, args.revision, args.output.resolve()))
        return
    if args.command == "stage-control":
        stage_control(args.frozen_task, args.prior_verifier, args.output)
        return
    client = import_module("maestro_sdk").MaestroClient.from_env()
    client.require("runs", "execution")
    args.work.mkdir(parents=True, exist_ok=True)
    if args.command == "oracle-smoke":
        print(
            oracle_smoke(client, args.work, args.product, args.calibration), flush=True
        )
    elif args.command == "submit":
        for arm in args.arms:
            print(
                arm,
                submit(client, args.work, args.product, arm, args.calibration),
                flush=True,
            )
    else:
        state = read(args.work / "state.json")
        unfinished = set(args.arms)
        last_counts: dict[str, Json] = {}
        while unfinished:
            for arm in sorted(unfinished):
                entry = (
                    read(args.work / "control.json") if arm == "control" else state[arm]
                )
                result = collect(client, args.work, arm, entry["run"])
                if result != last_counts.get(arm):
                    print(arm, result, flush=True)
                    last_counts[arm] = result
                if "index" in result:
                    unfinished.remove(arm)
            if not args.watch or not unfinished:
                break
            time.sleep(60)


if __name__ == "__main__":
    main()
