import json
from types import SimpleNamespace

from brickwright.driver import tracking
from brickwright.evaluation import p001, p001_native


def make_unpriced():
    return p001.make_plan(
        model="openai/gpt-6.2-sol",
        effort="medium",
        repetitions=1,
        max_turns=30,
        max_validations=12,
        max_cost=None,
        build_timeout=30,
        render_timeout=60,
        request_timeout=120,
        trial_timeout=1200,
    )


def test_unpriced_plan_passes_bounded_settings_to_both_native_arms(tmp_path):
    plan = make_unpriced()
    assert plan["settings"]["max_cost"] is None
    assert plan["trial_timeout"] == 1200
    a, b, *_ = p001.slots(plan)
    commands = [
        p001.generate_command(tmp_path, tmp_path, plan, slot.task) for slot in (a, b)
    ]
    assert commands[0] == commands[1]
    assert "--unpriced" in commands[0]
    assert commands[0][commands[0].index("--max-cost") + 1] == "inf"
    assert "--trial-timeout" not in commands[0]


def test_unpriced_still_requires_consistent_usage_and_enforces_turn_budget():
    settings = make_unpriced()["settings"]
    usage = {
        "prompt_tokens": 11,
        "completion_tokens": 5,
        "total_tokens": 16,
        "cost_usd": None,
    }
    response = {"model": "gpt-6.2-sol", "usage": usage.copy()}
    meta = {"usage": usage, "stop_reason": "finalized", "turns": 1, "validations": 1}
    assert p001.usage_problem(meta, [response], allow_unpriced=True) is None
    assert p001.budget_problem(meta, [response], settings) is None
    assert p001.budget_problem(meta | {"validations": 13}, [response], settings)
    response["usage"]["prompt_tokens"] = 12
    assert p001.usage_problem(meta, [response], allow_unpriced=True)


def test_unpriced_native_tracks_tokens_and_does_not_convert_missing_cost_to_zero(
    monkeypatch, tmp_path
):
    log = tmp_path / "responses.jsonl"
    response = SimpleNamespace(
        model="gpt-6.2-sol",
        usage=SimpleNamespace(prompt_tokens=8, completion_tokens=3, total_tokens=11),
        _hidden_params={"response_cost": 0.0},
    )

    def main():
        tracker = tracking.RunTracker()
        for _ in range(2):
            tracker.record(response)
            assert not tracker.has_unknown_cost()
        assert tracker.usage().cost_usd is None
        assert tracker.usage().total_tokens == 22

    original_unknown = tracking.RunTracker.has_unknown_cost
    original_cost = tracking._response_cost
    monkeypatch.setattr(p001_native, "main", main)
    p001_native.run(log, unpriced=True)
    assert tracking.RunTracker.has_unknown_cost is original_unknown
    assert tracking._response_cost is original_cost
    assert [row["usage"]["cost_usd"] for row in p001.read_responses(log)] == [
        None,
        None,
    ]


def test_native_priced_record_has_null_cap_when_unpriced():
    from brickwright.config import Config

    assert Config(max_cost=float("inf")).model_dump(mode="json")["max_cost"] == float(
        "inf"
    )
    assert '"max_cost":null' in Config(max_cost=float("inf")).model_dump_json()


def test_bound_openai_transport_attaches_only_the_named_key_and_disallows_redirects(
    monkeypatch, tmp_path
):
    import httpx
    import litellm
    import pytest

    from brickwright.driver.completion import litellm_complete

    key_file = tmp_path / "key"
    key_file.write_text("named-test-token")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-test-token")
    endpoint = "https://provider.example/v1"
    original_client = httpx.Client
    original_completion = litellm.completion
    urls = []

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False

        def send(request):
            urls.append(str(request.url))
            assert request.headers["authorization"] == "Bearer named-test-token"
            request_data = json.loads(request.content)
            assert request_data["reasoning"]["effort"] == "medium"
            assert request_data["tools"][0]["name"] == "recheck_build"
            return httpx.Response(
                307, headers={"Location": "https://other.example/redirect"}
            )

        return original_client(transport=httpx.MockTransport(send), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    with (
        p001_native._bound_responses(endpoint, key_file),
        pytest.raises(RuntimeError, match="Bound Responses request failed"),
    ):
        litellm_complete()(
            model="openai/gpt-6.2-sol",
            reasoning_effort="medium",
            messages=[{"role": "user", "content": "Test."}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "recheck_build",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            tool_choice="auto",
        )
    assert urls == [f"{endpoint}/responses"]
    assert litellm.completion is original_completion
