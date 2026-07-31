from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime

import pytest

from riftx.domain import Execution, ExecutorType
from riftx.runner.process_inspector import (
    ProcessIdentity,
    ProcessInspector,
    _argv_matches,
    _read_posix_command,
)


class StaticInspector(ProcessInspector):
    def __init__(self, identity: ProcessIdentity) -> None:
        self._identity = identity

    async def inspect(self, pid: int) -> ProcessIdentity | None:
        assert pid == self._identity.pid
        return self._identity


def shell_execution(created_at: datetime) -> Execution:
    return Execution(
        id="shell-exec",
        execution_key="shell-exec-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.SHELL,
        argv=["/bin/zsh", "-lc", "sleep 120"],
        command_text="sleep 120",
        cwd="/tmp",
        stdout_path="/tmp/shell-exec.stdout",
        stderr_path="/tmp/shell-exec.stderr",
        pid=8123,
        process_group_id=8123,
        process_created_at=created_at,
    )


def test_process_inspector_treats_unavailable_ps_as_unknown(monkeypatch) -> None:
    def denied(*_: object, **__: object) -> object:
        raise PermissionError("ps denied")

    monkeypatch.setattr(subprocess, "run", denied)
    assert _read_posix_command(1234) is None


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        (["/bin/sleep", "1"], "/bin/sleep 10"),
        (["tool", "--safe"], "/usr/bin/tool --safest"),
        (["tool", "one"], "/usr/bin/tool one extra"),
    ],
)
def test_direct_process_identity_requires_exact_argv_tokens(
    expected: list[str],
    actual: str,
) -> None:
    assert _argv_matches(expected, actual) is False


def test_direct_process_identity_accepts_exact_quoted_argv_tokens() -> None:
    assert _argv_matches(
        ["/usr/bin/tool", "argument with spaces", "--safe"],
        "/opt/tools/tool 'argument with spaces' --safe",
    )


async def test_shell_exec_replacement_matches_same_creation_and_process_group() -> None:
    created_at = datetime.now(UTC)
    execution = shell_execution(created_at)
    inspector = StaticInspector(
        ProcessIdentity(
            pid=execution.pid or 0,
            created_at=created_at,
            command="/bin/sleep 120",
            process_group_id=execution.process_group_id,
        )
    )

    assert await inspector.matches(execution) is True


async def test_shell_exec_replacement_rejects_mismatched_process_group() -> None:
    created_at = datetime.now(UTC)
    execution = shell_execution(created_at)
    inspector = StaticInspector(
        ProcessIdentity(
            pid=execution.pid or 0,
            created_at=created_at,
            command="/bin/sleep 120",
            process_group_id=execution.process_group_id + 1,
        )
    )

    assert await inspector.matches(execution) is False


async def test_shell_exec_replacement_requires_exact_arguments() -> None:
    created_at = datetime.now(UTC)
    execution = shell_execution(created_at)
    inspector = StaticInspector(
        ProcessIdentity(
            pid=execution.pid or 0,
            created_at=created_at,
            command="/bin/sleep 1200",
            process_group_id=execution.process_group_id,
        )
    )

    assert await inspector.matches(execution) is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX identity fields are required")
async def test_posix_unknown_identity_fields_fail_closed() -> None:
    created_at = datetime.now(UTC)
    execution = shell_execution(created_at)
    inspector = StaticInspector(
        ProcessIdentity(
            pid=execution.pid or 0,
            created_at=None,
            command=None,
            process_group_id=None,
        )
    )

    assert await inspector.matches(execution) is False


@pytest.mark.parametrize(
    "missing_field",
    ["process_created_at", "process_group_id", "command_identity"],
)
async def test_missing_recorded_identity_fields_fail_closed(missing_field: str) -> None:
    created_at = datetime.now(UTC)
    execution = shell_execution(created_at)
    if missing_field == "command_identity":
        execution = execution.model_copy(
            update={"argv": [], "command_text": None, "executable_path": None}
        )
    else:
        execution = execution.model_copy(update={missing_field: None})
    inspector = StaticInspector(
        ProcessIdentity(
            pid=execution.pid or 0,
            created_at=created_at,
            command="/bin/sleep 120",
            process_group_id=8123,
        )
    )

    assert await inspector.matches(execution) is False
