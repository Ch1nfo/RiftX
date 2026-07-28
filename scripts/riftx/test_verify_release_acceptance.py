import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).with_name("verify-release-acceptance.py")
SPEC = importlib.util.spec_from_file_location("verify_release_acceptance", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

TAG = "v1.0.0"
COMMIT = "a" * 40


def evidence(**fields: str) -> dict[str, str]:
    return {
        **fields,
        "status": "passed",
        "tag": TAG,
        "sourceCommit": COMMIT,
        "tester": "release-reviewer",
        "os": "applicable platform",
        "checkedAt": "2026-07-28T12:00:00+08:00",
        "evidence": "https://github.com/example/runs/1",
    }


def valid_record() -> dict[str, object]:
    return {
        "schema": "riftx.releaseAcceptance/v1",
        "tag": TAG,
        "version": "1.0.0",
        "sourceCommit": COMMIT,
        "status": "approved",
        "automatedEvidence": [
            evidence(scenario=scenario, lane=lane)
            for scenario, lanes in MODULE.AUTOMATED_LANES.items()
            for lane in lanes
        ],
        "humanEvidence": [
            evidence(scenario=scenario, platform=platform)
            for scenario in MODULE.HUMAN_SCENARIOS
            for platform in MODULE.PLATFORMS
        ],
        "releaseEvidence": [evidence(check=check) for check in MODULE.RELEASE_CHECKS],
        "decision": {
            "outcome": "go",
            "reviewer": "release-owner",
            "reviewedAt": "2026-07-28T13:00:00+08:00",
        },
    }


class VerifyReleaseAcceptanceTests(unittest.TestCase):
    def test_accepts_complete_m8_evidence(self) -> None:
        summary = MODULE.verify(valid_record())
        self.assertEqual(summary["automatedEvidenceCount"], 44)
        self.assertEqual(summary["humanEvidenceCount"], 18)
        self.assertEqual(summary["releaseEvidenceCount"], 13)
        self.assertEqual(summary["decision"], "go")

    def test_rejects_missing_live_tool_loop(self) -> None:
        record = valid_record()
        record["automatedEvidence"] = [
            item
            for item in record["automatedEvidence"]
            if not (item["scenario"] == "chatToolLoop" and item["lane"] == "live")
        ]
        with self.assertRaisesRegex(
            MODULE.AcceptanceError, "missing automatedEvidence"
        ):
            MODULE.verify(record)

    def test_rejects_evidence_from_another_commit(self) -> None:
        record = valid_record()
        record["humanEvidence"][0]["sourceCommit"] = "b" * 40
        with self.assertRaisesRegex(MODULE.AcceptanceError, "release source"):
            MODULE.verify(record)

    def test_rejects_pending_or_no_go_record(self) -> None:
        record = valid_record()
        record["status"] = "pending"
        with self.assertRaisesRegex(MODULE.AcceptanceError, "not approved"):
            MODULE.verify(record)
        record["status"] = "approved"
        record["decision"]["outcome"] = "no-go"
        with self.assertRaisesRegex(MODULE.AcceptanceError, "not go"):
            MODULE.verify(record)

    def test_rejects_non_https_evidence(self) -> None:
        record = valid_record()
        record["releaseEvidence"][0]["evidence"] = "/tmp/local-only.txt"
        with self.assertRaisesRegex(MODULE.AcceptanceError, "HTTPS or artifact URI"):
            MODULE.verify(record)


if __name__ == "__main__":
    unittest.main()
