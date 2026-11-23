import base64
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from audit import matches_original, observed_sessions


class AuditTest(unittest.TestCase):
    def test_image_proof_requires_a_tool_returning_the_exact_bytes(self):
        source = b"image payload"
        uri = "data:image/png;base64," + base64.b64encode(source).decode()
        image = {"type": "input_image", "image_url": uri}
        events = [
            {"type": "session_meta", "payload": {"id": "child"}},
            {
                "type": "response_item",
                "payload": {"type": "message", "content": [image]},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "input": "tools.view_image({path})",
                    "call_id": "image-reading",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "image-reading",
                    "output": [image],
                },
            },
        ]
        observed = observed_sessions(events)
        self.assertEqual(observed["images"][hashlib.sha256(source).hexdigest()], 1)

    def test_dispatch_pairs_receipt_with_child_path(self):
        events = [
            {"type": "session_meta", "payload": {"id": "parent"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "spawn",
                    "arguments": '{"task_name":"review","fork_turns":"none"}',
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "spawn",
                    "output": '{"task_name":"/root/review"}',
                },
            },
        ]
        self.assertIn("/root/review", observed_sessions(events)["fresh_dispatches"])

    def test_original_encoded_task_and_conditions_both_must_match(self):
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            original = root / "original"
            original.mkdir()
            body = b'{"id":"task","checks":["original requirement"]}'
            (original / "task.json").write_bytes(body)
            (root / "task.json").write_bytes(body)
            (root / "features.json").write_text(
                '{"features":[{"evidence":[{"check_id":"C01"}]}]}'
            )
            conditions = {
                "task_id": "task",
                "task_sha256": hashlib.sha256(body).hexdigest(),
                "checks": [{"id": "C01", "text": "original requirement"}],
            }
            encoded = json.dumps(conditions).encode()
            (root / "conditions.json").write_bytes(encoded)
            meta = {"conditions_sha256": hashlib.sha256(encoded).hexdigest()}
            self.assertEqual(matches_original(root, original, meta), (True, {"C01"}))
            (root / "task.json").write_bytes(body + b"\n")
            self.assertEqual(matches_original(root, original, meta), (False, {"C01"}))


if __name__ == "__main__":
    unittest.main()
