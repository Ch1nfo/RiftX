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


def release_evidence(check: str) -> dict[str, object]:
    item: dict[str, object] = evidence(check=check)
    if check == "performanceGate":
        item["metrics"] = {
            "sampleSeconds": 60,
            "desktopIdleCpuP95Percent": 4.5,
            "daemonIdleCpuP95Percent": 1.5,
            "configuredProfileCount": 16,
            "eagerRuntimeCount": 0,
            "timelineEntryCount": 10_000,
            "timelinePageP95Ms": 200,
            "killStartP95Ms": 1_500,
            "duplicateEventCountAfterReconnect": 0,
            "reportArtifactPayloadBytesRead": 0,
        }
    elif check == "defectGate":
        item["metrics"] = {
            "p0Open": 0,
            "p1Open": 0,
            "p2Open": 1,
            "p2WithWorkaroundRiskAndMilestone": 1,
            "flakyRequiredChecks": 0,
            "unexplainedMigrationFailures": 0,
            "unexplainedCrossPlatformFailures": 0,
        }
    return item


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
        "releaseEvidence": [release_evidence(check) for check in MODULE.RELEASE_CHECKS],
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
        self.assertEqual(summary["releaseEvidenceCount"], 14)
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

    def test_rejects_performance_threshold_failure(self) -> None:
        record = valid_record()
        performance = next(
            item
            for item in record["releaseEvidence"]
            if item["check"] == "performanceGate"
        )
        performance["metrics"]["killStartP95Ms"] = 2_001
        with self.assertRaisesRegex(MODULE.AcceptanceError, "at most 2000"):
            MODULE.verify(record)

    def test_rejects_unplanned_p2_or_required_check_flake(self) -> None:
        record = valid_record()
        defects = next(
            item for item in record["releaseEvidence"] if item["check"] == "defectGate"
        )
        defects["metrics"]["p2WithWorkaroundRiskAndMilestone"] = 0
        with self.assertRaisesRegex(MODULE.AcceptanceError, "every open P2"):
            MODULE.verify(record)
        defects["metrics"]["p2WithWorkaroundRiskAndMilestone"] = 1
        defects["metrics"]["flakyRequiredChecks"] = 1
        with self.assertRaisesRegex(MODULE.AcceptanceError, "must be 0"):
            MODULE.verify(record)

    def test_rejects_non_https_evidence(self) -> None:
        record = valid_record()
        record["releaseEvidence"][0]["evidence"] = "/tmp/local-only.txt"
        with self.assertRaisesRegex(MODULE.AcceptanceError, "HTTPS or artifact URI"):
            MODULE.verify(record)


if __name__ == "__main__":
    unittest.main()
