"""Pure durable identity checks shared by terminal admission paths."""

from __future__ import annotations

from pathlib import Path

from riftx.domain import Execution, ExecutorType, TerminalSession

from .models import TerminalLaunchRequest


def legacy_terminal_launch_identity_mismatches(
    terminal: TerminalSession,
    request: TerminalLaunchRequest,
) -> tuple[str, ...]:
    """Return reconstructable launch fields that differ for a legacy Terminal."""

    fields: tuple[tuple[str, object, object], ...] = (
        ("terminal_owner", terminal.owner, request.owner),
        ("terminal_cols", terminal.cols, request.cols),
        ("terminal_rows", terminal.rows, request.rows),
    )
    return tuple(
        field_name for field_name, persisted, requested in fields if persisted != requested
    )


def require_terminal_start_replay_matches(
    terminal: TerminalSession,
    execution: Execution,
    request: TerminalLaunchRequest,
) -> None:
    """Reject a Runner-side duplicate unless its complete durable launch matches."""

    if request.session_id is None or request.execution_id is None:
        raise ValueError("terminal_start replay identity is incomplete")
    expected_execution_key = request.execution_key or f"terminal:{request.session_id}"
    execution_fields: tuple[tuple[str, object, object], ...] = (
        ("execution_id", execution.id, request.execution_id),
        ("execution_key", execution.execution_key, expected_execution_key),
        ("run_id", execution.run_id, request.run_id),
        ("agent_session_id", execution.session_id, request.agent_session_id),
        ("tool_call_id", execution.tool_call_id, request.tool_call_id),
        ("attempt_group", execution.attempt_group, request.attempt_group),
        ("node_id", execution.node_id, request.node_id),
        ("runner_principal", execution.owner, request.runner_principal),
        ("executor_type", execution.executor_type, ExecutorType.PTY),
        ("argv", execution.argv, request.argv),
        ("command_text", execution.command_text, None),
        ("tool_id", execution.tool_id, request.tool_id),
        ("tool_version", execution.tool_version, request.tool_version),
        ("cwd", _canonical_path(execution.cwd), _canonical_path(request.cwd)),
        ("env", execution.env_diff, request.env),
    )
    if request.runner_command_id is not None:
        execution_fields += (
            ("runner_command_id", execution.runner_command_id, request.runner_command_id),
            (
                "runner_effect_binding_id",
                execution.runner_effect_binding_id,
                request.runner_effect_binding_id,
            ),
            (
                "runner_binding_digest",
                execution.runner_binding_digest,
                request.runner_binding_digest,
            ),
            (
                "runner_envelope_digest",
                execution.runner_envelope_digest,
                request.runner_envelope_digest,
            ),
        )
    terminal_fields: tuple[tuple[str, object, object], ...] = (
        ("terminal_session_id", terminal.id, request.session_id),
        ("terminal_execution_id", terminal.execution_id, request.execution_id),
        ("terminal_run_id", terminal.run_id, request.run_id),
        ("terminal_runner_id", terminal.runner_id, request.node_id),
        ("terminal_shell", terminal.shell, request.argv[0]),
        ("terminal_cwd", _canonical_path(terminal.cwd), _canonical_path(request.cwd)),
    )
    mismatched = [
        field_name
        for field_name, persisted, requested in (*execution_fields, *terminal_fields)
        if persisted != requested
    ]
    if execution.launch_fingerprint is None:
        # A current fingerprint proves immutable launch-time dimensions and
        # owner even after resize/takeover mutates the Terminal projection.
        # Legacy rows have no such proof, so reconstructable fields must agree.
        mismatched.extend(legacy_terminal_launch_identity_mismatches(terminal, request))
    elif execution.launch_fingerprint != request.launch_fingerprint:
        mismatched.append("launch_fingerprint")
    if not mismatched:
        return
    raise ValueError(
        "terminal_start conflicts with an existing durable admission: "
        + ", ".join(sorted(set(mismatched)))
    )


def _canonical_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())
