"""Invoke the checkout's native CLI and archive actual response model and usage."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from brickwright.cli import main
from brickwright.driver import tracking


def run(response_log: Path) -> None:
    original = tracking.RunTracker.record

    def record(self: tracking.RunTracker, response: Any) -> None:
        original(self, response)
        usage = tracking._usage_of(response)
        model = getattr(response, "model", None)
        with response_log.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"model": model, "usage": usage.model_dump()}) + "\n"
            )
        if usage.total_tokens <= 0 or usage.cost_usd is None or usage.cost_usd <= 0:
            raise RuntimeError("P-001 cannot verify usage or enforce the spend budget")
        if not isinstance(model, str) or not model:
            raise RuntimeError("P-001 could not verify the responding model")

    tracking.RunTracker.record = record
    try:
        main()
    finally:
        tracking.RunTracker.record = original


if __name__ == "__main__":
    log = Path(sys.argv.pop(1))
    run(log)
