from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

import riftx.runner._durable_file as durable_file_module
from riftx.domain import (
    Execution,
    ExecutionStatus,
    ExecutorType,
    RunnerPrincipal,
    TerminalSession,
    TerminalStatus,
)
from riftx.runner.state import FileExecutionRepository, FileTerminalRepository


def _execution(identifier: str, *, execution_key: str | None = None) -> Execution:
    return Execution(
        id=identifier,
        execution_key=execution_key or f"execution-key-{identifier}",
        run_id="run-state",
        node_id="runner-state",
        executor_type=ExecutorType.PROCESS,
        argv=["probe"],
        cwd="/tmp",
        stdout_path=f"/tmp/{identifier}.stdout",
        stderr_path=f"/tmp/{identifier}.stderr",
    )


def _create_execution_in_process(path: str, identifier: str, start) -> None:
    if not start.wait(timeout=10):
        raise RuntimeError("execution repository concurrency test timed out")
    asyncio.run(FileExecutionRepository(Path(path)).create_if_absent(_execution(identifier)))


def _claim_execution_key_in_process(
    path: str,
    identifier: str,
    execution_key: str,
    start,
    results,
) -> None:
    if not start.wait(timeout=10):
        raise RuntimeError("execution key claim concurrency test timed out")
    _, created = asyncio.run(
        FileExecutionRepository(Path(path)).create_if_absent(
            _execution(identifier, execution_key=execution_key)
        )
    )
    results.put(created)


def _create_terminal_in_process(path: str, identifier: str, start) -> None:
    if not start.wait(timeout=10):
        raise RuntimeError("terminal repository concurrency test timed out")
    terminal = TerminalSession(
        id=identifier,
        run_id="run-state",
        execution_id=f"execution-{identifier}",
    )
    asyncio.run(FileTerminalRepository(Path(path)).create(terminal))


def test_execution_repository_processes_merge_concurrent_creates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    path = tmp_path / "executions.json"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(
            target=_create_execution_in_process,
            args=(str(path), f"execution-{index}", start),
        )
        for index in range(12)
    ]

    for process in processes:
        process.start()
    start.set()
    try:
        for process in processes:
            process.join(timeout=20)
        assert all(process.exitcode == 0 for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert {item["id"] for item in persisted} == {
        f"execution-{index}" for index in range(12)
    }


def test_execution_repository_processes_allow_one_execution_key_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    path = tmp_path / "executions.json"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_claim_execution_key_in_process,
            args=(str(path), f"claim-{index}", "shared-key", start, results),
        )
        for index in range(8)
    ]

    for process in processes:
        process.start()
    start.set()
    try:
        for process in processes:
            process.join(timeout=20)
        assert all(process.exitcode == 0 for process in processes)
        claims = [results.get(timeout=5) for _ in processes]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()

    assert claims.count(True) == 1
    assert claims.count(False) == 7
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert len(persisted) == 1
    assert persisted[0]["execution_key"] == "shared-key"


def test_terminal_repository_processes_merge_concurrent_creates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    path = tmp_path / "terminals.json"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(
            target=_create_terminal_in_process,
            args=(str(path), f"terminal-{index}", start),
        )
        for index in range(12)
    ]

    for process in processes:
        process.start()
    start.set()
    try:
        for process in processes:
            process.join(timeout=20)
        assert all(process.exitcode == 0 for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert {item["id"] for item in persisted} == {
        f"terminal-{index}" for index in range(12)
    }


async def test_state_replace_fsyncs_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsynced: list[str] = []
    real_fsync = durable_file_module.os.fsync

    def recording_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        fsynced.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(durable_file_module.os, "fsync", recording_fsync)
    repository = FileExecutionRepository(tmp_path / "executions.json")

    _, created = await repository.create_if_absent(_execution("durable"))

    assert created
    assert "file" in fsynced
    if os.name != "nt":
        assert "directory" in fsynced


async def test_failed_replace_preserves_previous_state_and_removes_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "executions.json"
    repository = FileExecutionRepository(path)
    await repository.create_if_absent(_execution("existing"))
    original = path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError(f"injected replace failure for {source} -> {destination}")

    monkeypatch.setattr(durable_file_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        await repository.create_if_absent(_execution("not-committed"))

    assert path.read_bytes() == original
    temporary_files = await asyncio.to_thread(
        lambda: list(tmp_path.glob(".executions.json.*.tmp"))
    )
    assert temporary_files == []


async def test_terminal_active_scan_includes_created_but_open_scan_does_not(
    tmp_path: Path,
) -> None:
    repository = FileTerminalRepository(tmp_path / "terminals.json")
    created = TerminalSession(
        id="created",
        run_id="run-state",
        execution_id="execution-created",
    )
    opened = TerminalSession(
        id="opened",
        run_id="run-state",
        execution_id="execution-opened",
    )
    opened.transition_to(TerminalStatus.OPEN)
    closed = TerminalSession(
        id="closed",
        run_id="run-state",
        execution_id="execution-closed",
    )
    closed.transition_to(TerminalStatus.CLOSED)
    for terminal in (created, opened, closed):
        await repository.create(terminal)

    assert {item.id for item in await repository.list_active()} == {"created", "opened"}
    assert [item.id for item in await repository.list_open()] == ["opened"]


async def test_terminal_status_is_monotonic_across_file_repository_instances(
    tmp_path: Path,
) -> None:
    path = tmp_path / "terminal-status-cas.json"
    repository = FileTerminalRepository(path)
    terminal = TerminalSession(
        id="terminal-status-cas",
        run_id="run-state",
        execution_id="execution-status-cas",
    )
    await repository.create(terminal)
    opened = terminal.model_copy(deep=True)
    opened.transition_to(TerminalStatus.OPEN)
    closed = terminal.model_copy(deep=True)
    closed.transition_to(TerminalStatus.CLOSED)

    await asyncio.gather(
        FileTerminalRepository(path).save(opened),
        FileTerminalRepository(path).save(closed),
    )

    persisted = await repository.get(terminal.id)
    assert persisted is not None and persisted.status is TerminalStatus.CLOSED
    stale_current, stale_saved = await FileTerminalRepository(path).save_if_status(
        opened,
        expected={TerminalStatus.CREATED},
    )
    assert stale_saved is False
    assert stale_current.status is TerminalStatus.CLOSED
    assert (await FileTerminalRepository(path).save(opened)).status is TerminalStatus.CLOSED


async def test_execution_id_cannot_be_rebound_to_a_different_key(tmp_path: Path) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    original = _execution("immutable-id", execution_key="original-key")
    await repository.create_if_absent(original)

    with pytest.raises(RuntimeError, match="already bound to key"):
        await repository.create_if_absent(
            _execution("immutable-id", execution_key="replacement-key")
        )

    persisted = await repository.get("immutable-id")
    assert persisted is not None and persisted.execution_key == "original-key"


async def test_execution_owner_binding_is_one_way_across_repository_instances(
    tmp_path: Path,
) -> None:
    path = tmp_path / "executions.json"
    repository = FileExecutionRepository(path)
    execution = _execution("owner-binding")
    await repository.create_if_absent(execution)
    first = execution.model_copy(deep=True)
    first.owner = RunnerPrincipal(instance_id="runner-a", epoch=1)
    second = execution.model_copy(deep=True)
    second.owner = RunnerPrincipal(instance_id="runner-b", epoch=1)

    outcomes = await asyncio.gather(
        FileExecutionRepository(path).save(first),
        FileExecutionRepository(path).save(second),
        return_exceptions=True,
    )

    assert sum(isinstance(item, Execution) for item in outcomes) == 1
    assert sum(isinstance(item, RuntimeError) for item in outcomes) == 1
    persisted = await repository.get(execution.id)
    assert persisted is not None
    assert persisted.owner in (first.owner, second.owner)

    stale_ownerless = persisted.model_copy(deep=True)
    stale_ownerless.owner = None
    with pytest.raises(RuntimeError, match="owner is immutable"):
        await repository.save_if_status(
            stale_ownerless,
            expected={persisted.status},
        )


async def test_execution_physical_stop_proof_cannot_be_removed_or_replaced(
    tmp_path: Path,
) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    execution = _execution("immutable-stop-proof")
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    execution.transition_to(ExecutionStatus.EXITED)
    confirmed_at = datetime(2026, 8, 1, tzinfo=UTC)
    execution.physical_stop_confirmed_at = confirmed_at
    await repository.create_if_absent(execution)

    for replacement in (None, datetime(2026, 8, 2, tzinfo=UTC)):
        stale = execution.model_copy(deep=True)
        stale.physical_stop_confirmed_at = replacement
        current, saved = await repository.save_if_status(
            stale,
            expected={ExecutionStatus.EXITED},
        )
        assert saved is False
        assert current.physical_stop_confirmed_at == confirmed_at

    persisted = await repository.get(execution.id)
    assert persisted is not None
    assert persisted.physical_stop_confirmed_at == confirmed_at


async def test_execution_physical_identity_binding_rejects_split_brain_and_stale_cancel(
    tmp_path: Path,
) -> None:
    path = tmp_path / "executions.json"
    repository = FileExecutionRepository(path)
    execution = _execution("identity-cas")
    execution.transition_to(ExecutionStatus.STARTING)
    await repository.create_if_absent(execution)
    first = execution.model_copy(deep=True)
    first.pid = 41001
    first.process_group_id = 41001
    first.containment_id = "containment-first"
    first.process_created_at = datetime(2026, 8, 1, tzinfo=UTC)
    first.executable_path = "/usr/bin/first"
    first.tool_id = "tool-first"
    first.tool_version = "1.0"
    first.platform_system = "Linux"
    first.platform_release = "first-release"
    first.platform_architecture = "x86_64"
    second = execution.model_copy(deep=True)
    second.pid = 41002
    second.process_group_id = 41002
    second.containment_id = "containment-second"
    second.process_created_at = datetime(2026, 8, 2, tzinfo=UTC)
    second.executable_path = "/usr/bin/second"
    second.tool_id = "tool-second"
    second.tool_version = "2.0"
    second.platform_system = "Linux"
    second.platform_release = "second-release"
    second.platform_architecture = "aarch64"

    outcomes = await asyncio.gather(
        FileExecutionRepository(path).save_if_status(
            first,
            expected={ExecutionStatus.STARTING},
        ),
        FileExecutionRepository(path).save_if_status(
            second,
            expected={ExecutionStatus.STARTING},
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(item, tuple) and item[1] is True for item in outcomes) == 1
    assert sum(isinstance(item, RuntimeError) for item in outcomes) == 1
    bound = await repository.get(execution.id)
    assert bound is not None
    assert bound.pid in {first.pid, second.pid}
    stale_callback = execution.model_copy(deep=True)
    callback_current, callback_saved = await repository.save_if_status(
        stale_callback,
        expected={ExecutionStatus.STARTING},
    )
    assert callback_saved is False
    assert callback_current.tool_id == bound.tool_id
    assert callback_current.platform_architecture == bound.platform_architecture
    stale_cancel = execution.model_copy(deep=True)
    stale_cancel.transition_to(ExecutionStatus.CANCELLED)
    stale_cancel.physical_stop_confirmed_at = datetime(2026, 8, 3, tzinfo=UTC)

    current, saved = await repository.save_if_status(
        stale_cancel,
        expected={ExecutionStatus.STARTING},
    )

    assert saved is False
    assert current.status is ExecutionStatus.STARTING
    assert current.pid == bound.pid
    assert current.process_group_id == bound.process_group_id
    assert current.containment_id == bound.containment_id
    assert current.process_created_at == bound.process_created_at
    assert current.physical_stop_confirmed_at is None
