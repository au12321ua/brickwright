"""Invoke the checkout's native CLI and archive actual response model and usage."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from unittest.mock import patch

from brickwright.cli import main
from brickwright.driver import tracking


@contextmanager
def _bound_responses(api_base: str, key_file: Path) -> Generator[None]:
    import httpx
    import litellm
    import openai
    from litellm.llms.custom_httpx.http_handler import HTTPHandler
    from litellm.responses import main as responses

    key = key_file.read_text().strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError("evaluation key file must contain a single bearer token")
    destination = httpx.URL(f"{api_base}/responses")
    original = litellm.completion
    retry_delay: float | None = None
    quota_exhausted = False

    def enforce_destination(request: httpx.Request) -> None:
        if request.url != destination or request.method != "POST":
            raise RuntimeError("evaluation request moved outside its bound endpoint")

    def observe_rate_limit(response: httpx.Response) -> None:
        nonlocal retry_delay, quota_exhausted
        if response.status_code == 429:
            delay = response.headers.get("Retry-After", "")
            retry_delay = float(delay) if delay.isdecimal() else None
            quota_exhausted = _quota_exhausted(response.read().decode(errors="replace"))

    with httpx.Client(
        follow_redirects=False,
        trust_env=False,
        event_hooks={
            "request": [enforce_destination],
            "response": [observe_rate_limit],
        },
    ) as client:
        handler = HTTPHandler(client=client, follow_redirects=False)

        def complete(*positional: Any, **kwargs: Any) -> Any:
            nonlocal retry_delay, quota_exhausted
            if not (kwargs.get("model") or "").startswith("openai/responses/"):
                raise RuntimeError(
                    "evaluation must use the native OpenAI Responses route"
                )
            dispatch_mcp(tools=kwargs.get("tools"))
            # LiteLLM's local model table lacks this GPT-6 route's native
            # function tool choice. Pass it through alongside reasoning.
            kwargs["allowed_openai_params"] = [
                *(kwargs.get("allowed_openai_params") or ()),
                "tool_choice",
            ]
            deadline = monotonic() + float(kwargs.pop("timeout", None) or 120)
            for backoff in (5, 15, 30, None):
                retry_delay, quota_exhausted = None, False
                try:
                    return original(
                        *positional,
                        **kwargs,
                        client=handler,
                        api_key=key,
                        api_base=api_base,
                        _skip_mcp_handler=True,
                        timeout=max(0.001, deadline - monotonic()),
                    )
                except (openai.APIError, httpx.HTTPError, ValueError) as error:
                    status = getattr(error, "status_code", None)
                    if status == 429 and backoff is not None and not quota_exhausted:
                        delay = (
                            retry_delay if retry_delay is not None else float(backoff)
                        )
                        if delay < deadline - monotonic():
                            print(
                                f"Bound Responses route limited; retry in {delay:g}s.",
                                flush=True,
                            )
                            sleep(delay)
                            continue
                    detail = f" ({status})" if isinstance(status, int) else ""
                    reason = " (quota exhausted)" if quota_exhausted else ""
                    raise RuntimeError(
                        f"Bound Responses request failed: {type(error).__name__}{detail}{reason}"
                    ) from None
            raise AssertionError("the final bound Responses attempt must terminate")

        def dispatch_mcp(*, tools: Any, **kwargs: Any) -> None:
            # The pinned bridge imports its optional MCP gateway even for
            # native function tools. A bound evaluation has no MCP tools.
            if any(tool.get("type") == "mcp" for tool in tools or ()):
                raise ValueError(
                    "MCP gateway tools are unavailable in a bound evaluation"
                )

        with (
            patch.object(litellm, "completion", complete),
            patch.object(
                responses, "_responses_try_dispatch_mcp_gateway", dispatch_mcp
            ),
        ):
            yield


def _quota_exhausted(body: str) -> bool:
    body = body.casefold()
    return any(
        clue in body
        for clue in (
            "insufficient_quota",
            "quota_exceeded",
            "insufficient balance",
            "balance exhausted",
            "余额不足",
            "额度不足",
        )
    )


def run(response_log: Path, *, unpriced: bool = False) -> None:
    original = tracking.RunTracker.record

    def record(self: tracking.RunTracker, response: Any) -> None:
        original(self, response)
        usage = tracking._usage_of(response)
        model = getattr(response, "model", None)
        with response_log.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"model": model, "usage": usage.model_dump()}) + "\n"
            )
        if usage.total_tokens <= 0:
            raise RuntimeError("P-001 could not verify model token usage")
        if not unpriced and (usage.cost_usd is None or usage.cost_usd <= 0):
            raise RuntimeError("P-001 cannot verify usage or enforce the spend budget")
        if not isinstance(model, str) or not model:
            raise RuntimeError("P-001 could not verify the responding model")

    with ExitStack() as patches:
        patches.enter_context(patch.object(tracking.RunTracker, "record", record))
        if unpriced:
            # Both native CLIs receive an unbounded max_cost. Only this process
            # suppresses the missing-price gate. The original tracker still
            # persists genuine tokens and an unknown dollar total.
            patches.enter_context(
                patch.object(
                    tracking.RunTracker, "has_unknown_cost", lambda self: False
                )
            )
            patches.enter_context(
                patch.object(tracking, "_response_cost", lambda response: None)
            )
        main()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("response_log", type=Path)
    parser.add_argument("--unpriced", action="store_true")
    parser.add_argument("--api-base")
    parser.add_argument("--api-key-file", type=Path)
    args, tail = parser.parse_known_args()
    sys.argv[1:] = tail
    if args.unpriced:
        print("P-001 unpriced trial: total dollar cost is unknown.", flush=True)
    if args.api_base is None:
        run(args.response_log, unpriced=args.unpriced)
    else:
        with _bound_responses(args.api_base, args.api_key_file):
            run(args.response_log, unpriced=args.unpriced)
