from __future__ import annotations

import pytest

from riftx.domain import (
    RUNNER_COMMAND_PROTOCOLS,
    RunKind,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerEffectBinding,
    RunnerOperationFamily,
    RunnerPrincipal,
    RunnerResourceKind,
    runner_command_payload_binding_invalid_fields,
    runner_command_protocol,
    runner_success_result_invalid_fields,
)

_OWNER = RunnerPrincipal(instance_id="runner-protocol-owner", epoch=1)
_EXECUTION_KEY = "runner-protocol-execution-key"


def test_runner_command_protocol_registry_covers_the_closed_command_enum() -> None:
    assert set(RUNNER_COMMAND_PROTOCOLS) == set(RunnerCommandKind)
    for kind in RunnerCommandKind:
        protocol = runner_command_protocol(kind)
        assert protocol.result_schema.startswith("riftx.runner-result/")
        assert protocol.output_mode in {"none", "command", "execution"}
        assert (protocol.operation_family is RunnerOperationFamily.SAFETY_STOP) is (
            protocol.stop_ack_schema is not None
        )


@pytest.mark.parametrize("kind", list(RunnerCommandKind))
def test_runner_payload_registry_accepts_one_complete_shape_for_every_kind(
    kind: RunnerCommandKind,
) -> None:
    binding = _binding(kind)
    assert runner_command_payload_binding_invalid_fields(
        kind,
        binding,
        _payload(kind),
        authoritative_execution_key=(
            _EXECUTION_KEY if binding.execution_id is not None else None
        ),
    ) == ()


@pytest.mark.parametrize(
    ("kind", "payload", "invalid_field"),
    [
        (RunnerCommandKind.EXECUTE, {"execution_id": "execution-1"}, "request"),
        (
            RunnerCommandKind.TERMINAL_START,
            {"session_id": "terminal-1", "execution_id": "execution-1"},
            "request",
        ),
        (
            RunnerCommandKind.TARGET_HTTP,
            {"run_id": "run-1", "tool_call_ids": ["intent-1"]},
            "launch",
        ),
        (
            RunnerCommandKind.TARGET_HTTP_CANCEL,
            {
                "launch": {
                    "run_id": "run-1",
                    "node_id": "node-1",
                    "tool_call_id": "intent-1",
                }
            },
            "launch",
        ),
        (
            RunnerCommandKind.BROWSER,
            {
                "operation": "close",
                "command": {
                    "session_id": "browser-1",
                    "run_id": "run-1",
                    "node_id": "node-1",
                },
            },
            "operation",
        ),
        (
            RunnerCommandKind.BROWSER_CLOSE,
            {
                "operation": "observe",
                "command": {
                    "session_id": "browser-1",
                    "run_id": "run-1",
                    "node_id": "node-1",
                },
            },
            "operation",
        ),
    ],
)
def test_runner_payload_registry_rejects_cross_kind_shapes(
    kind: RunnerCommandKind,
    payload: dict[str, object],
    invalid_field: str,
) -> None:
    invalid = runner_command_payload_binding_invalid_fields(
        kind,
        _binding(kind),
        payload,
        authoritative_execution_key=(
            _EXECUTION_KEY
            if kind
            in {
                RunnerCommandKind.EXECUTE,
                RunnerCommandKind.CANCEL,
                RunnerCommandKind.TERMINAL_START,
                RunnerCommandKind.TERMINAL_CLOSE,
            }
            else None
        ),
    )
    assert invalid_field in invalid


@pytest.mark.parametrize("kind", list(RunnerCommandKind))
def test_runner_success_result_registry_is_complete_and_accepts_daemon_shape(
    kind: RunnerCommandKind,
) -> None:
    binding = _binding(kind)
    payload = _payload(kind)
    assert runner_success_result_invalid_fields(kind, binding, payload, {})
    assert runner_success_result_invalid_fields(
        kind,
        binding,
        payload,
        _result(kind),
    ) == ()


def _binding(kind: RunnerCommandKind) -> RunnerEffectBinding:
    protocol = runner_command_protocol(kind)
    resource_ids = {
        RunnerResourceKind.EXECUTION: "execution-1",
        RunnerResourceKind.TERMINAL_SESSION: "terminal-1",
        RunnerResourceKind.TARGET_HTTP_INTENT: "intent-1",
        RunnerResourceKind.BROWSER_SESSION: "browser-1",
    }
    execution_id = (
        "execution-1"
        if protocol.resource_kind
        in {RunnerResourceKind.EXECUTION, RunnerResourceKind.TERMINAL_SESSION}
        else None
    )
    return RunnerEffectBinding(
        id=f"binding-{kind.value}",
        run_id="run-1",
        run_kind=RunKind.GENERAL,
        node_id="node-1",
        target=_OWNER,
        origin=RunnerCommandOrigin.APPLICATION_SERVICE,
        operation_family=protocol.operation_family,
        execution_id=execution_id,
        resource_kind=protocol.resource_kind,
        resource_id=resource_ids[protocol.resource_kind],
    )


def _payload(kind: RunnerCommandKind) -> dict[str, object]:
    if kind is RunnerCommandKind.EXECUTE:
        return {
            "execution_id": "execution-1",
            "request": {
                "run_id": "run-1",
                "node_id": "node-1",
                "execution_id": "execution-1",
                "execution_key": _EXECUTION_KEY,
                "runner_principal": _OWNER.model_dump(mode="json"),
            },
        }
    if kind is RunnerCommandKind.CANCEL:
        return {
            "execution_id": "execution-1",
            "execution_key": _EXECUTION_KEY,
        }
    if kind is RunnerCommandKind.TARGET_HTTP:
        return {
            "launch": {
                "run_id": "run-1",
                "session_id": "session-1",
                "tool_call_id": "intent-1",
                "node_id": "node-1",
                "scope": {},
                "request": {"execution_key": "target-http-key"},
            }
        }
    if kind is RunnerCommandKind.TARGET_HTTP_CANCEL:
        return {"run_id": "run-1", "tool_call_ids": ["intent-1"]}
    if kind in {RunnerCommandKind.BROWSER, RunnerCommandKind.BROWSER_CLOSE}:
        return {
            "operation": (
                "close" if kind is RunnerCommandKind.BROWSER_CLOSE else "observe"
            ),
            "command": {
                "session_id": "browser-1",
                "run_id": "run-1",
                "node_id": "node-1",
            },
        }
    if kind is RunnerCommandKind.TERMINAL_START:
        return {
            "session_id": "terminal-1",
            "execution_id": "execution-1",
            "request": {
                "run_id": "run-1",
                "node_id": "node-1",
                "session_id": "terminal-1",
                "execution_id": "execution-1",
                "execution_key": _EXECUTION_KEY,
                "runner_principal": _OWNER.model_dump(mode="json"),
            },
        }
    base: dict[str, object] = {
        "session_id": "terminal-1",
        "execution_id": "execution-1",
    }
    if kind is RunnerCommandKind.TERMINAL_CLOSE:
        return {**base, "execution_key": _EXECUTION_KEY}
    base["operation_id"] = f"operation-{kind.value}"
    if kind is RunnerCommandKind.TERMINAL_WRITE:
        base["data"] = "eA=="
    elif kind is RunnerCommandKind.TERMINAL_RESIZE:
        base.update({"cols": 120, "rows": 40})
    return base


def _result(kind: RunnerCommandKind) -> dict[str, object]:
    if kind is RunnerCommandKind.EXECUTE:
        return {
            "execution_id": "execution-1",
            "local_execution_id": "execution-1",
            "execution_key": _EXECUTION_KEY,
            "owner": _OWNER.model_dump(mode="json"),
            "status": "running",
        }
    if kind in {RunnerCommandKind.CANCEL, RunnerCommandKind.TERMINAL_CLOSE}:
        result: dict[str, object] = {
            "execution_id": "execution-1",
            "local_execution_id": "execution-1",
            "execution_key": _EXECUTION_KEY,
            "owner": _OWNER.model_dump(mode="json"),
            "status": "cancelled",
            "physical_stop_confirmed": True,
        }
        if kind is RunnerCommandKind.TERMINAL_CLOSE:
            result["session_id"] = "terminal-1"
        return result
    if kind is RunnerCommandKind.TARGET_HTTP:
        return {
            "result": {
                "request_id": "request-1",
                "execution_key": "target-http-key",
                "request_hash": "a" * 64,
                "status_code": 200,
                "elapsed_ms": 1,
                "final_url": "https://target.invalid/",
                "truncated": False,
            }
        }
    if kind is RunnerCommandKind.TARGET_HTTP_CANCEL:
        return {
            "outcomes": [
                {"tool_call_id": "intent-1", "confirmed": True, "reason": "stopped"}
            ]
        }
    if kind in {RunnerCommandKind.BROWSER, RunnerCommandKind.BROWSER_CLOSE}:
        return {
            "result": {
                "session": {
                    "id": "browser-1",
                    "run_id": "run-1",
                    "node_id": "node-1",
                    "status": (
                        "closed"
                        if kind is RunnerCommandKind.BROWSER_CLOSE
                        else "active"
                    ),
                }
            }
        }
    if kind is RunnerCommandKind.TERMINAL_START:
        return {
            "result": {
                "session_id": "terminal-1",
                "execution_id": "execution-1",
                "status": "running",
                "duplicate": False,
            }
        }
    operation_result: dict[str, object] = {
        "session_id": "terminal-1",
        "operation_id": f"operation-{kind.value}",
        "duplicate": False,
    }
    if kind is RunnerCommandKind.TERMINAL_WRITE:
        operation_result["bytes_written"] = 1
    elif kind is RunnerCommandKind.TERMINAL_RESIZE:
        operation_result.update({"cols": 120, "rows": 40})
    else:
        operation_result["interrupted"] = True
    return {"result": operation_result}
