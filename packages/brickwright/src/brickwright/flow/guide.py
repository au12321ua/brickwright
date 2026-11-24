"""Choose the one step a host can take next on a record."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..actions import RECORD, Action, edit, provide, run

if TYPE_CHECKING:
    from ..record import Record

SUBMISSION = "SUBMISSION_MPD"
_FIRST = (
    "flow.conditions",
    "flow.version",
    "flow.features",
    "flow.plan",
)


def validate_action() -> Action:
    return run("Validate the current build.py.", "external", "validate", RECORD)


def repair_action(record: Record, summary: str) -> Action:
    return edit(summary, record.build_path.resolve(), validate_action())


def delivery_action(record: Record) -> Action:
    return provide(
        f"Copy {record.ldr_path.resolve()} byte for byte to the submission path, "
        "then verify the copied file.",
        run(
            "Verify the submitted file against the finalized snapshot.",
            "external",
            "verify-delivery",
            RECORD,
            SUBMISSION,
        ),
        placeholders=(SUBMISSION,),
    )


def _untouched(record: Record) -> bool:
    from ..record import get_template

    try:
        return record.read_build() == get_template()
    except OSError:
        return False


def flow_next(record: Record, state: dict[str, Any] | None = None) -> Action | None:
    """The first unmet step whose prerequisites hold, or None when nothing remains."""
    from .state import flow_status

    meta = record.read_meta()
    if meta.flow is None:
        if meta.finalized:
            return None
        if _untouched(record):
            return repair_action(record, "Write the model in build.py.")
        if meta.status.value == "valid" and meta.validated_source_sha256 == (
            record.source_sha256
        ):
            return run("Finalize the validated build.", "external", "finalize", RECORD)
        if meta.validated_source_sha256 == record.source_sha256:
            return repair_action(record, "Fix the reported problems in build.py.")
        return validate_action()

    state = state or flow_status(record)
    if state["finalized"] and state["ready"]:
        return delivery_action(record)
    codes = [issue["code"] for issue in state["diagnostics"]]
    for code in _FIRST:
        if code not in codes:
            continue
        if code in ("flow.features", "flow.plan"):
            return _artifact_action(record, code.removeprefix("flow."))
        return None
    if not state["structural_passed"]:
        module = next(
            (
                issue
                for issue in state["diagnostics"]
                if issue["code"].startswith(("module.", "flow.modules"))
                and issue.get("next_action")
            ),
            None,
        )
        if module is not None:
            return repair_action(record, module["next_action"])
        if _untouched(record):
            return repair_action(record, "Write the model in build.py.")
        if meta.validated_source_sha256 == record.source_sha256:
            return repair_action(
                record,
                "Fix the collisions, unsupported parts and disconnected components in "
                "the structure report.",
            )
        return validate_action()
    if not state["ready"]:
        return _review_action(record, state)
    return run("Finalize the reviewed snapshot.", "external", "finalize", RECORD)


def _artifact_action(record: Record, name: str) -> Action:
    draft = record.path / "drafts" / f"{name}.json"
    summary = (
        f"Fill in the {name} draft. Read its schema with `external {name}`."
        if draft.is_file()
        else f"Write the {name} JSON at this path. Read its schema with `external {name}`."
    )
    return edit(
        summary,
        draft.resolve(),
        run(
            f"Submit the {name}.",
            "external",
            name,
            RECORD,
            "--set",
            str(draft.resolve()),
        ),
    )


def _review_action(record: Record, state: dict[str, Any]) -> Action:
    from ..aligned_review import compare_action, render_action, review_progress

    review = state["review"]
    if review.get("current") and not review.get("passed"):
        items = "; ".join(review.get("remaining_items") or [])
        return repair_action(
            record,
            "Repair the items from the accepted review, then validate, render, compare "
            f"and review the new snapshot. Remaining: {items}",
        )
    progress = review_progress(record, state["snapshot_id"], state["mpd_path"])
    rendered = progress["render"]
    task_cameras = (record.path / "task.json").is_file()
    if rendered is None and not task_cameras and record.read_meta().prompt_image:
        return run(
            "Prepare a matched-viewpoint review.",
            "external",
            "review-instructions",
            RECORD,
        )
    if rendered is None or not rendered["current"]:
        cameras = rendered and rendered["cameras"]
        from pathlib import Path

        return render_action(Path(cameras) if cameras else None)
    comparison = progress["comparison"]
    if comparison is None or not comparison["current"]:
        from pathlib import Path

        directory = comparison and comparison["reference_dir"]
        return compare_action(rendered["views"], Path(directory) if directory else None)
    if progress["open_round"] is not None:
        path = progress["open_round"]["report_path"]
        return edit(
            "Fill in the open round's report template and submit it.",
            path,
            run("Submit the report.", "external", "review", RECORD, "--set", path),
            fields=("conditions", "features", "findings", "verdict", "reviewer"),
        )
    return run(
        "Prepare a review round for the current snapshot.",
        "external",
        "review-instructions",
        RECORD,
    )
