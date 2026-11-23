import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from maestro import PROTOCOL, comparison_request, extract_run, session_usage, trial_row


class EvaluationTest(unittest.TestCase):
    def test_six_task_comparison_replaces_only_product_attachment(self):
        baseline = {
            "attachments": [PROTOCOL["baseline"], "shared"],
            "dataset_version_id": PROTOCOL["dataset"],
            "repetitions": 2,
            "task_selection": {"mode": "all_eligible"},
        }
        candidate = comparison_request(baseline, "candidate", "new")
        self.assertEqual(candidate, dict(baseline, attachments=["new", "shared"]))
        self.assertEqual(baseline["attachments"][0], PROTOCOL["baseline"])

    def test_bus_arms_share_task_and_product(self):
        baseline = {"attachments": [PROTOCOL["baseline"], "shared"], "repetitions": 2}
        whole = comparison_request(baseline, "candidate", "new")
        modular = comparison_request(baseline, "bus-modular", "new", "task", "module")
        self.assertEqual(
            modular,
            dict(
                whole,
                attachments=["new", "shared", "module"],
                task_selection={"mode": "explicit", "task_ids": ["task"]},
            ),
        )

    def test_strips_archive_run_name_prefix(self):
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            source = root / "source.zip"
            with ZipFile(source, "w") as archive:
                archive.writestr("run-label/manifest.json", "{}")
                archive.writestr("run-label/tasks/R-10258/result.json", "{}")
            extract_run(source, root)
            self.assertTrue((root / "manifest.json").exists())
            self.assertTrue((root / "tasks/R-10258/result.json").exists())

    def test_unscored_is_not_zero_and_invalid_is_a_real_zero(self):
        trial = {
            "id": "trial",
            "task_key": "R-10258",
            "state": "execution_error",
            "repetition_index": 1,
            "reason_code": "failed",
            "result": None,
        }
        row = trial_row(Path("/tmp/missing-evaluation-archive"), [], trial)
        self.assertIsNone(row["score"])
        self.assertIsNone(row["delivered"])
        trial["state"] = "scored"
        trial["result"] = {"attempt_id": "attempt", "score": 0.0}
        row = trial_row(Path("/tmp/missing-evaluation-archive"), [], trial)
        self.assertEqual(row["score"], 0.0)
        self.assertIsNone(row["delivered"])

    def test_codex_usage_takes_last_nonempty_total_for_each_session(self):
        rows = [
            {"type": "session_meta", "payload": {"id": "session", "source": "exec"}},
            {
                "type": "turn_context",
                "payload": {"model": "gpt-6.2-sol", "effort": "medium"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 12,
                            "cached_input_tokens": 8,
                            "output_tokens": 4,
                        }
                    },
                },
            },
            {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
        ]
        record = session_usage(rows)
        self.assertEqual(record["session_id"], "session")
        self.assertEqual(record["model"], ["gpt-6.2-sol"])
        self.assertEqual(record["effort"], ["medium"])
        self.assertEqual(record["tokens"]["output_tokens"], 4)


if __name__ == "__main__":
    unittest.main()
