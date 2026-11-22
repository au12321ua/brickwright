"""Audit final artifacts, package them for blind review, and aggregate the pairs."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from .p001 import (
    ARMS,
    CAPTURED_FILES,
    TOKEN_FIELDS,
    Slot,
    budget_problem,
    check_generation,
    checkout_commit,
    digest,
    json_data,
    load_plan,
    read_responses,
    slots,
    usage_problem,
    write_json,
)

DIMENSIONS = ("structure", "appearance", "function")
COUNT_FIELDS = (
    "pieces",
    "connector_placed_pieces",
    "direct_connector_pieces",
    "subassembly_connector_pieces",
    "live_joint_child_pieces",
)


@dataclass(frozen=True)
class Trial:
    slot: Slot
    attempt: dict[str, Any] | None = None
    record: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    response_models: tuple[str, ...] = ()
    artifact: bool = False
    issue: str | None = None


def _verify_file(path: Path, expected: str | None) -> None:
    actual = digest(path) if path.is_file() else None
    if actual != expected:
        raise ValueError(f"{path} changed since the evaluation captured it")


def _trials(output: Path, plan: dict[str, Any]) -> list[Trial]:
    private = output / "private"
    plan_sha = digest(private / "plan.json")
    arms = {
        arm: json_data(private / f"{arm}.json")
        for arm in ARMS
        if (private / f"{arm}.json").is_file()
    }
    if len(arms) == 2 and (
        arms["old"]["commit"] == arms["new"]["commit"]
        or arms["old"]["runtime"] != arms["new"]["runtime"]
    ):
        raise ValueError("the arms must bind distinct revisions and matching settings")
    result = []
    record_ids = set()
    for slot in slots(plan):
        directory = slot.directory(output)
        attempt_path = directory / "attempt.json"
        if not attempt_path.is_file():
            result.append(Trial(slot, issue="unstarted or interrupted trial"))
            continue
        attempt = json_data(attempt_path)
        if (
            attempt["slot"] != slot.key
            or attempt["plan_sha256"] != plan_sha
            or attempt["commit"] != arms.get(slot.arm, {}).get("commit")
        ):
            raise ValueError(
                f"{slot.key}: attempt does not match its trial, revision or plan"
            )
        response_log = directory / "responses.jsonl"
        _verify_file(response_log, attempt["responses_sha256"])
        record_id = attempt["record_id"]
        if record_id is None:
            result.append(
                Trial(slot, attempt, issue="native generator produced no record")
            )
            continue
        if not re.fullmatch(r"[a-z0-9-]+", record_id) or record_id in record_ids:
            raise ValueError(f"{slot.key}: invalid or duplicated record id")
        record_ids.add(record_id)
        record = directory / "records" / record_id
        _verify_file(record / "meta.json", attempt["meta_sha256"])
        metadata = json_data(record / "meta.json")
        if metadata["id"] != record_id:
            raise ValueError(f"{slot.key}: native record id does not match its attempt")
        check_generation(slot, plan, metadata)
        for name in CAPTURED_FILES:
            _verify_file(record / name, attempt["files"][name])
        responses = read_responses(response_log)
        models = tuple(
            sorted({row["model"] for row in responses if isinstance(row["model"], str)})
        )
        issue = attempt["usage_problem"] or usage_problem(metadata, responses)
        if issue is None:
            issue = budget_problem(metadata, responses, plan["settings"])
        result.append(
            Trial(
                slot,
                attempt,
                record,
                metadata,
                models,
                _has_artifact(attempt, metadata, record),
                issue,
            )
        )
    return result


def _has_artifact(
    attempt: dict[str, Any], metadata: dict[str, Any], record: Path
) -> bool:
    files = attempt["files"]
    source = files["build.py"]
    if (
        metadata.get("finalized") is not True
        or metadata.get("status") != "valid"
        or not source
        or source != metadata.get("exported_source_sha256")
        or source != metadata.get("validated_source_sha256")
        or not files["out/build.ldr"]
        or not files["out/report.json"]
    ):
        return False
    validation = json_data(record / "out" / "report.json")
    return (
        validation.get("ok") is True
        and validation.get("source_sha256") == source
        and (validation.get("brick_count") or 0) > 0
    )


def _measure(
    output: Path, trial: Trial, timeout: float, destination: Path
) -> dict[str, Any]:
    assert trial.record is not None and trial.attempt is not None
    private = output / "private"
    arm = json_data(private / f"{trial.slot.arm}.json")
    checkout = Path(arm["checkout"])
    if (
        checkout_commit(checkout) != arm["commit"]
        or digest(checkout / "uv.lock") != arm["lock_sha256"]
    ):
        raise ValueError(
            f"restore the captured revision before measuring {trial.slot.key}"
        )
    command = [
        str(checkout / ".venv" / "bin" / "python"),
        "-I",
        str(Path(__file__).with_name("p001_measure.py").resolve()),
        str(trial.record),
        str(destination),
    ]
    log_path = trial.slot.directory(output) / "measurement.log"
    with log_path.open("wb") as log:
        try:
            subprocess.run(
                command,
                cwd=checkout,
                timeout=timeout,
                check=True,
                stdout=log,
                stderr=log,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            raise ValueError(
                f"could not replay {trial.slot.key}. See {log_path}"
            ) from exc
    measured = json_data(destination / "measurement.json")
    if (
        measured["source_sha256"] != trial.attempt["files"]["build.py"]
        or measured["artifact_sha256"] != trial.attempt["files"]["out/build.ldr"]
    ):
        raise ValueError(
            f"{trial.slot.key}: measurement was made from different artifacts"
        )
    return measured


def prepare_review(output: Path) -> None:
    review = output / "review"
    if review.exists():
        raise FileExistsError(f"review bundle already exists: {review}")
    plan = load_plan(output)
    trials = _trials(output, plan)
    if any(trial.attempt is None for trial in trials):
        raise ValueError("unstarted or interrupted trials remain in the frozen plan")
    private = output / "private"
    items: list[dict[str, Any]] = []
    mapping = {}
    with TemporaryDirectory(prefix=".p001-", dir=private) as temp:
        stage = Path(temp)
        bundle = stage / "review"
        bundle.mkdir()
        for trial in trials:
            review_id = uuid4().hex
            item = {
                "id": review_id,
                "prompt": trial.slot.task["prompt"],
                "requirements": trial.slot.task["requirements"],
                "artifact": None,
            }
            entry = {"slot": trial.slot.key, "files": {}, "measurement": None}
            if trial.artifact:
                assert trial.record is not None and trial.attempt is not None
                destination = stage / review_id
                measured = _measure(
                    output, trial, plan["settings"]["render_timeout"], destination
                )
                assets = destination / "assets"
                if trial.attempt["files"]["out/build.png"]:
                    shutil.copyfile(
                        trial.record / "out" / "build.png", assets / "preview.png"
                    )
                    item["preview"] = "preview.png"
                item.update(artifact="model.ldr", evidence=measured["evidence"])
                entry.update(
                    measurement={name: measured[name] for name in COUNT_FIELDS},
                    files={path.name: digest(path) for path in assets.iterdir()},
                )
                assets.rename(bundle / review_id)
            items.append(item)
            mapping[review_id] = entry
        items.sort(key=lambda item: item["id"])
        write_json(bundle / "review.json", {"rubric": plan["rubric"], "items": items})
        review_sha = digest(bundle / "review.json")
        write_json(
            bundle / "scores.json",
            {
                "review_sha256": review_sha,
                "scores": {item["id"]: None for item in items if item["artifact"]},
            },
        )
        write_json(
            private / "review_key.json",
            {
                "plan_sha256": digest(private / "plan.json"),
                "review_sha256": review_sha,
                "items": mapping,
            },
        )
        bundle.rename(review)


def _validate_score(score: dict[str, Any]) -> None:
    if not isinstance(score, dict) or any(
        type(score.get(name)) is not int or score[name] not in (0, 1, 2)
        for name in DIMENSIONS
    ):
        raise ValueError(
            "every reviewed artifact needs three integer scores between 0 and 2"
        )
    if type(score.get("complete")) is not bool:
        raise ValueError("each score needs a Boolean complete decision")
    if score["complete"] and (score["structure"] != 2 or score["function"] != 2):
        raise ValueError("completion requires full structure and functionality scores")
    if not isinstance(score.get("notes"), str) or not score["notes"].strip():
        raise ValueError("each score needs a short evidence note")


def _reviews(output: Path, score_path: Path | None) -> dict[str, dict[str, Any]]:
    private = output / "private"
    review_key = private / "review_key.json"
    if not review_key.is_file() and score_path is None:
        return {}
    key = json_data(review_key)
    if key["plan_sha256"] != digest(private / "plan.json"):
        raise ValueError("review bundle belongs to a different plan")
    _verify_file(output / "review" / "review.json", key["review_sha256"])
    scores = {}
    if score_path is not None:
        data = json_data(score_path)
        if data["review_sha256"] != key["review_sha256"]:
            raise ValueError("scores belong to a different review bundle")
        scores = data["scores"]
    allowed = {token for token, entry in key["items"].items() if entry["measurement"]}
    if set(scores) - allowed:
        raise ValueError(
            "scored an unknown review id or a trial without a final artifact"
        )
    result = {}
    for token, entry in key["items"].items():
        for asset, recorded in entry["files"].items():
            _verify_file(output / "review" / token / asset, recorded)
        score = scores.get(token)
        if score is not None:
            _validate_score(score)
        result[entry["slot"]] = {**entry, "review_id": token, "score": score}
    return result


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        name: sum((row["measurement"] or {}).get(name, 0) for row in rows)
        for name in COUNT_FIELDS
    }
    scores = [row["score"] for row in rows if row["score"] is not None]
    quality = sum(score[name] for score in scores for name in DIMENSIONS)
    completed = sum(score["complete"] for score in scores)
    usages = [row["usage"] or {} for row in rows]
    costs = [usage["cost_usd"] for usage in usages if (usage.get("cost_usd") or 0) > 0]
    return {
        "planned_runs": len(rows),
        "attempted_runs": sum(row["attempted"] for row in rows),
        "final_artifacts": sum(row["artifact"] for row in rows),
        "scored_artifacts": len(scores),
        "completed_tasks": completed,
        "completion_rate": completed / len(rows),
        "quality_points": quality,
        "quality_mean": quality / len(scores) if scores else None,
        **counts,
        "connector_share": counts["connector_placed_pieces"] / counts["pieces"]
        if counts["pieces"]
        else None,
        **{name: sum(usage.get(name, 0) for usage in usages) for name in TOKEN_FIELDS},
        "known_cost_usd": sum(usage.get("known_cost_usd", 0) for usage in usages),
        "cost_usd": sum(costs) if len(costs) == len(rows) else None,
        "budget_unverified_runs": sum(row["issue"] is not None for row in rows),
        "over_budget_runs": sum(row["over_budget"] for row in rows),
    }


def _compare(
    old: dict[str, Any],
    new: dict[str, Any],
    numerator: str,
    denominator: str,
    strict: bool,
) -> bool | None:
    if not old[denominator] or not new[denominator]:
        return None
    before = Fraction(old[numerator], old[denominator])
    after = Fraction(new[numerator], new[denominator])
    return after > before if strict else after >= before


def build_report(output: Path, score_path: Path | None) -> dict[str, Any]:
    plan = load_plan(output)
    reviews = _reviews(output, score_path)
    pairs = {}
    rows = {arm: [] for arm in ARMS}
    for trial in _trials(output, plan):
        slot, meta = trial.slot, trial.metadata
        review = reviews.get(slot.key, {})
        row = {
            "attempted": trial.attempt is not None,
            "artifact": trial.artifact,
            "issue": trial.issue,
            "record_id": meta.get("id"),
            "exit_code": (trial.attempt or {}).get("exit_code"),
            "stop_reason": meta.get("stop_reason"),
            "turns": meta.get("turns"),
            "validations": meta.get("validations"),
            "over_budget": meta.get("over_budget", False),
            "usage": meta.get("usage"),
            "responding_models": list(trial.response_models),
            "review_id": review.get("review_id"),
            "measurement": review.get("measurement"),
            "score": review.get("score"),
        }
        key = (slot.task["id"], slot.repetition)
        pair = pairs.setdefault(key, {"task": key[0], "repetition": key[1]})
        pair[slot.arm] = row
        rows[slot.arm].append(row)
    summary = {arm: _summary(rows[arm]) for arm in ARMS}
    old, new = summary["old"], summary["new"]
    checks = {
        "connector_share_increased": _compare(
            old, new, "connector_placed_pieces", "pieces", strict=True
        ),
        "quality_maintained": _compare(
            old, new, "quality_points", "scored_artifacts", strict=False
        ),
        "completion_maintained": _compare(
            old, new, "completed_tasks", "planned_runs", strict=False
        ),
    }
    all_rows = rows["old"] + rows["new"]
    model_versions = sorted(
        {model for row in all_rows for model in row["responding_models"]}
    )
    pending = []
    if any(row["issue"] for row in all_rows):
        pending.append(
            "some trials lack a comparable native run or verified model usage"
        )
    if len(model_versions) != 1:
        pending.append("a common responding model version could not be established")
    if any(row["artifact"] and row["measurement"] is None for row in all_rows):
        pending.append("final artifacts still need placement measurements")
    if any(row["artifact"] and row["score"] is None for row in all_rows):
        pending.append("final artifacts still need blinded scores")
    if None in checks.values() and False not in checks.values():
        pending.append("one or more comparisons have no denominator")
    return {
        "status": "pending"
        if pending
        else "passed"
        if all(checks.values())
        else "failed",
        "pending_reasons": pending,
        "settings": plan["settings"],
        "responding_models": model_versions,
        "arms": summary,
        "checks": checks,
        "pairs": list(pairs.values()),
    }
