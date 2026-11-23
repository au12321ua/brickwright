"""Verify OMR36 record gates and independent image reads against the Maestro archive."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from maestro import PROTOCOL, Json, file_digest, read, write

VIEWS = ("angle", "face", "end", "top", "reverse")
REQUIRED_IMAGES = {
    f"{kind}-{view}.png" for kind in ("goal", "candidate", "overlay") for view in VIEWS
}
STAMP_PATHS = (
    "task.json",
    "conditions.json",
    "features.json",
    "plan.json",
    "merge-manifest.json",
    "flow/current.json",
    "review.json",
    "out/build.ldr",
)


def observed_sessions(events: Iterable[Json]) -> Json:
    """Read exact image response bytes and native spawn receipts, not prose claims."""
    session: Json = {
        "images": Counter(),
        "fresh_dispatches": set(),
        "dispatches": set(),
        "settings": set(),
    }
    image_calls: set[str] = set()
    spawn_calls: dict[str, Json] = {}
    for event in events:
        kind, payload = event["type"], event["payload"]
        if kind == "session_meta":
            source = payload.get("source")
            subagent = source.get("subagent", {}) if isinstance(source, dict) else {}
            session.update(
                id=payload["id"],
                version=payload.get("cli_version"),
                spawn=subagent.get("thread_spawn"),
            )
        if kind == "turn_context":
            session["settings"].add((payload.get("model"), payload.get("effort")))
        if kind != "response_item":
            continue
        call_id = payload.get("call_id")
        item = payload.get("type")
        if item in {"function_call", "custom_tool_call"}:
            if "view_image" in (payload.get("name", "") + (payload.get("input") or "")):
                image_calls.add(call_id)
            if payload.get("name") == "spawn_agent":
                spawn_calls[call_id] = json.loads(payload.get("arguments") or "{}")
        if (
            item in {"function_call_output", "custom_tool_call_output"}
            and call_id in image_calls
        ):
            for image in payload.get("output") or []:
                if isinstance(image, dict) and image.get("type") == "input_image":
                    uri = image.get("image_url", "")
                    if uri.startswith("data:image/") and ";base64," in uri:
                        content = base64.b64decode(uri.split(",", 1)[1], validate=True)
                        session["images"][hashlib.sha256(content).hexdigest()] += 1
        if item == "function_call_output" and call_id in spawn_calls:
            output = payload.get("output")
            reply = (
                json.loads(output)
                if isinstance(output, str) and output.startswith("{")
                else {}
            )
            if task_name := reply.get("task_name"):
                session["dispatches"].add(task_name)
                if spawn_calls[call_id].get("fork_turns") == "none":
                    session["fresh_dispatches"].add(task_name)
    return session


def sessions(root: Path) -> dict[str, Json]:
    results: dict[str, Json] = {}
    for rollout in sorted(root.rglob("rollout-*.jsonl")):
        with rollout.open() as stream:
            evidence = observed_sessions(
                json.loads(line) for line in stream if line.strip()
            )
        evidence["rollout_sha256"] = file_digest(rollout)
        if evidence.get("id"):
            results[evidence["id"]] = evidence
    return results


def matches_original(record: Path, original: Path, meta: Json) -> tuple[bool, set[str]]:
    original_task = read(original / "task.json")
    conditions = record / "conditions.json"
    expected = [
        {"id": f"C{index:02d}", "text": text}
        for index, text in enumerate(original_task["checks"], 1)
    ]
    ids = {check["id"] for check in expected}
    if not ((record / "task.json").is_file() and conditions.is_file()):
        return False, ids
    original_sha = file_digest(original / "task.json")
    actual = read(conditions)
    covered = (
        {
            evidence.get("check_id")
            for feature in read(record / "features.json")["features"]
            for evidence in feature["evidence"]
        }
        if (record / "features.json").is_file()
        else set()
    )
    return (
        file_digest(record / "task.json") == original_sha
        and actual.get("task_sha256") == original_sha
        and actual.get("task_id") == original_task["id"]
        and actual.get("checks") == expected
        and file_digest(conditions) == meta.get("conditions_sha256")
        and covered - {None} == ids
    ), ids


def audit_review(
    path: Path, original: Path, ids: set[str], logs: dict[str, Json]
) -> Json:
    entry = read(path)
    result = {
        key: entry.get(key)
        for key in ("round_id", "status", "passed", "snapshot_id", "submitted_at")
    }
    if entry.get("status") != "accepted":
        return result
    packet = path.parent / "inputs"
    manifest_path = packet / "manifest.json"
    report = entry["report"]
    reviewer = report["reviewer"]
    parent_id, child_id = reviewer["parent_session_id"], reviewer["session_id"]
    condition_ids = [row["id"] for row in report["conditions"]]
    row = result | {
        "parent_session_id": parent_id,
        "child_session_id": child_id,
        "condition_ids": condition_ids,
    }
    deviations: list[str] = []
    unknown: list[str] = []
    if sorted(condition_ids) != sorted(ids):
        deviations.append("review_did_not_cover_original_conditions")
    if not manifest_path.is_file():
        unknown.append("frozen_review_manifest_unavailable")
    else:
        manifest = read(manifest_path)
        if (
            file_digest(manifest_path) != entry["input_sha256"]
            or manifest["snapshot_id"] != entry["snapshot_id"]
            or manifest.get("parent_session_id") != parent_id
            or manifest.get("round_id") != entry["round_id"]
        ):
            deviations.append("review_manifest_binding_mismatch")
        wanted = manifest.get("image_reads", [])
        if set(wanted) != REQUIRED_IMAGES or len(wanted) != len(REQUIRED_IMAGES):
            deviations.append("missing_fixed_views_or_overlays")
        if sorted(wanted) != sorted(reviewer.get("image_reads", [])):
            deviations.append("review_receipt_did_not_cover_images")
        if any(
            file_digest(original / f"goal-{view}.png")
            != manifest["files"].get(f"goal-{view}.png")
            for view in VIEWS
        ) or file_digest(original / "cameras.json") != manifest["files"].get(
            "cameras.json"
        ):
            deviations.append("review_used_different_goal_or_camera_bytes")
        row["expected_image_reads"] = len(wanted)
        child = logs.get(child_id)
        if child:
            required = Counter(
                manifest["files"][name] for name in wanted if name in manifest["files"]
            )
            row["exact_image_reads"] = sum((required & child["images"]).values())
            if row["exact_image_reads"] != len(wanted):
                deviations.append(
                    "frozen_images_missing_from_actual_child_tool_outputs"
                )
    child, parent = logs.get(child_id), logs.get(parent_id)
    model = PROTOCOL["model"]
    effort = PROTOCOL["effort"]
    version = PROTOCOL["codex_version"]
    if reviewer.get("dispatch") != "codex_subagent" or child_id == parent_id:
        deviations.append("review_did_not_use_a_separate_native_subagent")
    if reviewer.get("model") != model or reviewer.get("effort") != effort:
        deviations.append("review_receipt_model_or_effort_mismatch")
    if not child or not parent:
        unknown.append("native_parent_or_child_rollout_unavailable")
    else:
        spawn = child.get("spawn") or {}
        name = spawn.get("agent_path")
        row["rollout_sha256"] = child["rollout_sha256"]
        if (
            spawn.get("parent_thread_id") != parent_id
            or name not in parent["dispatches"]
        ):
            deviations.append("native_child_not_linked_to_parent_dispatch")
        elif name not in parent["fresh_dispatches"]:
            deviations.append("reviewer_dispatch_inherited_building_context")
        if any(
            log["settings"] != {(model, effort)} or log["version"] != version
            for log in (child, parent)
        ):
            deviations.append("actual_parent_or_child_native_setting_mismatch")
    return row | {"deviations": deviations, "unavailable_evidence": unknown}


def delivery_bound(record: Path, meta: Json, model: Path, reviews: list[Json]) -> bool:
    paths = list(STAMP_PATHS)
    if meta.get("prompt_image"):
        paths.append(meta["prompt_image"])
    digests = {
        name: file_digest(record / name) if (record / name).is_file() else None
        for name in paths
    }
    stamp = hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest()
    if (
        not model.is_file()
        or not reviews
        or not (record / "flow/current.json").is_file()
        or digests["out/build.ldr"] != file_digest(model)
        or stamp != meta.get("finalized_flow_sha256")
    ):
        return False
    pointer = read(record / "flow/current.json")
    structure = record / "flow/snapshots" / pointer["snapshot_id"] / "structure.json"
    if not structure.is_file() or file_digest(structure) != pointer.get(
        "structure_sha256"
    ):
        return False
    checked = read(structure)
    latest = max(
        reviews, key=lambda review: (review["submitted_at"], review["round_id"])
    )
    passed_or_exhausted = latest["passed"] or len(reviews) == PROTOCOL["review_rounds"]
    return bool(
        checked.get("passed")
        and checked.get("complete")
        and pointer.get("native_passed")
        and checked.get("mpd_sha256") == digests["out/build.ldr"]
        and latest["snapshot_id"] == pointer["snapshot_id"]
        and passed_or_exhausted
    )


def audit_trial(root: Path, frozen_tasks: Path, trial: Json) -> Json:
    result = {
        key: trial[key]
        for key in ("task_id", "trial_index", "state", "score", "delivered")
    }
    if not trial.get("attempt_path"):
        return result | {"unavailable_evidence": ["trial_archive_unavailable"]}
    attempt = root / trial["attempt_path"]
    artifacts = attempt / "artifacts/logs/artifacts"
    selected = (trial.get("score_details") or {}).get("record_id")
    records = artifacts / "records"
    candidates = sorted(path.parent for path in records.glob("*/meta.json"))
    record = (
        records / selected if selected else (candidates[-1] if candidates else None)
    )
    if record is None or not (record / "meta.json").is_file():
        return result | {"unavailable_evidence": ["workflow_record_unavailable"]}
    original = frozen_tasks / trial["task_id"] / "environment/input"
    logs = sessions(attempt / "agent/sessions")
    meta = read(record / "meta.json")
    original_unchanged, ids = matches_original(record, original, meta)
    reviews = [
        audit_review(path, original, ids, logs)
        for path in sorted(record.glob("reviews/*/review.json"))
    ]
    accepted = [review for review in reviews if review["status"] == "accepted"]
    deviations = []
    if meta.get("flow") != 1:
        deviations.append("flow_not_enabled")
    if not original_unchanged:
        deviations.append("original_task_checks_or_feature_mapping_missing_or_changed")
    budget = (meta.get("generation", {}).get("config") or {}).get(
        "flow_review_rounds", 2
    )
    if budget != PROTOCOL["review_rounds"] or len(accepted) > PROTOCOL["review_rounds"]:
        deviations.append("review_budget_mismatch")
    children = [review["child_session_id"] for review in accepted]
    if len(set(children)) != len(children):
        deviations.append("reused_child_session_consumed_multiple_review_rounds")
    if trial["delivered"] and not delivery_bound(
        record, meta, artifacts / "model.mpd", accepted
    ):
        deviations.append("platform_delivery_not_bound_to_passed_and_reviewed_flow")
    return result | {
        "record_path": str(record.relative_to(root)),
        "flow_enabled": meta.get("flow") == 1,
        "original_checks_preserved": original_unchanged,
        "configured_review_rounds": budget,
        "accepted_review_rounds": len(accepted),
        "reviews": reviews,
        "deviations": deviations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("frozen_tasks", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    index = read(args.archive / "index.json")
    trials = [
        audit_trial(args.archive, args.frozen_tasks, trial) for trial in index["trials"]
    ]
    write(
        args.output,
        {
            "run_id": index["run_id"],
            "archive_omissions": index["archive_omissions"],
            "trials": trials,
        },
    )


if __name__ == "__main__":
    main()
