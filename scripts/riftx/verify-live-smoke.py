#!/usr/bin/env python3
"""Verify one protected live provider capability probe and Runtime tool loop."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROTOCOL_CONTRACTS = {
    "responses": ({"responses"}, {"responses"}),
    "chat": ({"chat_completions", "chatCompletions"}, {"chatCompletions", "chat_completions"}),
}


class VerificationError(ValueError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", choices=sorted(PROTOCOL_CONTRACTS), required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--capability", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--conversation", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--expected-marker", required=True)
    parser.add_argument("--expected-artifact", required=True)
    parser.add_argument("--scan-file", action="append", type=Path, default=[])
    parser.add_argument("--secret-env", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise VerificationError(f"cannot decode JSON from {path}: {error}") from error


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise VerificationError(f"cannot read event stream {path}: {error}") from error
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise VerificationError(
                f"invalid event JSON at {path}:{line_number}: {error}"
            ) from error
        if not isinstance(event, dict):
            raise VerificationError(f"event at {path}:{line_number} is not an object")
        events.append(event)
    if not events:
        raise VerificationError("event stream is empty")
    return events


def verify_capability(
    capability: Any, protocol: str, profile: str
) -> dict[str, str]:
    if not isinstance(capability, dict) or capability.get("ok") is not True:
        raise VerificationError("provider capability probe did not succeed")
    if capability.get("profileName") != profile:
        raise VerificationError("capability probe used an unexpected profile")
    accepted, _ = PROTOCOL_CONTRACTS[protocol]
    if capability.get("protocol") not in accepted:
        raise VerificationError("capability probe reported an unexpected protocol")
    matrix = capability.get("capabilities")
    if not isinstance(matrix, dict):
        raise VerificationError("capability matrix is missing")
    statuses: dict[str, str] = {}
    for layer in ("config", "streamText", "functionTools"):
        check = matrix.get(layer)
        if not isinstance(check, dict) or check.get("status") != "passed":
            raise VerificationError(f"provider capability {layer} did not pass")
        statuses[layer] = "passed"
    return statuses


def verify_tool_loop(
    report: Any,
    conversation: Any,
    events: list[dict[str, Any]],
    protocol: str,
    profile: str,
    expected_marker: str,
    expected_artifact: str,
) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise VerificationError("report is not an object")
    report_profile = report.get("llmProfile")
    if not isinstance(report_profile, dict) or report_profile.get("name") != profile:
        raise VerificationError("report used an unexpected LLM profile")
    _, accepted_report_protocols = PROTOCOL_CONTRACTS[protocol]
    if report_profile.get("protocol") not in accepted_report_protocols:
        raise VerificationError("report recorded an unexpected LLM protocol")

    executions = report.get("executions")
    if not isinstance(executions, list):
        raise VerificationError("report execution list is missing")
    completed = [
        execution
        for execution in executions
        if isinstance(execution, dict)
        and execution.get("status") == "completed"
        and execution.get("exitCode") == 0
        and isinstance(execution.get("id"), str)
        and isinstance(execution.get("turnId"), str)
        and isinstance(execution.get("stdoutBytes"), int)
        and execution["stdoutBytes"] > 0
    ]
    if len(completed) != 1:
        raise VerificationError("tool loop must contain exactly one successful execution")
    execution = completed[0]

    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        raise VerificationError("report artifact list is missing")
    matching_artifacts = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict)
        and artifact.get("path") == expected_artifact
        and artifact.get("executionId") == execution["id"]
        and isinstance(artifact.get("id"), str)
        and isinstance(artifact.get("sizeBytes"), int)
        and artifact["sizeBytes"] > 0
        and isinstance(artifact.get("sha256"), str)
        and SHA256_RE.fullmatch(artifact["sha256"])
    ]
    if len(matching_artifacts) != 1:
        raise VerificationError("expected execution-bound artifact is missing")
    artifact = matching_artifacts[0]

    if not isinstance(conversation, dict) or not isinstance(conversation.get("data"), list):
        raise VerificationError("conversation page is missing")
    final_messages = [
        entry
        for entry in conversation["data"]
        if isinstance(entry, dict)
        and entry.get("role") == "agent"
        and entry.get("kind") == "message"
        and entry.get("turnId") == execution["turnId"]
        and expected_marker in str(entry.get("text", ""))
    ]
    if not final_messages:
        raise VerificationError("final agent marker is missing from the execution turn")

    event_names = [event.get("event") for event in events if isinstance(event.get("event"), str)]
    if "turn/completed" not in event_names:
        raise VerificationError("event stream did not observe turn/completed")
    event_turn_ids = {
        event.get("data", {}).get("turnId")
        for event in events
        if event.get("event") == "turn/completed" and isinstance(event.get("data"), dict)
    }
    if event_turn_ids != {execution["turnId"]}:
        raise VerificationError("turn/completed does not match the executed turn")

    return {
        "turnId": execution["turnId"],
        "executionId": execution["id"],
        "artifactId": artifact["id"],
        "artifactSha256": artifact["sha256"],
        "finalMarkerObserved": True,
        "turnCompletedObserved": True,
    }


def verify_secrets(paths: list[Path], environment_names: list[str]) -> None:
    secrets = [(name, os.environ.get(name, "").encode()) for name in environment_names]
    missing = [name for name, value in secrets if not value]
    if missing:
        raise VerificationError(f"protected secret environment variable is empty: {missing[0]}")
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError as error:
            raise VerificationError(f"cannot scan {path}: {error}") from error
        for name, value in secrets:
            if value in data:
                raise VerificationError(f"protected secret {name} appeared in {path}")


def verify(args: argparse.Namespace) -> dict[str, Any]:
    capability = load_json(args.capability)
    report = load_json(args.report)
    conversation = load_json(args.conversation)
    events = load_events(args.events)
    statuses = verify_capability(capability, args.protocol, args.profile)
    tool_loop = verify_tool_loop(
        report,
        conversation,
        events,
        args.protocol,
        args.profile,
        args.expected_marker,
        args.expected_artifact,
    )
    verify_secrets(
        list(dict.fromkeys([
            args.capability,
            args.report,
            args.conversation,
            args.events,
            *args.scan_file,
        ])),
        args.secret_env,
    )
    return {
        "schema": "riftxLiveSmokeSummaryV1",
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "protocol": args.protocol,
        "profile": args.profile,
        "capabilities": statuses,
        "toolLoop": tool_loop,
    }


def main() -> int:
    args = parse_args()
    try:
        summary = verify(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (VerificationError, OSError) as error:
        print(f"live smoke validation failed: {error}", file=sys.stderr)
        return 1
    print(f"live smoke validation passed for {args.protocol}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
