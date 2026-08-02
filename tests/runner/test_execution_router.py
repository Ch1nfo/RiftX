from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from riftx.domain import Execution, ExecutorType
from riftx.runner.remote import NodeExecutionRouter


def _execution(*, node_id: str, executor_type: ExecutorType) -> Execution:
    return Execution(
        id=f"execution-{node_id}-{executor_type.value}",
        execution_key=f"key-{node_id}-{executor_type.value}",
        run_id="run-1",
        node_id=node_id,
        executor_type=executor_type,
        argv=["tool"],
        cwd="/tmp",
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
    )


@dataclass
class _Repository:
    execution: Execution

    async def get(self, execution_id: str) -> Execution | None:
        return self.execution if execution_id == self.execution.id else None


@dataclass
class _Runner:
    calls: list[str] = field(default_factory=list)

    async def cancel(self, execution_id: str) -> Execution:
        self.calls.append(execution_id)
        raise AssertionError("test runner return value must be configured")


@dataclass
class _ReturningRunner(_Runner):
    execution: Execution | None = None

    async def cancel(self, execution_id: str) -> Execution:
        self.calls.append(execution_id)
        assert self.execution is not None
        return self.execution


@dataclass
class _TerminalCloser:
    execution: Execution
    calls: list[str] = field(default_factory=list)

    async def close_execution(self, execution_id: str) -> Execution:
        self.calls.append(execution_id)
        return self.execution


@pytest.mark.parametrize(
    ("node_id", "executor_type", "expected_route"),
    [
        ("local", ExecutorType.PTY, "terminal"),
        ("local", ExecutorType.PROCESS, "local_process"),
        ("local", ExecutorType.SHELL, "local_process"),
        ("remote", ExecutorType.PTY, "remote_execution"),
        ("remote", ExecutorType.PROCESS, "remote_execution"),
    ],
)
async def test_node_execution_router_cancel_is_executor_aware(
    node_id: str,
    executor_type: ExecutorType,
    expected_route: str,
) -> None:
    execution = _execution(node_id=node_id, executor_type=executor_type)
    local = _ReturningRunner(execution=execution)
    remote = _ReturningRunner(execution=execution)
    terminal = _TerminalCloser(execution)
    router = NodeExecutionRouter(
        local_node_id="local",
        repository=_Repository(execution),  # type: ignore[arg-type]
        local=local,  # type: ignore[arg-type]
        remote=remote,  # type: ignore[arg-type]
        local_terminal=terminal,
    )

    assert await router.cancel(execution.id) == execution
    assert {
        "terminal": terminal.calls,
        "local_process": local.calls,
        "remote_execution": remote.calls,
    } == {
        route: [execution.id] if route == expected_route else []
        for route in ("terminal", "local_process", "remote_execution")
    }
