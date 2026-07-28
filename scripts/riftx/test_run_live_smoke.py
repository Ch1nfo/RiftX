import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).with_name("run-live-smoke.py")
SPEC = importlib.util.spec_from_file_location("run_live_smoke", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RunLiveSmokeTests(unittest.TestCase):
    def test_expected_command_is_local_and_protocol_scoped(self) -> None:
        command = MODULE.expected_command(MODULE.CASE_CONTRACTS["responses"])
        self.assertEqual(
            command,
            "mkdir -p artifacts && printf '%s' 'RIFTX_RESPONSES_ARTIFACT_OK' > "
            "artifacts/responses-live-smoke.txt && printf '%s\\n' "
            "'RIFTX_RESPONSES_TOOL_STDOUT_OK'",
        )
        self.assertNotIn("curl", command)
        self.assertNotIn("sudo", command)

    def test_approval_requires_exact_bound_command(self) -> None:
        command = MODULE.expected_command(MODULE.CASE_CONTRACTS["chat"])
        approval = {
            "id": "approval-1",
            "engagementId": "eng-1",
            "kind": "command",
            "command": command,
            "executionIntent": {
                "engagementId": "eng-1",
                "mode": "pentest",
                "threadId": "thread-1",
                "turnId": "turn-1",
                "toolCallId": "call-1",
                "bindingSha256": "a" * 64,
                "commandSha256": MODULE.command_digest(command),
                "displayArgv": ["/bin/sh", "-lc", command],
            },
        }
        self.assertTrue(MODULE.approval_matches(approval, "eng-1", command))
        approval["command"] = "curl https://example.invalid"
        approval["executionIntent"]["displayArgv"] = ["curl", "https://example.invalid"]
        self.assertFalse(MODULE.approval_matches(approval, "eng-1", command))

    def test_completion_requires_execution_bound_artifact(self) -> None:
        report = {
            "executions": [{"id": "exec-1", "status": "completed", "exitCode": 0}],
            "artifacts": [
                {
                    "executionId": "exec-1",
                    "path": "artifacts/responses-live-smoke.txt",
                }
            ],
        }
        self.assertTrue(
            MODULE.has_completed_execution(report, "artifacts/responses-live-smoke.txt")
        )
        report["artifacts"][0]["executionId"] = "exec-other"
        self.assertFalse(
            MODULE.has_completed_execution(report, "artifacts/responses-live-smoke.txt")
        )


if __name__ == "__main__":
    unittest.main()
