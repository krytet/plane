from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import hook_handler

THREAD_ID = "01900000-0000-7000-8000-000000000000"
TURN_ID = "01900000-0000-7000-8000-000000000001"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def payload(event: str, **extra: object) -> dict[str, object]:
    return {
        "session_id": THREAD_ID,
        "turn_id": TURN_ID,
        "hook_event_name": event,
        "transcript_path": "/private/transcript.jsonl",
        "prompt": "prompt-secret",
        "tool_input": {"command": "tool-input-secret"},
        "tool_response": "tool-output-secret",
        "last_assistant_message": "assistant-secret",
        **extra,
    }


class HookHandlerTests(unittest.TestCase):
    def test_maps_each_supported_event_without_retaining_payload_text(self) -> None:
        expected = {
            "UserPromptSubmit": ("running", False),
            "PermissionRequest": ("needs_action", False),
            "PostToolUse": ("running", False),
            "Stop": ("answer_available", False),
            "Interrupt": ("needs_action", True),
        }
        for event, (state, terminal) in expected.items():
            with self.subTest(event=event), tempfile.TemporaryDirectory() as directory:
                spool = Path(directory)
                self.assertTrue(hook_handler.observe(payload(event), spool, NOW))
                files = list(spool.glob("*.json"))
                self.assertEqual(len(files), 1)
                content = files[0].read_text(encoding="utf-8")
                record = json.loads(content)
                self.assertEqual(record["event"], event)
                self.assertEqual(record["state"], state)
                self.assertEqual(record["terminal"], terminal)
                self.assertEqual(record["thread_id"], THREAD_ID)
                self.assertEqual(record["turn_id"], TURN_ID)
                self.assertEqual(record["observed_at"], "2026-10-22T12:00:00.000000Z")
                self.assertEqual(
                    set(record),
                    {
                        "version",
                        "id",
                        "source",
                        "event",
                        "state",
                        "terminal",
                        "thread_id",
                        "turn_id",
                        "observed_at",
                    },
                )
                for secret in (
                    "prompt-secret",
                    "tool-input-secret",
                    "tool-output-secret",
                    "assistant-secret",
                    "transcript.jsonl",
                ):
                    self.assertNotIn(secret, content)

    def test_duplicate_delivery_is_idempotent_for_the_same_receipt_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory)
            self.assertTrue(hook_handler.observe(payload("Stop"), spool, NOW))
            first = (spool / f"{hook_handler.observation_from_payload(payload('Stop'), NOW)['id']}.json").read_bytes()

            self.assertTrue(hook_handler.observe(payload("Stop"), spool, NOW))
            files = list(spool.glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), first)

    def test_post_tool_heartbeat_is_rate_limited_per_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory)
            self.assertTrue(hook_handler.observe(payload("PostToolUse"), spool, NOW))
            self.assertFalse(hook_handler.observe(payload("PostToolUse"), spool, NOW + timedelta(seconds=10)))
            self.assertEqual(len(list(spool.glob("*.json"))), 1)

            self.assertTrue(hook_handler.observe(payload("PostToolUse"), spool, NOW + timedelta(seconds=31)))
            self.assertEqual(len(list(spool.glob("*.json"))), 2)
            event_ids = {json.loads(path.read_text(encoding="utf-8"))["id"] for path in spool.glob("*.json")}
            self.assertEqual(len(event_ids), 2)
            self.assertTrue((spool / ".state").is_dir())

    def test_stop_allows_one_explicit_post_tool_resume_without_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory)
            self.assertTrue(hook_handler.observe(payload("PostToolUse"), spool, NOW))
            self.assertFalse(hook_handler.observe(payload("PostToolUse"), spool, NOW + timedelta(seconds=1)))
            self.assertTrue(hook_handler.observe(payload("Stop"), spool, NOW + timedelta(seconds=2)))
            self.assertTrue(hook_handler.observe(payload("PostToolUse"), spool, NOW + timedelta(seconds=3)))
            self.assertFalse(hook_handler.observe(payload("PostToolUse"), spool, NOW + timedelta(seconds=4)))

            events = [json.loads(path.read_text(encoding="utf-8"))["event"] for path in spool.glob("*.json")]
            self.assertEqual(events.count("Stop"), 1)
            self.assertEqual(events.count("PostToolUse"), 2)

    def test_repeated_permission_events_and_stop_events_keep_unique_spool_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory)
            self.assertTrue(
                hook_handler.observe(
                    payload("PermissionRequest", tool_name="Bash"),
                    spool,
                    NOW,
                )
            )
            self.assertTrue(hook_handler.observe(payload("PostToolUse"), spool, NOW + timedelta(seconds=1)))
            self.assertTrue(
                hook_handler.observe(
                    payload("PermissionRequest", tool_name="Bash"),
                    spool,
                    NOW + timedelta(seconds=2),
                )
            )
            self.assertTrue(hook_handler.observe(payload("Stop"), spool, NOW + timedelta(seconds=3)))
            self.assertTrue(hook_handler.observe(payload("Stop"), spool, NOW + timedelta(seconds=4)))

            records = [json.loads(path.read_text(encoding="utf-8")) for path in spool.glob("*.json")]
            self.assertEqual(len(records), 5)
            self.assertEqual(len({record["id"] for record in records}), 5)
            permissions = [record for record in records if record["event"] == "PermissionRequest"]
            self.assertEqual(len(permissions), 2)
            self.assertNotEqual(permissions[0]["id"], permissions[1]["id"])

    def test_invalid_or_unbounded_payloads_are_no_ops(self) -> None:
        invalid = [
            None,
            {},
            payload("UserPromptSubmit", session_id="not-a-uuid"),
            payload("UserPromptSubmit", turn_id="thr_123"),
            payload("PreToolUse"),
            payload("Unknown"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory)
            for value in invalid:
                with self.subTest(value=value):
                    self.assertFalse(hook_handler.observe(value, spool, NOW))
            self.assertEqual(list(spool.iterdir()), [])

        valid = json.dumps(payload("Stop")).encode("utf-8")
        self.assertIsNone(hook_handler.read_payload(io.BytesIO(valid + b" " * hook_handler.MAX_INPUT_BYTES)))
        self.assertIsNone(hook_handler.read_payload(io.BytesIO(b"{not-json")))

    def test_cli_is_silent_for_valid_malformed_and_oversized_input(self) -> None:
        handler = Path(__file__).with_name("hook_handler.py")
        cases = [
            json.dumps(payload("PermissionRequest")).encode("utf-8"),
            b"{malformed",
            b" " * (hook_handler.MAX_INPUT_BYTES + 1),
        ]
        for stdin in cases:
            with self.subTest(size=len(stdin)), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [sys.executable, handler, "--spool-dir", directory],
                    input=stdin,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
