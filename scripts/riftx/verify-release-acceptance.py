#!/usr/bin/env python3
"""Verify that a RiftX 1.0 release acceptance record covers every M8 gate."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

AUTOMATED_LANES = {
    "responsesText": ("mock", "live", "macos", "windows", "linux"),
    "responsesToolLoop": ("mock", "live", "macos", "windows", "linux"),
    "chatText": ("mock", "live", "macos", "windows", "linux"),
    "chatToolLoop": ("mock", "live", "macos", "windows", "linux"),
    "redTeamApproval": ("mock", "macos", "windows", "linux"),
    "pentestApproval": ("mock", "macos", "windows", "linux"),
    "autoMultiTurn": ("mock", "macos", "windows", "linux"),
    "deadlineKill": ("mock", "macos", "windows", "linux"),
    "reportArtifact": ("mock", "macos", "windows", "linux"),
    "profileRollback": ("mock", "macos", "windows", "linux"),
}
HUMAN_SCENARIOS = tuple("ABCDEF")
PLATFORMS = ("macos", "windows", "linux")
RELEASE_CHECKS = (
    "protectedEnvironments",
    "tagAndSourceCommit",
    "macosSigningNotarizationStaple",
    "windowsAuthenticode",
    "cleanInstallMacos",
    "cleanInstallWindows",
    "cleanInstallUbuntu2204",
    "cleanInstallUbuntu2404",
    "upgradeAndRollback",
    "checksumsSbomManifest",
    "liveSecretScan",
    "performanceGate",
    "releaseNotes",
    "defectGate",
)
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[-.][0-9A-Za-z.-]+)?$")


class AcceptanceError(ValueError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def parse_timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise AcceptanceError(f"{field} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AcceptanceError(f"{field} must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None:
        raise AcceptanceError(f"{field} must include a timezone")


def require_string(record: dict[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceError(f"{field} must be a non-empty string")
    return value


def evidence_tag_matches(value: Any, final_tag: str, allow_rc: bool) -> bool:
    if value == final_tag:
        return True
    if not allow_rc or not isinstance(value, str):
        return False
    prefix = f"{final_tag}-rc."
    number = value.removeprefix(prefix)
    return (
        value == f"{prefix}{number}" and number.isdigit() and not number.startswith("0")
    )


def verify_evidence_entry(
    entry: Any,
    tag: str,
    source_commit: str,
    context: str,
    *,
    allow_rc: bool,
) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise AcceptanceError(f"{context} evidence must be an object")
    if entry.get("status") != "passed":
        raise AcceptanceError(f"{context} evidence is not passed")
    if (
        not evidence_tag_matches(entry.get("tag"), tag, allow_rc)
        or entry.get("sourceCommit") != source_commit
    ):
        raise AcceptanceError(f"{context} evidence does not match the release source")
    require_string(entry, "tester")
    require_string(entry, "os")
    evidence = require_string(entry, "evidence")
    if not (evidence.startswith("https://") or evidence.startswith("artifact://")):
        raise AcceptanceError(f"{context} evidence must be an HTTPS or artifact URI")
    parse_timestamp(entry.get("checkedAt"), f"{context}.checkedAt")
    checksum = entry.get("artifactSha256")
    if checksum is not None and (
        not isinstance(checksum, str) or not SHA256_RE.fullmatch(checksum)
    ):
        raise AcceptanceError(f"{context}.artifactSha256 must be a SHA-256 digest")
    return entry


def verify_matrix(
    entries: Any,
    required: set[tuple[str, str]],
    key_fields: tuple[str, str],
    tag: str,
    source_commit: str,
    name: str,
) -> None:
    if not isinstance(entries, list):
        raise AcceptanceError(f"{name} must be an array")
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise AcceptanceError(f"{name} evidence must be an object")
        key = (entry.get(key_fields[0]), entry.get(key_fields[1]))
        if key not in required:
            raise AcceptanceError(f"unexpected {name} evidence key: {key}")
        if key in seen:
            raise AcceptanceError(f"duplicate {name} evidence key: {key}")
        seen.add(key)
        verify_evidence_entry(entry, tag, source_commit, f"{name} {key}", allow_rc=True)
    missing = sorted(required - seen)
    if missing:
        raise AcceptanceError(f"missing {name} evidence: {missing[0]}")


def verify_performance_metrics(entry: dict[str, Any]) -> None:
    metrics = entry.get("metrics")
    if not isinstance(metrics, dict):
        raise AcceptanceError("performanceGate.metrics must be an object")
    requirements = {
        "sampleSeconds": ("minimum", 60),
        "desktopIdleCpuP95Percent": ("maximum", 5.0),
        "daemonIdleCpuP95Percent": ("maximum", 2.0),
        "configuredProfileCount": ("minimum", 16),
        "eagerRuntimeCount": ("maximum", 0),
        "timelineEntryCount": ("minimum", 10_000),
        "timelinePageP95Ms": ("maximum", 250),
        "killStartP95Ms": ("maximum", 2_000),
        "duplicateEventCountAfterReconnect": ("maximum", 0),
        "reportArtifactPayloadBytesRead": ("maximum", 0),
    }
    for name, (bound, threshold) in requirements.items():
        value = metrics.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AcceptanceError(f"performanceGate.metrics.{name} must be numeric")
        if bound == "minimum" and value < threshold:
            raise AcceptanceError(
                f"performanceGate.metrics.{name} must be at least {threshold}"
            )
        if bound == "maximum" and value > threshold:
            raise AcceptanceError(
                f"performanceGate.metrics.{name} must be at most {threshold}"
            )


def verify_defect_metrics(entry: dict[str, Any]) -> None:
    metrics = entry.get("metrics")
    if not isinstance(metrics, dict):
        raise AcceptanceError("defectGate.metrics must be an object")
    zero_fields = (
        "p0Open",
        "p1Open",
        "flakyRequiredChecks",
        "unexplainedMigrationFailures",
        "unexplainedCrossPlatformFailures",
    )
    for name in zero_fields:
        value = metrics.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value != 0:
            raise AcceptanceError(f"defectGate.metrics.{name} must be 0")
    p2_open = metrics.get("p2Open")
    p2_planned = metrics.get("p2WithWorkaroundRiskAndMilestone")
    if (
        isinstance(p2_open, bool)
        or not isinstance(p2_open, int)
        or p2_open < 0
        or p2_planned != p2_open
    ):
        raise AcceptanceError(
            "every open P2 must have a workaround, risk assessment, and 1.0.x milestone"
        )


def verify_release_checks(entries: Any, tag: str, source_commit: str) -> None:
    required = set(RELEASE_CHECKS)
    if not isinstance(entries, list):
        raise AcceptanceError("releaseEvidence must be an array")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise AcceptanceError("release evidence must be an object")
        check = entry.get("check")
        if check not in required:
            raise AcceptanceError(f"unexpected release evidence check: {check}")
        if check in seen:
            raise AcceptanceError(f"duplicate release evidence check: {check}")
        seen.add(check)
        verify_evidence_entry(
            entry,
            tag,
            source_commit,
            f"release check {check}",
            allow_rc=False,
        )
        if check == "performanceGate":
            verify_performance_metrics(entry)
        elif check == "defectGate":
            verify_defect_metrics(entry)
    missing = sorted(required - seen)
    if missing:
        raise AcceptanceError(f"missing release evidence: {missing[0]}")


def verify(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise AcceptanceError("acceptance record must be an object")
    if record.get("schema") != "riftx.releaseAcceptance/v1":
        raise AcceptanceError("unsupported acceptance record schema")
    tag = require_string(record, "tag")
    version = require_string(record, "version")
    source_commit = require_string(record, "sourceCommit")
    if not TAG_RE.fullmatch(tag) or tag != f"v{version}":
        raise AcceptanceError("tag and version do not form one semantic release")
    if not COMMIT_RE.fullmatch(source_commit):
        raise AcceptanceError("sourceCommit must be a full lowercase Git commit")
    if record.get("status") != "approved":
        raise AcceptanceError("acceptance record is not approved")

    automated_required = {
        (scenario, lane)
        for scenario, lanes in AUTOMATED_LANES.items()
        for lane in lanes
    }
    human_required = {
        (scenario, platform) for scenario in HUMAN_SCENARIOS for platform in PLATFORMS
    }
    verify_matrix(
        record.get("automatedEvidence"),
        automated_required,
        ("scenario", "lane"),
        tag,
        source_commit,
        "automatedEvidence",
    )
    verify_matrix(
        record.get("humanEvidence"),
        human_required,
        ("scenario", "platform"),
        tag,
        source_commit,
        "humanEvidence",
    )
    verify_release_checks(record.get("releaseEvidence"), tag, source_commit)

    decision = record.get("decision")
    if not isinstance(decision, dict) or decision.get("outcome") != "go":
        raise AcceptanceError("Go/No-Go decision is not go")
    require_string(decision, "reviewer")
    parse_timestamp(decision.get("reviewedAt"), "decision.reviewedAt")
    return {
        "schema": "riftx.releaseAcceptanceSummary/v1",
        "tag": tag,
        "version": version,
        "sourceCommit": source_commit,
        "automatedEvidenceCount": len(automated_required),
        "humanEvidenceCount": len(human_required),
        "releaseEvidenceCount": len(RELEASE_CHECKS),
        "decision": "go",
    }


def main() -> int:
    args = parse_args()
    try:
        data = args.record.read_bytes()
        record = json.loads(data)
        summary = verify(record)
        summary["recordSha256"] = hashlib.sha256(data).hexdigest()
        rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, json.JSONDecodeError, AcceptanceError) as error:
        print(f"release acceptance validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
