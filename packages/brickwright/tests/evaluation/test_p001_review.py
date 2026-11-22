import json
from pathlib import Path

import pytest
from bricklayer import LegoBuild, brick, plate, to_ldraw

from brickwright.evaluation import p001, p001_review

_BUILD = """\
from bricklayer import LegoBuild, brick, plate

def create_build():
    result = LegoBuild()
    base = result.add(brick(1, 1))
    {placement}
    return result

def run_tests():
    return []
"""


@pytest.fixture
def comparison(tmp_path, monkeypatch):
    plan = p001.make_plan(
        model="openai/a-fixed-model",
        effort="medium",
        repetitions=1,
        max_turns=30,
        max_validations=12,
        max_cost=0.25,
        build_timeout=30,
        render_timeout=60,
        request_timeout=120,
    )
    plan["tasks"] = plan["tasks"][:1]
    output = tmp_path / "comparison"
    p001.freeze_plan(output, plan)
    root = Path(__file__).resolve().parents[4]
    monkeypatch.setattr(
        p001_review, "checkout_commit", lambda checkout: f"commit-{checkout.name}"
    )

    for slot in p001.slots(plan):
        checkout = tmp_path / slot.arm
        checkout.mkdir()
        (checkout / ".venv").symlink_to(root / ".venv", target_is_directory=True)
        (checkout / "uv.lock").symlink_to(root / "uv.lock")
        # Both have the same correctly mated LEGO geometry. Only the new fixture
        # positions its leaf by calling the placement solver at execution time.
        placement = (
            'result.connect(plate(1, 1), "antistud", to=base.find_connector("stud"))'
            if slot.arm == "new"
            else "result.stack(plate(1, 1), on=base)"
        )
        _stage_run(output, checkout, slot, plan, placement)
    return output


def _stage_run(output, checkout, slot, plan, placement):
    private = output / "private"
    commit = f"commit-{slot.arm}"
    p001.write_json(
        private / f"{slot.arm}.json",
        {
            "checkout": str(checkout),
            "commit": commit,
            "runtime": {},
            "lock_sha256": p001.digest(checkout / "uv.lock"),
        },
    )
    directory = slot.directory(output)
    record = directory / "records" / f"20260101-000000-{slot.arm}-12345678"
    (record / "out").mkdir(parents=True)
    source = record / "build.py"
    source.write_text(_BUILD.format(placement=placement))
    source_hash = p001.digest(source)
    build = LegoBuild()
    base = build.add(brick(1, 1))
    build.stack(plate(1, 1), on=base)
    (record / "out" / "build.ldr").write_text(to_ldraw(build))
    p001.write_json(
        record / "out" / "report.json",
        {"ok": True, "source_sha256": source_hash, "brick_count": 2},
    )
    usage = {
        "prompt_tokens": 9,
        "completion_tokens": 1,
        "total_tokens": 10,
        "cost_usd": 0.03,
        "known_cost_usd": 0.03,
    }
    p001.write_json(
        record / "meta.json",
        {
            "id": record.name,
            "finalized": True,
            "status": "valid",
            "stop_reason": "finalized",
            "validated_source_sha256": source_hash,
            "exported_source_sha256": source_hash,
            "turns": 1,
            "validations": 1,
            "prompt": slot.task["prompt"],
            "generation": {
                "external": False,
                "model": plan["settings"]["model"],
                "effort": plan["settings"]["effort"],
                "config": plan["settings"],
            },
            "usage": usage,
        },
    )
    responses = directory / "responses.jsonl"
    responses.write_text(
        json.dumps({"model": "pinned-model-20260101", "usage": usage}) + "\n"
    )
    p001.write_json(
        directory / "attempt.json",
        {
            "slot": slot.key,
            "plan_sha256": p001.digest(private / "plan.json"),
            "commit": commit,
            "exit_code": 0,
            "record_id": record.name,
            "meta_sha256": p001.digest(record / "meta.json"),
            "files": {
                name: p001.digest(record / name) if (record / name).exists() else None
                for name in (
                    "build.py",
                    "out/build.ldr",
                    "out/build.png",
                    "out/report.json",
                )
            },
            "usage_problem": None,
            "responses_sha256": p001.digest(responses),
        },
    )


def test_measurement_review_and_aggregation(comparison):
    # A pair of real local SDK model replays, without any LLM requests.
    p001_review.prepare_review(comparison)
    review = comparison / "review"
    cases = p001.json_data(review / "review.json")["items"]
    assert len(cases) == 2
    assert {path.name for path in review.iterdir()} == {
        "review.json",
        "scores.json",
        *(case["id"] for case in cases),
    }
    for case in cases:
        assert set(case) == {"id", "prompt", "requirements", "artifact", "evidence"}
        assert case["artifact"] == "model.ldr"
        assert case["evidence"]["pieces"] == 2
        assert list((review / case["id"]).iterdir()) == [
            review / case["id"] / "model.ldr"
        ]

    scores = p001.json_data(review / "scores.json")
    assert p001_review.build_report(comparison, None)["status"] == "pending"
    for case in cases:
        scores["scores"][case["id"]] = {
            "structure": 2,
            "appearance": 1,
            "function": 1,
            "complete": False,
            "notes": "Stable sample geometry. The requested sign is absent.",
        }
    scores_path = review / "scores.json"
    p001.write_json(scores_path, scores)
    report = p001_review.build_report(comparison, scores_path)
    assert report["status"] == "passed"
    assert report["arms"]["old"]["connector_share"] == 0
    assert report["arms"]["new"]["connector_share"] == 0.5
    assert report["arms"]["old"]["completion_rate"] == 0
    assert report["arms"]["new"]["quality_mean"] == 4
    assert report["arms"]["new"]["cost_usd"] == 0.03

    old_id = report["pairs"][0]["old"]["review_id"]
    new_id = report["pairs"][0]["new"]["review_id"]
    scores["scores"][new_id]["appearance"] = 0
    p001.write_json(scores_path, scores)
    regression = p001_review.build_report(comparison, scores_path)
    assert regression["status"] == "failed"
    assert regression["checks"]["quality_maintained"] is False

    scores["scores"][old_id].update(function=2, complete=True)
    scores["scores"][new_id].update(appearance=2)
    p001.write_json(scores_path, scores)
    regression = p001_review.build_report(comparison, scores_path)
    assert regression["status"] == "failed"
    assert regression["checks"]["quality_maintained"] is True
    assert regression["checks"]["completion_maintained"] is False

    p001.write_json(scores_path, {**scores, "review_sha256": "different batch"})
    with pytest.raises(ValueError, match="review bundle"):
        p001_review.build_report(comparison, scores_path)


def test_missing_trial_counts_against_completion_without_acceptance(comparison):
    slot = next(p001.slots(p001.load_plan(comparison)))
    (slot.directory(comparison) / "attempt.json").unlink()
    with pytest.raises(ValueError, match="unstarted or interrupted"):
        p001_review.prepare_review(comparison)
    report = p001_review.build_report(comparison, None)
    assert report["status"] == "pending"
    assert report["arms"]["old"]["planned_runs"] == 1
    assert report["arms"]["old"]["completed_tasks"] == 0


def test_rejects_altered_native_record(comparison):
    slot = next(p001.slots(p001.load_plan(comparison)))
    directory = slot.directory(comparison)
    record = next((directory / "records").iterdir())
    (record / "build.py").write_text("# updated after the run\n")
    with pytest.raises(ValueError, match="build.py changed"):
        p001_review.build_report(comparison, None)
