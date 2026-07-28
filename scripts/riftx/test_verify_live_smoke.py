import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).with_name("verify-live-smoke.py")
SPEC = importlib.util.spec_from_file_location("verify_live_smoke", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VerifyLiveSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.files = self._write_valid_case()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_json(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _write_valid_case(self) -> dict[str, Path]:
        execution_id = "exec-1"
        turn_id = "turn-1"
        return {
            "capability": self._write_json(
                "capability.json",
                {
                    "profileName": "responses_live",
                    "protocol": "responses",
                    "model": "live-model",
                    "ok": True,
                    "capabilities": {
                        layer: {"status": "passed", "detail": "ok"}
                        for layer in ("config", "streamText", "functionTools")
                    },
                },
            ),
            "report": self._write_json(
                "report.json",
                {
                    "schema": "riftxReportV1",
                    "llmProfile": {"name": "responses_live", "protocol": "responses"},
                    "executions": [
                        {
                            "id": execution_id,
                            "turnId": turn_id,
                            "status": "completed",
                            "exitCode": 0,
                            "stdoutBytes": 24,
                        }
                    ],
                    "artifacts": [
                        {
                            "id": "artifact-1",
                            "executionId": execution_id,
                            "path": "artifacts/responses-live-smoke.txt",
                            "sizeBytes": 12,
                            "sha256": "a" * 64,
                        }
                    ],
                },
            ),
            "conversation": self._write_json(
                "conversation.json",
                {
                    "data": [
                        {
                            "role": "agent",
                            "kind": "message",
                            "turnId": turn_id,
                            "text": "Done. RIFTX_RESPONSES_FINAL_OK",
                        }
                    ],
                    "nextCursor": None,
                },
            ),
            "events": self._write_json_lines(
                "events.ndjson",
                [{"event": "turn/completed", "data": {"turnId": turn_id}}],
            ),
        }

    def _write_json_lines(self, name: str, values: list[object]) -> Path:
        path = self.root / name
        path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")
        return path

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            protocol="responses",
            profile="responses_live",
            capability=self.files["capability"],
            report=self.files["report"],
            conversation=self.files["conversation"],
            events=self.files["events"],
            expected_marker="RIFTX_RESPONSES_FINAL_OK",
            expected_artifact="artifacts/responses-live-smoke.txt",
            scan_file=[],
            secret_env=[],
            output=self.root / "summary.json",
        )

    def test_accepts_structured_capability_and_execution_bound_tool_loop(self) -> None:
        summary = MODULE.verify(self._args())
        self.assertEqual(summary["schema"], "riftxLiveSmokeSummaryV1")
        self.assertEqual(summary["protocol"], "responses")
        self.assertEqual(
            summary["toolLoop"],
            {
                "turnId": "turn-1",
                "executionId": "exec-1",
                "artifactId": "artifact-1",
                "artifactSha256": "a" * 64,
                "finalMarkerObserved": True,
                "turnCompletedObserved": True,
            },
        )

    def test_rejects_text_only_tool_claim_without_execution(self) -> None:
        self.files["report"].write_text(
            json.dumps(
                {
                    "llmProfile": {"name": "responses_live", "protocol": "responses"},
                    "executions": [],
                    "artifacts": [],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MODULE.VerificationError, "successful execution"):
            MODULE.verify(self._args())

    def test_rejects_final_marker_from_a_different_turn(self) -> None:
        conversation = json.loads(self.files["conversation"].read_text(encoding="utf-8"))
        conversation["data"][0]["turnId"] = "turn-other"
        self.files["conversation"].write_text(json.dumps(conversation), encoding="utf-8")
        with self.assertRaisesRegex(MODULE.VerificationError, "final agent marker"):
            MODULE.verify(self._args())

    def test_rejects_artifact_not_bound_to_execution(self) -> None:
        report = json.loads(self.files["report"].read_text(encoding="utf-8"))
        report["artifacts"][0]["executionId"] = "exec-other"
        self.files["report"].write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(MODULE.VerificationError, "execution-bound artifact"):
            MODULE.verify(self._args())

    def test_rejects_mismatched_turn_completion_event(self) -> None:
        self._write_json_lines(
            "events.ndjson",
            [{"event": "turn/completed", "data": {"turnId": "turn-other"}}],
        )
        with self.assertRaisesRegex(MODULE.VerificationError, "executed turn"):
            MODULE.verify(self._args())

    def test_rejects_secret_in_any_scanned_evidence(self) -> None:
        extra = self.root / "daemon.log"
        extra.write_text("provider-key-value", encoding="utf-8")
        args = self._args()
        args.scan_file = [extra]
        args.secret_env = ["RIFTX_TEST_SECRET"]
        with mock.patch.dict(os.environ, {"RIFTX_TEST_SECRET": "provider-key-value"}):
            with self.assertRaisesRegex(MODULE.VerificationError, "appeared"):
                MODULE.verify(args)

    def test_accepts_chat_protocol_wire_names(self) -> None:
        capability = json.loads(self.files["capability"].read_text(encoding="utf-8"))
        capability.update(profileName="chat_live", protocol="chat_completions")
        self.files["capability"].write_text(json.dumps(capability), encoding="utf-8")
        report = json.loads(self.files["report"].read_text(encoding="utf-8"))
        report["llmProfile"] = {"name": "chat_live", "protocol": "chatCompletions"}
        report["artifacts"][0]["path"] = "artifacts/chat-live-smoke.txt"
        self.files["report"].write_text(json.dumps(report), encoding="utf-8")
        args = self._args()
        args.protocol = "chat"
        args.profile = "chat_live"
        args.expected_artifact = "artifacts/chat-live-smoke.txt"
        self.assertEqual(MODULE.verify(args)["protocol"], "chat")


if __name__ == "__main__":
    unittest.main()
