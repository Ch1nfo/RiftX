#!/usr/bin/env python3
"""Run one guarded RiftX live Runtime tool loop against an active daemon."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from typing import Any

CASE_CONTRACTS = {
    "responses": {
        "final_marker": "RIFTX_RESPONSES_FINAL_OK",
        "artifact_marker": "RIFTX_RESPONSES_ARTIFACT_OK",
        "stdout_marker": "RIFTX_RESPONSES_TOOL_STDOUT_OK",
        "artifact": "artifacts/responses-live-smoke.txt",
    },
    "chat": {
        "final_marker": "RIFTX_CHAT_FINAL_OK",
        "artifact_marker": "RIFTX_CHAT_ARTIFACT_OK",
        "stdout_marker": "RIFTX_CHAT_TOOL_STDOUT_OK",
        "artifact": "artifacts/chat-live-smoke.txt",
    },
}


class SmokeError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", choices=sorted(CASE_CONTRACTS), required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--riftx", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ipc-socket", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=360)
    return parser.parse_args()


def run_command(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise SmokeError(
            f"command failed with exit {result.returncode}: {command[0]}: {result.stderr.strip()}"
        )
    return result


def run_cli_json(args: argparse.Namespace, *command: str, timeout: int = 30) -> Any:
    result = run_command(
        [str(args.riftx), "--config", str(args.config), *command], timeout=timeout
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SmokeError(f"RiftX CLI returned invalid JSON: {error}") from error


def expected_command(contract: dict[str, str]) -> str:
    return (
        "mkdir -p artifacts && "
        f"printf '%s' '{contract['artifact_marker']}' > {contract['artifact']} && "
        f"printf '%s\\n' '{contract['stdout_marker']}'"
    )


def command_digest(command: str) -> str:
    hasher = hashlib.sha256()
    for argument in shlex.split(command):
        encoded = argument.encode()
        hasher.update(len(encoded).to_bytes(8, byteorder="little"))
        hasher.update(encoded)
    return hasher.hexdigest()


def build_prompt(command: str, final_marker: str) -> str:
    return (
        "This is an authorized RiftX protected live smoke in an isolated Lab workspace. "
        "You MUST call the exec_command function exactly once; do not merely describe or quote it. "
        f"Use this exact cmd string with no additions or substitutions: {command}\n"
        "After the tool result is returned, send one final concise assistant message containing "
        f"the exact marker {final_marker}. Do not call any other tool."
    )


def create_engagement(args: argparse.Namespace, contract: dict[str, str]) -> dict[str, Any]:
    expires_at = int(time.time()) + max(args.timeout_seconds * 2, 1_800)
    engagement = run_cli_json(
        args,
        "engagements",
        "create",
        "--name",
        f"{args.protocol.title()} protected live tool loop",
        "--objective",
        "Execute one harmless local workspace marker command and summarize its result",
        "--success-criterion",
        f"Capture {contract['artifact']} and emit the final marker",
        "--entry-point",
        "127.0.0.1",
        "--cidr",
        "127.0.0.0/8",
        "--mode",
        "pentest",
        "--llm-profile",
        args.profile,
        "--environment",
        "lab",
        "--capability",
        "evidence.capture",
        "--expires-at",
        str(expires_at),
        "--json",
    )
    if not isinstance(engagement, dict) or not isinstance(engagement.get("id"), str):
        raise SmokeError("engagement create did not return an ID")
    if engagement.get("llmProfile") != args.profile:
        raise SmokeError("engagement create selected an unexpected LLM profile")
    return engagement


def approval_matches(
    approval: dict[str, Any], engagement_id: str, command: str
) -> bool:
    if approval.get("kind") != "command" or approval.get("engagementId") != engagement_id:
        return False
    intent = approval.get("executionIntent")
    if not isinstance(intent, dict):
        return False
    display_argv = intent.get("displayArgv")
    return (
        intent.get("engagementId") == engagement_id
        and intent.get("mode") == "pentest"
        and isinstance(intent.get("threadId"), str)
        and isinstance(intent.get("turnId"), str)
        and isinstance(intent.get("toolCallId"), str)
        and isinstance(intent.get("bindingSha256"), str)
        and len(intent["bindingSha256"]) == 64
        and intent.get("commandSha256") == command_digest(command)
        and isinstance(display_argv, list)
        and all(isinstance(value, str) for value in display_argv)
        and approval.get("command") == command
    )


def decide_approvals(
    args: argparse.Namespace, engagement_id: str, command: str
) -> None:
    approvals = run_cli_json(
        args, "approvals", "list", engagement_id, "--json", timeout=15
    )
    if not isinstance(approvals, list):
        raise SmokeError("pending approval response is not a list")
    for approval in approvals:
        if not isinstance(approval, dict) or not isinstance(approval.get("id"), str):
            raise SmokeError("pending approval is malformed")
        decision = "approve" if approval_matches(approval, engagement_id, command) else "deny"
        run_cli_json(
            args,
            "approvals",
            "decide",
            approval["id"],
            decision,
            "--json",
            timeout=15,
        )
        if decision == "deny":
            raise SmokeError("model requested a command outside the guarded live-smoke contract")


def fetch_conversation(args: argparse.Namespace, engagement_id: str) -> dict[str, Any]:
    result = run_command(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--unix-socket",
            str(args.ipc_socket),
            f"http://riftxd.local/v1/engagements/{engagement_id}/conversation?limit=200",
        ],
        timeout=15,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SmokeError(f"conversation endpoint returned invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise SmokeError("conversation endpoint did not return an object")
    return value


def has_completed_execution(report: Any, artifact_path: str) -> bool:
    if not isinstance(report, dict):
        return False
    executions = report.get("executions")
    artifacts = report.get("artifacts")
    if not isinstance(executions, list) or not isinstance(artifacts, list):
        return False
    completed_ids = {
        execution.get("id")
        for execution in executions
        if isinstance(execution, dict)
        and execution.get("status") == "completed"
        and execution.get("exitCode") == 0
    }
    return any(
        isinstance(artifact, dict)
        and artifact.get("path") == artifact_path
        and artifact.get("executionId") in completed_ids
        for artifact in artifacts
    )


def has_final_marker(conversation: Any, marker: str) -> bool:
    return isinstance(conversation, dict) and isinstance(conversation.get("data"), list) and any(
        isinstance(entry, dict)
        and entry.get("role") == "agent"
        and entry.get("kind") == "message"
        and marker in str(entry.get("text", ""))
        for entry in conversation["data"]
    )


def event_completed(path: Path) -> bool:
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "turn/completed":
            return True
    return False


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_smoke(args: argparse.Namespace) -> None:
    if args.timeout_seconds < 30:
        raise SmokeError("timeout must be at least 30 seconds")
    contract = CASE_CONTRACTS[args.protocol]
    command = expected_command(contract)
    prompt = build_prompt(command, contract["final_marker"])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.output_dir / args.protocol
    events_path = Path(f"{prefix}-events.ndjson")
    event_errors_path = Path(f"{prefix}-events.stderr.log")
    report_path = Path(f"{prefix}-report.json")
    conversation_path = Path(f"{prefix}-conversation.json")

    engagement = create_engagement(args, contract)
    engagement_id = engagement["id"]
    run_cli_json(args, "engagements", "activate", engagement_id, "--json")

    with events_path.open("w", encoding="utf-8") as events_file, event_errors_path.open(
        "w", encoding="utf-8"
    ) as event_errors:
        event_process = subprocess.Popen(
            [
                str(args.riftx),
                "--config",
                str(args.config),
                "events",
                engagement_id,
                "--json",
            ],
            stdout=events_file,
            stderr=event_errors,
            text=True,
            start_new_session=True,
        )
        try:
            time.sleep(1)
            run_cli_json(args, "turn", engagement_id, prompt, "--json")
            deadline = time.monotonic() + args.timeout_seconds
            last_report: Any = None
            last_conversation: Any = None
            while time.monotonic() < deadline:
                if event_process.poll() is not None:
                    raise SmokeError("event subscription exited before tool-loop completion")
                decide_approvals(args, engagement_id, command)
                last_report = run_cli_json(
                    args, "report", engagement_id, "--format", "json", timeout=15
                )
                last_conversation = fetch_conversation(args, engagement_id)
                write_json(report_path, last_report)
                write_json(conversation_path, last_conversation)
                if has_completed_execution(last_report, contract["artifact"]) and has_final_marker(
                    last_conversation, contract["final_marker"]
                ):
                    for _ in range(20):
                        events_file.flush()
                        if event_completed(events_path):
                            return
                        time.sleep(0.25)
                    raise SmokeError("turn completed without a matching streamed completion event")
                time.sleep(1)
            raise SmokeError("live Runtime tool loop timed out")
        finally:
            if event_process.poll() is None:
                try:
                    os.killpg(event_process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    event_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(event_process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    event_process.wait(timeout=5)


def main() -> int:
    args = parse_args()
    try:
        run_smoke(args)
    except (SmokeError, OSError, subprocess.SubprocessError) as error:
        print(f"live smoke execution failed: {error}", file=sys.stderr)
        return 1
    print(f"live Runtime tool loop completed for {args.protocol}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
