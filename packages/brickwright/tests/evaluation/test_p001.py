import json

import pytest

from brickwright.evaluation import p001


@pytest.fixture
def plan():
    return p001.make_plan(
        model="openai/a-fixed-model",
        effort="medium",
        repetitions=2,
        max_turns=30,
        max_validations=12,
        max_cost=0.25,
        build_timeout=30,
        render_timeout=60,
        request_timeout=120,
    )


def test_same_prompt_and_native_settings_for_every_pair(plan, tmp_path):
    slots = list(p001.slots(plan))
    assert len(slots) == 2 * 2 * 5
    assert [slot.arm for slot in slots[:4]] == ["old", "new", "new", "old"]
    for left, right in zip(slots[::2], slots[1::2]):
        assert left.task == right.task
        assert left.repetition == right.repetition
        old_command = p001.generate_command(
            tmp_path / "a", tmp_path / "old", plan, left.task
        )
        new_command = p001.generate_command(
            tmp_path / "b", tmp_path / "new", plan, right.task
        )
        assert old_command[1:3] == new_command[1:3]
        assert old_command[4:6] == new_command[4:6]
        assert old_command[7:] == new_command[7:]
        assert old_command[-1] == left.task["prompt"]
    assert plan["settings"]["max_cost"] == 0.25


def test_plan_is_frozen_before_run(plan, tmp_path):
    output = tmp_path / "run"
    p001.freeze_plan(output, plan)
    assert p001.load_plan(output) == plan
    with pytest.raises(FileExistsError):
        p001.freeze_plan(output, plan)
    path = output / "private" / "plan.json"
    path.write_text(json.dumps({**plan, "repetitions": 3}))
    with pytest.raises(ValueError, match="frozen plan"):
        p001.load_plan(output)


@pytest.mark.parametrize("field,value", [("max_turns", 0), ("max_cost", float("inf"))])
def test_plan_requires_finite_positive_budgets(plan, field, value):
    settings = dict(plan["settings"])
    for flag in p001.DISABLED:
        del settings[flag]
    settings[field] = value
    with pytest.raises(ValueError, match=field):
        p001.make_plan(repetitions=1, **settings)


@pytest.mark.parametrize("field,value", [("model", "another-model"), ("max_cost", 2.0)])
def test_record_must_match_frozen_generation_settings(plan, field, value):
    slot = next(p001.slots(plan))
    settings = dict(plan["settings"])
    metadata = {
        "prompt": slot.task["prompt"],
        "generation": {
            "external": False,
            "model": settings["model"],
            "effort": settings["effort"],
            "config": settings,
        },
    }
    p001.check_generation(slot, plan, metadata)
    settings[field] = value
    with pytest.raises(ValueError, match=field):
        p001.check_generation(slot, plan, metadata)


@pytest.mark.parametrize("cost", [None, 0, -0.1])
def test_nonpositive_or_missing_pricing_blocks_spend_comparison(cost):
    metadata = {"usage": {"total_tokens": 100, "cost_usd": cost}}
    assert "budget" in (p001.usage_problem(metadata) or "")


def test_records_without_model_access_do_not_count_as_a_behavioral_comparison():
    assert "no model response" in (
        p001.usage_problem(
            {"stop_reason": "error", "usage": {"total_tokens": 0, "cost_usd": None}}
        )
        or ""
    )
    assert (
        p001.usage_problem(
            {
                "stop_reason": "max_turns",
                "usage": {"total_tokens": 100, "cost_usd": 0.01},
            }
        )
        is None
    )


def test_check_observed_budget_keeps_one_post_response_overrun(plan):
    settings = plan["settings"]
    metadata = {"turns": 2, "validations": 3}
    priced_responses = [{"usage": {"cost_usd": 0.15}}, {"usage": {"cost_usd": 0.2}}]
    assert p001.budget_problem(metadata, priced_responses, settings) is None
    priced_responses[0]["usage"]["cost_usd"] = settings["max_cost"]
    problem = p001.budget_problem(metadata, priced_responses, settings)
    assert "after reaching" in (problem or "")


def test_check_observed_budget_checks_turn_and_validation_limits(plan):
    settings = plan["settings"]
    metadata = {
        "turns": settings["max_turns"],
        "validations": settings["max_validations"] + 1,
    }
    responses = [{"usage": {"cost_usd": 0.001}}] * settings["max_turns"]
    assert "validation" in (p001.budget_problem(metadata, responses, settings) or "")
