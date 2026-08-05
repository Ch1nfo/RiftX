from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import riftx.executors.process as process_module
from riftx.application.errors import ApplicationConflictError, RepositoryConflictError
from riftx.application.services.run_safety import RunSafetyStopService
from riftx.domain import (
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
    RunnerPrincipal,
)
from riftx.executors import (
    DirectProcessExecutor,
    EnvironmentMode,
    LinuxCgroupV2Manager,
    ProcessResult,
    ProcessStartError,
    ShellKind,
    build_shell_argv,
)
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import ExecutionLaunchRequest, ProcessInspector, ProcessSupervisor, RunnerPaths
from riftx.runner.state import FileExecutionRepository
from riftx.runner.supervisor import ProcessTerminationError

from ._containment_support import FakeKernelContainmentManager

FIXTURE = Path(__file__).parent / "fixtures" / "fake_process.py"
PYTHON_EXECUTABLE = str(Path(sys.executable).resolve())


def wait_for_nonempty_file(path: Path, deadline_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for {path}")


def wait_for_process_exit(pid: int, deadline_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for process {pid} to exit")


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def kill_process_group(process_group_id: int | None) -> None:
    if process_group_id is None:
        return
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return


class AlwaysMatchingInspector:
    async def matches(self, execution: Execution) -> bool:
        return True


class NeverMatchingInspector:
    async def matches(self, execution: Execution) -> bool:
        return False


class MatchThenMissingInspector:
    def __init__(self) -> None:
        self.calls = 0

    async def matches(self, execution: Execution) -> bool:
        self.calls += 1
        return self.calls == 1


class FailingInspector:
    async def matches(self, execution: Execution) -> bool:
        raise RuntimeError("process inspection failed")


class BlockingNeverMatchingInspector:
    """Pause an absence verdict so a concurrent terminal write can win."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def matches(self, execution: Execution) -> bool:
        self.entered.set()
        await self.release.wait()
        return False


class _DetachedContainment:
    def __init__(
        self,
        *,
        identifier: str,
        boundary_exists: bool = True,
        block_termination: bool = False,
        identifier_error: Exception | None = None,
    ) -> None:
        self._identifier = identifier
        self._boundary_exists = boundary_exists
        self._identifier_error = identifier_error
        self.block_termination = block_termination
        self.terminate_started = asyncio.Event()
        self.release_terminate = asyncio.Event()
        self.terminate_calls = 0
        self.cleaned = False

    @property
    def identifier(self) -> str:
        if self._identifier_error is not None:
            raise self._identifier_error
        return self._identifier

    def boundary_exists(self) -> bool:
        return self._boundary_exists

    async def is_populated(self) -> bool:
        return True

    async def terminate(self, *, grace_seconds: float) -> None:
        self.terminate_calls += 1
        self.terminate_started.set()
        if self.block_termination:
            await self.release_terminate.wait()

    async def cleanup(self) -> None:
        self.cleaned = True
        self._boundary_exists = False


class _DetachedContainmentManager:
    def __init__(self, containment: _DetachedContainment) -> None:
        self.containment = containment

    def containment_for(self, execution_key: str) -> _DetachedContainment:
        return self.containment


class _DetachedProcessExecutor:
    def __init__(self, containment: _DetachedContainment) -> None:
        self.containment_manager = _DetachedContainmentManager(containment)


class ActivationFailingProcessHandle:
    def __init__(self, argv: list[str]) -> None:
        self.request = SimpleNamespace(argv=list(argv))
        self.pid = 424245
        self.process_group_id = 424245
        self.containment_identifier = "activation-failing-containment"
        self.started_at = datetime.now(UTC)
        self.aborted = False
        self.boundary_exists = True

    async def activate(self) -> None:
        raise ProcessStartError("injected activation failure")

    async def abort_gated_start(
        self,
        *,
        confirmation_seconds: float = 0.5,
        cleanup_containment: bool = False,
    ) -> bool:
        del confirmation_seconds
        assert cleanup_containment is False
        self.aborted = True
        return True

    async def cleanup_confirmed_containment(self) -> None:
        self.boundary_exists = False


class BlockFirstConditionalExecutionSave:
    """Pause one post-spawn CAS so cancellation hits the ownership gap."""

    def __init__(self, delegate: SQLAlchemyExecutionRepository) -> None:
        self._delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.captured_pid: int | None = None
        self._block_next = True

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def save_if_status(self, execution: Execution, *, expected):
        if self._block_next:
            self._block_next = False
            self.captured_pid = execution.pid
            self.entered.set()
            await self.release.wait()
        return await self._delegate.save_if_status(execution, expected=expected)


class BlockFinalizationConditionalSave:
    """Pause an EXITED CAS after the monitor has read an active record."""

    def __init__(self, delegate: SQLAlchemyExecutionRepository) -> None:
        self._delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._block_next = True

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def save_if_status(self, execution: Execution, *, expected):
        if self._block_next and execution.status is ExecutionStatus.EXITED:
            self._block_next = False
            self.entered.set()
            await self.release.wait()
        return await self._delegate.save_if_status(execution, expected=expected)


class RejectPhysicalStopProofSave:
    def __init__(self, delegate: SQLAlchemyExecutionRepository) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def save_if_status(self, execution: Execution, *, expected):
        if execution.physical_stop_confirmed_at is not None:
            raise RuntimeError("injected physical-stop proof persistence failure")
        return await self._delegate.save_if_status(execution, expected=expected)


class FailingSpawnCleanupContainment:
    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self.fail_force_terminate = False

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def force_terminate(self, *, confirmation_seconds: float) -> None:
        if self.fail_force_terminate:
            raise RuntimeError("injected force_terminate failure")
        await self._delegate.force_terminate(
            confirmation_seconds=confirmation_seconds,
        )


class FailingSpawnCleanupManager:
    def __init__(self, root: Path) -> None:
        self._delegate = FakeKernelContainmentManager(root)
        self.containment: FailingSpawnCleanupContainment | None = None

    async def prepare(self, execution_key: str) -> FailingSpawnCleanupContainment:
        containment = FailingSpawnCleanupContainment(await self._delegate.prepare(execution_key))
        self.containment = containment
        return containment

    def containment_for(self, execution_key: str) -> FailingSpawnCleanupContainment:
        resolved = self._delegate.containment_for(execution_key)
        if self.containment is None or self.containment.identifier != resolved.identifier:
            raise AssertionError("spawn-cleanup containment was not prepared")
        return self.containment


async def make_supervisor(
    tmp_path: Path,
    *,
    on_completed: Callable[[Execution], Awaitable[None]] | None = None,
) -> tuple[Database, SQLAlchemyExecutionRepository, ProcessSupervisor]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Runner tests"))
    await runs.create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="local-node",
            objective=Objective(description="Exercise runner"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        termination_grace_seconds=0.1,
        on_completed=on_completed,
    )
    return database, executions, supervisor


def launch_request(
    tmp_path: Path,
    key: str,
    *fixture_args: str,
    timeout_seconds: float | None = None,
) -> ExecutionLaunchRequest:
    return ExecutionLaunchRequest(
        execution_key=key,
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        cwd=tmp_path,
        argv=[sys.executable, str(FIXTURE), *fixture_args],
        tool_id="fake-process",
        tool_version="1.2.3",
        env={"RIFTX_TEST_VALUE": "supervised"},
        timeout_seconds=timeout_seconds,
    )


def shell_launch_request(
    tmp_path: Path,
    key: str,
    *,
    script: str,
) -> ExecutionLaunchRequest:
    if Path("/bin/zsh").exists():
        shell = ShellKind.ZSH
        shell_path = Path("/bin/zsh")
    elif Path("/bin/bash").exists():
        shell = ShellKind.BASH
        shell_path = Path("/bin/bash")
    else:
        pytest.skip("zsh/bash is required for shell execution")
    return ExecutionLaunchRequest(
        execution_key=key,
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.SHELL,
        cwd=tmp_path,
        command_text=script,
        shell=shell,
        shell_path=shell_path,
        timeout_seconds=30,
    )


async def test_supervisor_persists_lifecycle_and_reads_output_by_cursor(
    tmp_path: Path,
) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(launch_request(tmp_path, "success-key", "success"))
    completed = await supervisor.wait(started.id)
    persisted = await executions.get(completed.id)

    first = await supervisor.read_output(completed.id, max_bytes=8)
    second = await supervisor.read_output(
        completed.id,
        stdout_cursor=first.stdout.next_cursor,
        stderr_cursor=first.stderr.next_cursor,
        max_bytes=1024,
    )

    assert completed.status is ExecutionStatus.EXITED
    assert completed.exit_code == 0
    assert completed.tool_id == "fake-process"
    assert completed.tool_version == "1.2.3"
    assert completed.executable_path == PYTHON_EXECUTABLE
    assert completed.platform_system
    assert completed.platform_release
    assert completed.platform_architecture
    assert completed.process_created_at == completed.started_at
    assert completed.created_at is not None
    assert persisted is not None and persisted.created_at == completed.created_at
    assert first.stdout.data + second.stdout.data == (
        b"stdout: \xe4\xbd\xa0\xe5\xa5\xbd RiftX\nenv: supervised\n"
    )
    assert first.stderr.data + second.stderr.data == b"stderr: diagnostic\n"
    assert second.stdout.eof is True
    assert second.stderr.eof is True
    await supervisor.close()
    await database.dispose()


async def test_supervisor_notifies_after_persisting_completion(tmp_path: Path) -> None:
    completed_ids: list[str] = []

    async def notify(execution: Execution) -> None:
        completed_ids.append(execution.id)

    database, executions, supervisor = await make_supervisor(
        tmp_path,
        on_completed=notify,
    )
    started = await supervisor.start(launch_request(tmp_path, "notify-key", "success"))
    completed = await supervisor.wait(started.id)

    assert completed_ids == [completed.id]
    persisted = await executions.get(completed.id)
    assert persisted is not None and persisted.status is ExecutionStatus.EXITED
    await supervisor.close()
    await database.dispose()


async def test_output_is_readable_while_process_is_running(tmp_path: Path) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(launch_request(tmp_path, "stream-key", "stream"))
    await asyncio.to_thread(wait_for_nonempty_file, Path(started.stdout_path))

    first = await supervisor.read_output(started.id)
    completed = await supervisor.wait(started.id)
    second = await supervisor.read_output(started.id, stdout_cursor=first.stdout.next_cursor)

    assert completed.status is ExecutionStatus.EXITED
    assert first.stdout.data == b"first\n"
    assert second.stdout.data == b"second\n"
    await supervisor.close()
    await database.dispose()


async def test_monitor_finalization_cannot_overwrite_concurrent_cancelled_status(
    tmp_path: Path,
) -> None:
    database, executions, initial_supervisor = await make_supervisor(tmp_path)
    await initial_supervisor.close()
    barrier_repository = BlockFinalizationConditionalSave(executions)
    completed_callbacks: list[ExecutionStatus] = []

    async def record_completion(execution: Execution) -> None:
        completed_callbacks.append(execution.status)

    supervisor = ProcessSupervisor(
        barrier_repository,  # type: ignore[arg-type]
        RunnerPaths(tmp_path / "finalization-cas-state"),
        on_completed=record_completion,
    )
    started = await supervisor.start(launch_request(tmp_path, "finalization-cas-key", "success"))
    await barrier_repository.entered.wait()

    concurrent = await executions.get(started.id)
    assert concurrent is not None
    assert concurrent.status is ExecutionStatus.RUNNING
    concurrent.transition_to(ExecutionStatus.CANCELLED)
    await executions.save(concurrent)
    barrier_repository.release.set()

    settled = await supervisor.wait(started.id)

    assert settled.status is ExecutionStatus.CANCELLED
    assert settled.physical_stop_confirmed_at is not None
    assert completed_callbacks == [ExecutionStatus.CANCELLED]
    await supervisor.close()
    await database.dispose()


async def test_concurrent_physical_stop_finalization_notifies_at_most_once(
    tmp_path: Path,
) -> None:
    completed_callbacks: list[ExecutionStatus] = []

    async def record_completion(execution: Execution) -> None:
        completed_callbacks.append(execution.status)

    database, executions, supervisor = await make_supervisor(
        tmp_path,
        on_completed=record_completion,
    )
    execution = Execution(
        id="concurrent-proof-finalization",
        execution_key="concurrent-proof-finalization",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=["fake-process"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "concurrent-proof.stdout"),
        stderr_path=str(tmp_path / "concurrent-proof.stderr"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    await executions.create_if_absent(execution)
    result = ProcessResult(status=ExecutionStatus.EXITED, exit_code=0)

    await asyncio.gather(
        supervisor._finalize(
            execution.id,
            result,
            cancel_confirmed=False,
            physical_stop_confirmed=True,
        ),
        supervisor._finalize(
            execution.id,
            result,
            cancel_confirmed=False,
            physical_stop_confirmed=True,
        ),
    )

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.EXITED
    assert persisted.physical_stop_confirmed_at is not None
    assert completed_callbacks == [ExecutionStatus.EXITED]
    await supervisor.close()
    await database.dispose()


async def test_execution_key_is_idempotent(tmp_path: Path) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    request = launch_request(tmp_path, "same-key", "success")

    first = await supervisor.start(request)
    second = await supervisor.start(request)
    completed = await supervisor.wait(first.id)
    output = await supervisor.read_output(first.id)

    assert second.id == first.id
    assert completed.status is ExecutionStatus.EXITED
    assert output.stdout.data.count(b"stdout:") == 1
    await supervisor.close()
    await database.dispose()


async def test_process_launch_fingerprint_rejects_stable_field_replay(
    tmp_path: Path,
) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    request = launch_request(tmp_path, "process-launch-identity", "success")

    first = await supervisor.start(request)
    exact = await supervisor.start(request)
    assert exact.id == first.id
    assert exact.launch_fingerprint == request.launch_fingerprint

    foreign_requests = (
        request.model_copy(update={"argv": [sys.executable, "-c", "print('foreign')"]}),
        request.model_copy(update={"env": {"RIFTX_TEST_VALUE": "foreign"}}),
        request.model_copy(update={"timeout_seconds": 17.0}),
        request.model_copy(update={"tool_version": "foreign-version"}),
        request.model_copy(update={"session_id": "foreign-session"}),
        request.model_copy(update={"execution_id": "foreign-explicit-id"}),
    )
    for foreign in foreign_requests:
        with pytest.raises(RepositoryConflictError):
            await supervisor.start(foreign)

    completed = await supervisor.wait(first.id)
    output = await supervisor.read_output(first.id)
    assert completed.status is ExecutionStatus.EXITED
    assert output.stdout.data.count(b"stdout:") == 1
    await supervisor.close()
    await database.dispose()


async def test_shell_empty_argv_first_binds_and_fingerprint_replay_is_exact(
    tmp_path: Path,
) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    request = shell_launch_request(
        tmp_path,
        "shell-launch-identity",
        script="printf 'shell-ok\\n'",
    )
    assert request.argv == []

    first = await supervisor.start(request)
    exact = await supervisor.start(request)

    assert first.argv == build_shell_argv(
        request.shell,
        request.shell_path or Path("/bin/sh"),
        request.command_text or "",
    )
    assert exact.id == first.id
    assert exact.argv == first.argv
    for foreign in (
        request.model_copy(update={"command_text": "printf 'foreign\\n'"}),
        request.model_copy(update={"timeout_seconds": 19.0}),
        request.model_copy(update={"environment_mode": EnvironmentMode.CLEAN}),
        request.model_copy(update={"shell_path": tmp_path / "foreign-shell"}),
    ):
        with pytest.raises(RepositoryConflictError):
            await supervisor.start(foreign)

    completed = await supervisor.wait(first.id)
    assert completed.status is ExecutionStatus.EXITED
    await supervisor.close()
    await database.dispose()


async def test_legacy_null_fingerprint_shell_replay_allows_resolved_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    request = shell_launch_request(
        tmp_path,
        "legacy-shell-key",
        script="printf 'legacy\\n'",
    )
    assert request.shell is not None
    shell_path = request.shell_path or Path("/bin/sh")
    legacy = Execution(
        id="legacy-shell-execution",
        execution_key=request.execution_key,
        launch_fingerprint=None,
        run_id=request.run_id,
        node_id=request.node_id,
        executor_type=ExecutorType.SHELL,
        argv=build_shell_argv(request.shell, shell_path, request.command_text or ""),
        command_text=request.command_text,
        cwd=str(request.cwd),
        env_diff=request.env,
        stdout_path=str(tmp_path / "legacy-shell.stdout"),
        stderr_path=str(tmp_path / "legacy-shell.stderr"),
    )
    assert (await executions.create_if_absent(legacy))[1] is True
    start_handle = AsyncMock(side_effect=AssertionError("legacy replay must not spawn"))
    monkeypatch.setattr(supervisor, "_start_handle", start_handle)

    replay = await supervisor.start(request)

    assert replay.id == legacy.id
    assert replay.argv == legacy.argv
    start_handle.assert_not_awaited()
    with pytest.raises(RepositoryConflictError):
        await supervisor.start(request.model_copy(update={"command_text": "printf 'foreign\\n'"}))
    await supervisor.close()
    await database.dispose()


async def test_legacy_null_fingerprint_rejects_foreign_explicit_execution_id(
    tmp_path: Path,
) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    request = launch_request(tmp_path, "legacy-explicit-id", "success")
    legacy = Execution(
        id="legacy-original-id",
        execution_key=request.execution_key,
        launch_fingerprint=None,
        run_id=request.run_id,
        node_id=request.node_id,
        executor_type=request.executor_type,
        argv=request.argv,
        tool_id=request.tool_id,
        tool_version=request.tool_version,
        cwd=str(request.cwd),
        env_diff=request.env,
        stdout_path=str(tmp_path / "legacy-explicit.stdout"),
        stderr_path=str(tmp_path / "legacy-explicit.stderr"),
    )
    assert (await executions.create_if_absent(legacy))[1] is True

    with pytest.raises(ApplicationConflictError) as captured:
        await supervisor.start(request.model_copy(update={"execution_id": "foreign-explicit-id"}))

    assert captured.value.code == "execution_idempotency_conflict"
    assert (await executions.get(legacy.id)) is not None
    await supervisor.close()
    await database.dispose()


async def test_start_registers_starting_before_effect_guard_and_never_spawns_when_blocked(
    tmp_path: Path,
) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    owner = RunnerPrincipal(instance_id="starting-owner", epoch=4)
    guard_entered = asyncio.Event()
    guard_release = asyncio.Event()

    async def blocked_guard() -> None:
        guard_entered.set()
        await guard_release.wait()
        raise ApplicationConflictError("run_execution_blocked", "Run is pausing")

    start_task = asyncio.create_task(
        supervisor.start(
            launch_request(
                tmp_path,
                "guarded-start",
                "sleep",
                "--seconds",
                "30",
            ).model_copy(update={"runner_principal": owner}),
            effect_guard=blocked_guard,
        )
    )
    await guard_entered.wait()

    registered = list(await executions.list("run-1"))
    assert len(registered) == 1
    assert registered[0].status is ExecutionStatus.STARTING
    assert registered[0].owner == owner
    assert registered[0].pid is None

    guard_release.set()
    with pytest.raises(ApplicationConflictError, match="Run is pausing"):
        await start_task

    persisted = await executions.get(registered[0].id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    assert persisted.pid is None
    await supervisor.close()
    await database.dispose()


async def test_safely_aborted_activation_persists_cancelled_proof_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    request = launch_request(tmp_path, "activation-failure-proof", "success")
    handle = ActivationFailingProcessHandle(request.argv)

    async def return_activation_failing_handle(*args: object) -> ActivationFailingProcessHandle:
        del args
        return handle

    monkeypatch.setattr(supervisor, "_start_handle", return_activation_failing_handle)

    stopped = await supervisor.start(request)

    persisted = await executions.get(stopped.id)
    assert handle.aborted is True
    assert handle.boundary_exists is False
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    assert persisted.physical_stop_confirmed_at is not None
    await supervisor.close()
    await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="fake containment uses POSIX process groups")
async def test_remote_owner_survives_process_crash_snapshot_duplicate_and_cancel(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "remote-owner-crash-state"
    repository_path = state_path / "executions.json"
    repository = FileExecutionRepository(repository_path)
    containment_manager = FakeKernelContainmentManager(tmp_path / "remote-owner-containment")
    process_executor = DirectProcessExecutor(
        containment_manager,
        autodetect_containment=False,
        defer_activation=True,
    )
    owner = RunnerPrincipal(instance_id="remote-owner-instance", epoch=9)
    request = launch_request(
        tmp_path,
        "remote-owner-crash-key",
        "sleep",
        "--seconds",
        "30",
    ).model_copy(
        update={
            "execution_id": "remote-owner-crash-execution",
            "runner_principal": owner,
        }
    )
    original = ProcessSupervisor(
        repository,
        RunnerPaths(state_path),
        process_executor=process_executor,
        termination_grace_seconds=0.1,
    )
    started = await original.start(request)
    assert started.pid is not None
    try:
        crash_snapshot = await FileExecutionRepository(repository_path).get(started.id)
        assert crash_snapshot is not None
        assert crash_snapshot.status is ExecutionStatus.RUNNING
        assert crash_snapshot.owner == owner
        assert crash_snapshot.containment_id is not None

        # Dropping only the in-memory monitor models a Runner process crash;
        # the durable record and the owned child remain for restart recovery.
        await original.close(cancel_running=False)
        assert process_exists(started.pid)
        restarted_repository = FileExecutionRepository(repository_path)
        restarted = ProcessSupervisor(
            restarted_repository,
            RunnerPaths(state_path),
            process_executor=DirectProcessExecutor(
                containment_manager,
                autodetect_containment=False,
                defer_activation=True,
            ),
            termination_grace_seconds=0.1,
        )

        duplicate = await restarted.start(request)
        assert duplicate.id == started.id
        assert duplicate.pid == started.pid
        assert duplicate.owner == owner

        cancelled = await restarted.cancel(started.id)
        assert cancelled.status is ExecutionStatus.CANCELLED
        assert cancelled.owner == owner
        assert cancelled.physical_stop_confirmed_at is not None
        await asyncio.to_thread(wait_for_process_exit, started.pid)
        await restarted.close()
    finally:
        kill_process_group(started.process_group_id)
        await original.close(cancel_running=False)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup assertion")
async def test_cancelled_start_coroutine_terminates_process_spawned_before_registration(
    tmp_path: Path,
) -> None:
    database, executions, _ = await make_supervisor(tmp_path)
    barrier_repository = BlockFirstConditionalExecutionSave(executions)
    supervisor = ProcessSupervisor(
        barrier_repository,  # type: ignore[arg-type]
        RunnerPaths(tmp_path / "cancelled-start-state"),
        termination_grace_seconds=0.1,
    )
    start_task = asyncio.create_task(
        supervisor.start(launch_request(tmp_path, "cancelled-start", "sleep", "--seconds", "30"))
    )
    await barrier_repository.entered.wait()
    spawned_pid = barrier_repository.captured_pid
    assert spawned_pid is not None

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    await asyncio.to_thread(wait_for_process_exit, spawned_pid)
    registered = list(await executions.list("run-1"))
    assert len(registered) == 1
    assert registered[0].status is ExecutionStatus.CANCELLED
    await supervisor.close()
    await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="gated launcher is POSIX-only")
@pytest.mark.parametrize("failure_mode", ["force_terminate", "process_wait"])
async def test_spawn_cleanup_failure_retains_identity_and_never_manufactures_stop_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    database, executions, _ = await make_supervisor(tmp_path)
    manager = FailingSpawnCleanupManager(tmp_path / "spawn-cleanup-containment")
    executor = DirectProcessExecutor(
        manager,  # type: ignore[arg-type]
        autodetect_containment=False,
        require_containment=True,
        defer_activation=True,
    )
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "spawn-cleanup-state"),
        process_executor=executor,
        termination_grace_seconds=0.1,
    )
    wait_failure_enabled = failure_mode == "process_wait"
    original_wait = asyncio.subprocess.Process.wait

    async def injected_wait(process: asyncio.subprocess.Process) -> int:
        if wait_failure_enabled:
            raise RuntimeError("injected process.wait failure")
        return await original_wait(process)

    async def fail_readiness(*_args, **_kwargs) -> bytes:
        raise ProcessStartError("injected launcher readiness failure")

    monkeypatch.setattr(asyncio.subprocess.Process, "wait", injected_wait)
    monkeypatch.setattr(process_module, "_read_launcher_readiness", fail_readiness)
    if failure_mode == "force_terminate":
        # ``prepare`` creates this wrapper during ``start``; set its failure
        # immediately after the launcher reaches the readiness hook.
        async def fail_after_prepare(*args, **kwargs) -> bytes:
            assert manager.containment is not None
            manager.containment.fail_force_terminate = True
            return await fail_readiness(*args, **kwargs)

        monkeypatch.setattr(
            process_module,
            "_read_launcher_readiness",
            fail_after_prepare,
        )

    started = await supervisor.start(
        launch_request(tmp_path, f"spawn-cleanup-{failure_mode}", "sleep", "--seconds", "30")
    )

    assert started.status is ExecutionStatus.STARTING
    assert started.pid is not None
    assert started.process_group_id is not None
    assert started.containment_id is not None
    assert started.process_created_at is not None
    assert started.physical_stop_confirmed_at is None

    expected_error = (
        "force_terminate failure" if failure_mode == "force_terminate" else "process.wait failure"
    )
    with pytest.raises(RuntimeError, match=expected_error):
        await supervisor.cancel(started.id)

    unconfirmed = await executions.get(started.id)
    assert unconfirmed is not None
    assert unconfirmed.status is ExecutionStatus.STARTING
    assert unconfirmed.pid == started.pid
    assert unconfirmed.containment_id == started.containment_id
    assert unconfirmed.physical_stop_confirmed_at is None

    wait_failure_enabled = False
    assert manager.containment is not None
    manager.containment.fail_force_terminate = False
    cancelled = await supervisor.cancel(started.id)

    assert cancelled.status is ExecutionStatus.CANCELLED
    assert cancelled.physical_stop_confirmed_at is not None
    await supervisor.close()
    await database.dispose()


async def test_supervisor_marks_confirmed_timeout_as_hard_timeout(tmp_path: Path) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(
        launch_request(
            tmp_path,
            "timeout-key",
            "sleep",
            "--seconds",
            "30",
            timeout_seconds=0.05,
        )
    )

    completed = await supervisor.wait(started.id)

    assert completed.status is ExecutionStatus.HARD_TIMEOUT
    assert completed.finished_at is not None
    assert completed.physical_stop_confirmed_at is not None
    await supervisor.close()
    await database.dispose()


async def test_supervisor_cancels_managed_process_after_durable_status_becomes_failed(
    tmp_path: Path,
) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(
        launch_request(tmp_path, "managed-failed-key", "sleep", "--seconds", "30")
    )
    failed = await executions.get(started.id)
    assert failed is not None
    failed.transition_to(ExecutionStatus.FAILED)
    await executions.save(failed)

    cancelled = await supervisor.cancel(started.id)

    assert cancelled.status is ExecutionStatus.CANCELLED
    persisted = await executions.get(started.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    await supervisor.close()
    await database.dispose()


async def test_supervisor_cancels_running_process(tmp_path: Path) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(
        launch_request(tmp_path, "cancel-key", "sleep", "--seconds", "30")
    )

    cancelled = await supervisor.cancel(started.id)

    assert cancelled.status is ExecutionStatus.CANCELLED
    assert cancelled.finished_at is not None
    await supervisor.close()
    await database.dispose()


@pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or not os.environ.get("RIFTX_TEST_CGROUP_V2_ROOT")
    or not os.environ.get("RIFTX_TEST_PAYLOAD_UID")
    or not os.environ.get("RIFTX_TEST_PAYLOAD_GID"),
    reason="requires delegated cgroup v2 and a distinct payload uid/gid",
)
async def test_supervisor_cgroup_cancel_stops_setsid_double_fork(tmp_path: Path) -> None:
    database, executions, unused_supervisor = await make_supervisor(tmp_path)
    await unused_supervisor.close()
    manager = LinuxCgroupV2Manager(
        Path(os.environ["RIFTX_TEST_CGROUP_V2_ROOT"]),
        payload_uid=int(os.environ["RIFTX_TEST_PAYLOAD_UID"]),
        payload_gid=int(os.environ["RIFTX_TEST_PAYLOAD_GID"]),
    )
    await asyncio.to_thread(tmp_path.chmod, 0o777)
    executor = DirectProcessExecutor(
        manager,
        autodetect_containment=False,
        require_containment=True,
        defer_activation=True,
    )
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "real-cgroup-state"),
        process_executor=executor,
        termination_grace_seconds=0.1,
    )
    heartbeat = tmp_path / "real-cgroup-heartbeat"
    pid_file = tmp_path / "real-cgroup-escaped.pid"
    started = await supervisor.start(
        launch_request(
            tmp_path,
            f"supervisor-setsid:{tmp_path}",
            "setsid-double-fork",
            "--seconds",
            "30",
            "--heartbeat",
            str(heartbeat),
            "--pid-file",
            str(pid_file),
        )
    )
    escaped_pid: int | None = None
    try:
        await asyncio.to_thread(wait_for_nonempty_file, heartbeat)
        await asyncio.to_thread(wait_for_nonempty_file, pid_file)
        escaped_pid = int(pid_file.read_text())

        cancelled = await supervisor.cancel(started.id)
        size_after_cancel = heartbeat.stat().st_size
        await asyncio.to_thread(wait_for_process_exit, escaped_pid)
        await asyncio.sleep(0.15)

        assert started.containment_id is not None
        assert cancelled.status is ExecutionStatus.CANCELLED
        assert heartbeat.stat().st_size == size_after_cancel
    finally:
        if escaped_pid is not None and process_exists(escaped_pid):
            os.kill(escaped_pid, signal.SIGKILL)
        await supervisor.close()
        await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
async def test_managed_cancel_waits_for_stubborn_child_before_persisting_cancelled(
    tmp_path: Path,
) -> None:
    database, executions, initial_supervisor = await make_supervisor(tmp_path)
    await initial_supervisor.close()
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "managed-stubborn-state"),
        termination_grace_seconds=0.5,
    )
    heartbeat = tmp_path / "managed-stubborn-heartbeat"
    started = await supervisor.start(
        launch_request(
            tmp_path,
            "managed-stubborn-key",
            "stubborn-child",
            "--seconds",
            "30",
            "--heartbeat",
            str(heartbeat),
        )
    )
    cancel_operation: asyncio.Task[Execution] | None = None
    try:
        await asyncio.to_thread(wait_for_nonempty_file, Path(started.stdout_path))
        await asyncio.to_thread(wait_for_nonempty_file, heartbeat)
        output = await supervisor.read_output(started.id)
        child_pid = int(output.stdout.data.decode().strip().splitlines()[0])
        assert started.pid is not None

        cancel_operation = asyncio.create_task(supervisor.cancel(started.id))
        await asyncio.to_thread(wait_for_process_exit, started.pid)

        during_grace = await executions.get(started.id)
        assert during_grace is not None
        assert during_grace.status is ExecutionStatus.RUNNING
        assert process_exists(child_pid)
        assert not cancel_operation.done()

        cancelled = await cancel_operation
        assert cancelled.status is ExecutionStatus.CANCELLED
        await asyncio.to_thread(wait_for_process_exit, child_pid)
    finally:
        kill_process_group(started.process_group_id)
        if cancel_operation is not None:
            await asyncio.gather(cancel_operation, return_exceptions=True)
        await supervisor.close()
        await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
async def test_managed_cleanup_survives_caller_cancellation_during_term_grace(
    tmp_path: Path,
) -> None:
    database, executions, initial_supervisor = await make_supervisor(tmp_path)
    await initial_supervisor.close()
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "caller-cancelled-state"),
        termination_grace_seconds=0.3,
    )
    heartbeat = tmp_path / "caller-cancelled-heartbeat"
    started = await supervisor.start(
        launch_request(
            tmp_path,
            "caller-cancelled-key",
            "stubborn-child",
            "--seconds",
            "30",
            "--heartbeat",
            str(heartbeat),
        )
    )
    cancel_operation: asyncio.Task[Execution] | None = None
    try:
        assert started.pid is not None
        await asyncio.to_thread(wait_for_nonempty_file, Path(started.stdout_path))
        await asyncio.to_thread(wait_for_nonempty_file, heartbeat)
        output = await supervisor.read_output(started.id)
        child_pid = int(output.stdout.data.decode().strip().splitlines()[0])

        cancel_operation = asyncio.create_task(supervisor.cancel(started.id))
        await asyncio.to_thread(wait_for_process_exit, started.pid)
        cancel_operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancel_operation

        settled = await supervisor.wait(started.id)

        assert settled.status is ExecutionStatus.CANCELLED
        await asyncio.to_thread(wait_for_process_exit, child_pid)
    finally:
        kill_process_group(started.process_group_id)
        await supervisor.close()
        await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
async def test_managed_cancel_failure_never_persists_cancelled_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    heartbeat = tmp_path / "failed-managed-cancel-heartbeat"
    started = await supervisor.start(
        launch_request(
            tmp_path,
            "failed-managed-cancel-key",
            "stubborn-child",
            "--seconds",
            "30",
            "--heartbeat",
            str(heartbeat),
        )
    )
    await asyncio.to_thread(wait_for_nonempty_file, Path(started.stdout_path))
    await asyncio.to_thread(wait_for_nonempty_file, heartbeat)
    output = await supervisor.read_output(started.id)
    child_pid = int(output.stdout.data.decode().strip().splitlines()[0])

    async def fail_after_leader_exits(
        process_group_id: int,
        *,
        grace_seconds: float,
    ) -> None:
        del grace_seconds
        os.killpg(process_group_id, signal.SIGTERM)
        await asyncio.sleep(0.05)
        raise PermissionError("process-group confirmation denied")

    monkeypatch.setattr(
        "riftx.executors.process._terminate_posix_process_group",
        fail_after_leader_exits,
    )
    try:
        with pytest.raises(PermissionError, match="confirmation denied"):
            await asyncio.wait_for(supervisor.cancel(started.id), timeout=0.5)

        persisted = await executions.get(started.id)
        assert persisted is not None
        assert persisted.status is ExecutionStatus.RUNNING
        assert process_exists(child_pid)

        monkeypatch.undo()
        cancelled = await supervisor.cancel(started.id)

        assert cancelled.status is ExecutionStatus.CANCELLED
        await asyncio.to_thread(wait_for_process_exit, child_pid)
    finally:
        kill_process_group(started.process_group_id)
        await supervisor.close()
        await database.dispose()


@pytest.mark.parametrize("status", [ExecutionStatus.CREATED, ExecutionStatus.QUEUED])
async def test_supervisor_cancels_not_yet_started_execution(
    tmp_path: Path,
    status: ExecutionStatus,
) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    execution = Execution(
        id=f"not-started-{status.value}",
        execution_key=f"not-started-{status.value}-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "-c", "raise AssertionError('must not start')"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"{status.value}.stdout"),
        stderr_path=str(tmp_path / f"{status.value}.stderr"),
        status=status,
    )
    await executions.create_if_absent(execution)

    cancelled = await supervisor.cancel(execution.id)

    assert cancelled.status is ExecutionStatus.CANCELLED
    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    assert persisted.finished_at is not None
    await supervisor.close()
    await database.dispose()


async def test_supervisor_confirms_pre_spawn_failed_execution_cancelled(
    tmp_path: Path,
) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    execution = Execution(
        id="failed-before-spawn",
        execution_key="failed-before-spawn-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=["missing-executable"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "failed-before-spawn.stdout"),
        stderr_path=str(tmp_path / "failed-before-spawn.stderr"),
        status=ExecutionStatus.FAILED,
    )
    await executions.create_if_absent(execution)

    cancelled = await supervisor.cancel(execution.id)

    assert cancelled.status is ExecutionStatus.CANCELLED
    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize("status", [ExecutionStatus.LOST, ExecutionStatus.FAILED])
async def test_supervisor_does_not_claim_detached_absence_without_containment(
    tmp_path: Path,
    status: ExecutionStatus,
) -> None:
    database, executions, _ = await make_supervisor(tmp_path)
    execution = Execution(
        id=f"{status.value}-without-process",
        execution_key=f"{status.value}-without-process-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "missing.py"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"{status.value}-without-process.stdout"),
        stderr_path=str(tmp_path / f"{status.value}-without-process.stderr"),
        status=status,
        pid=424242,
        process_group_id=424242,
    )
    await executions.create_if_absent(execution)
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        inspector=NeverMatchingInspector(),  # type: ignore[arg-type]
    )

    with pytest.raises(ProcessTerminationError, match="no durable kernel containment"):
        await supervisor.cancel(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is status
    await supervisor.close()
    await database.dispose()


async def test_detached_containment_stop_survives_cancelled_api_caller(
    tmp_path: Path,
) -> None:
    database, executions, original_supervisor = await make_supervisor(tmp_path)
    await original_supervisor.close()
    containment = _DetachedContainment(
        identifier="durable-containment",
        block_termination=True,
    )
    execution = Execution(
        id="shielded-detached-stop",
        execution_key="shielded-detached-stop-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "missing.py"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "shielded.stdout"),
        stderr_path=str(tmp_path / "shielded.stderr"),
        status=ExecutionStatus.RUNNING,
        pid=424242,
        process_group_id=424242,
        containment_id=containment.identifier,
    )
    await executions.create_if_absent(execution)
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        process_executor=_DetachedProcessExecutor(containment),  # type: ignore[arg-type]
    )

    first_cancel = asyncio.create_task(supervisor.cancel(execution.id))
    await containment.terminate_started.wait()
    first_cancel.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_cancel

    containment.release_terminate.set()
    cancelled = await supervisor.cancel(execution.id)

    assert cancelled.status is ExecutionStatus.CANCELLED
    assert containment.terminate_calls == 1
    assert containment.cleaned is True
    await supervisor.close()
    await database.dispose()


async def test_detached_containment_leaf_survives_stop_proof_persistence_failure(
    tmp_path: Path,
) -> None:
    database, executions, original_supervisor = await make_supervisor(tmp_path)
    await original_supervisor.close()
    containment = _DetachedContainment(identifier="proof-failure-containment")
    execution = Execution(
        id="proof-failure-detached-stop",
        execution_key="proof-failure-detached-stop-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "missing.py"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "proof-failure.stdout"),
        stderr_path=str(tmp_path / "proof-failure.stderr"),
        status=ExecutionStatus.RUNNING,
        pid=424244,
        process_group_id=424244,
        containment_id=containment.identifier,
    )
    await executions.create_if_absent(execution)
    supervisor = ProcessSupervisor(
        RejectPhysicalStopProofSave(executions),  # type: ignore[arg-type]
        RunnerPaths(tmp_path / "proof-failure-state"),
        process_executor=_DetachedProcessExecutor(containment),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="proof persistence failure"):
        await supervisor.cancel(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.RUNNING
    assert persisted.physical_stop_confirmed_at is None
    assert containment.terminate_calls == 1
    assert containment.boundary_exists() is True
    assert containment.cleaned is False
    await supervisor.close()
    await database.dispose()


async def test_failed_containment_only_execution_must_terminate_before_proof(
    tmp_path: Path,
) -> None:
    database, executions, original_supervisor = await make_supervisor(tmp_path)
    await original_supervisor.close()
    containment = _DetachedContainment(identifier="failed-containment-only")
    execution = Execution(
        id="failed-containment-only",
        execution_key="failed-containment-only-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "missing.py"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "failed-containment-only.stdout"),
        stderr_path=str(tmp_path / "failed-containment-only.stderr"),
        status=ExecutionStatus.FAILED,
        containment_id=containment.identifier,
    )
    await executions.create_if_absent(execution)
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "failed-containment-only-state"),
        process_executor=_DetachedProcessExecutor(containment),  # type: ignore[arg-type]
    )

    stopped = await supervisor.cancel(execution.id)

    assert containment.terminate_calls == 1
    assert containment.cleaned is True
    assert stopped.status is ExecutionStatus.CANCELLED
    assert stopped.physical_stop_confirmed_at is not None
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize(
    "status",
    [
        ExecutionStatus.COMPLETED,
        ExecutionStatus.EXITED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.HARD_TIMEOUT,
    ],
)
async def test_detached_stop_preserves_historical_terminal_outcome_when_adding_proof(
    tmp_path: Path,
    status: ExecutionStatus,
) -> None:
    database, executions, original_supervisor = await make_supervisor(tmp_path)
    await original_supervisor.close()
    containment = _DetachedContainment(identifier=f"historical-{status.value}-containment")
    execution = Execution(
        id=f"historical-{status.value}-stop",
        execution_key=f"historical-{status.value}-stop-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "missing.py"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"historical-{status.value}.stdout"),
        stderr_path=str(tmp_path / f"historical-{status.value}.stderr"),
        status=status,
        pid=424245,
        process_group_id=424245,
        containment_id=containment.identifier,
    )
    await executions.create_if_absent(execution)
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / f"historical-{status.value}-state"),
        process_executor=_DetachedProcessExecutor(containment),  # type: ignore[arg-type]
    )

    stopped = await supervisor.cancel(execution.id)

    assert stopped.status is status
    assert stopped.physical_stop_confirmed_at is not None
    assert containment.terminate_calls == 1
    assert containment.cleaned is True
    assert containment.boundary_exists() is False
    await supervisor.close()
    await database.dispose()


async def test_existing_stop_proof_makes_missing_containment_cleanup_idempotent(
    tmp_path: Path,
) -> None:
    database, executions, original_supervisor = await make_supervisor(tmp_path)
    await original_supervisor.close()
    containment = _DetachedContainment(
        identifier="already-cleaned-process-containment",
        boundary_exists=False,
    )
    execution = Execution(
        id="already-confirmed-process-stop",
        execution_key="already-confirmed-process-stop-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "missing.py"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "already-confirmed.stdout"),
        stderr_path=str(tmp_path / "already-confirmed.stderr"),
        status=ExecutionStatus.EXITED,
        pid=424246,
        process_group_id=424246,
        containment_id=containment.identifier,
        physical_stop_confirmed_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    await executions.create_if_absent(execution)
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "already-confirmed-state"),
        process_executor=_DetachedProcessExecutor(containment),  # type: ignore[arg-type]
    )

    stopped = await supervisor.cancel(execution.id)

    assert stopped.status is ExecutionStatus.EXITED
    assert stopped.physical_stop_confirmed_at == execution.physical_stop_confirmed_at
    assert containment.terminate_calls == 0
    assert containment.cleaned is False
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize("boundary_state", ["missing", "replaced"])
async def test_detached_containment_uncertainty_preserves_active_status_on_cancel_and_recover(
    tmp_path: Path,
    boundary_state: str,
) -> None:
    database, executions, original_supervisor = await make_supervisor(tmp_path)
    await original_supervisor.close()
    execution_key = f"uncertain-containment-{boundary_state}-key"
    manager = FakeKernelContainmentManager(tmp_path / "containment")
    original = await manager.prepare(execution_key)
    persisted_identifier = original.identifier
    await original.cleanup()
    if boundary_state == "missing":
        error = "missing or its kernel identity changed"
    else:
        replacement = await manager.prepare(execution_key)
        assert replacement.identifier != persisted_identifier
        error = "different delegated root"
    execution = Execution(
        id=f"uncertain-containment-{boundary_state}",
        execution_key=execution_key,
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "missing.py"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"uncertain-{boundary_state}.stdout"),
        stderr_path=str(tmp_path / f"uncertain-{boundary_state}.stderr"),
        status=ExecutionStatus.RUNNING,
        pid=424243,
        process_group_id=424243,
        containment_id=persisted_identifier,
    )
    await executions.create_if_absent(execution)
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        process_executor=DirectProcessExecutor(manager, autodetect_containment=False),
    )

    with pytest.raises(ProcessTerminationError, match=error):
        await supervisor.cancel(execution.id)
    recovered = await supervisor.recover()

    assert [item.id for item in recovered] == [execution.id]
    assert recovered[0].status is ExecutionStatus.RUNNING
    persisted = await executions.get(execution.id)
    assert persisted is not None and persisted.status is ExecutionStatus.RUNNING
    if boundary_state == "replaced":
        assert replacement.path.exists()
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize("status", [ExecutionStatus.STARTING, ExecutionStatus.LOST])
async def test_supervisor_does_not_confirm_stop_without_process_identity(
    tmp_path: Path,
    status: ExecutionStatus,
) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    execution = Execution(
        id=f"missing-identity-{status.value}",
        execution_key=f"missing-identity-{status.value}-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"missing-identity-{status.value}.stdout"),
        stderr_path=str(tmp_path / f"missing-identity-{status.value}.stderr"),
        status=status,
    )
    await executions.create_if_absent(execution)

    with pytest.raises(ProcessTerminationError, match="process identity"):
        await supervisor.cancel(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is status
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize("started_field", ["started_at", "process_created_at"])
async def test_supervisor_does_not_treat_ambiguous_failed_execution_as_pre_spawn(
    tmp_path: Path,
    started_field: str,
) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    execution = Execution(
        id=f"failed-with-{started_field}",
        execution_key=f"failed-with-{started_field}-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"failed-with-{started_field}.stdout"),
        stderr_path=str(tmp_path / f"failed-with-{started_field}.stderr"),
        status=ExecutionStatus.FAILED,
        **{started_field: datetime.now(UTC)},
    )
    await executions.create_if_absent(execution)

    with pytest.raises(ProcessTerminationError, match="process identity"):
        await supervisor.cancel(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.FAILED
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize("status", [ExecutionStatus.LOST, ExecutionStatus.FAILED])
async def test_supervisor_best_effort_kill_still_does_not_confirm_without_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: ExecutionStatus,
) -> None:
    database, executions, _ = await make_supervisor(tmp_path)
    execution = Execution(
        id=f"{status.value}-with-matching-process",
        execution_key=f"{status.value}-with-matching-process-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"{status.value}-with-matching-process.stdout"),
        stderr_path=str(tmp_path / f"{status.value}-with-matching-process.stderr"),
        status=status,
        pid=424243,
        process_group_id=424244,
    )
    await executions.create_if_absent(execution)
    terminated: list[int | None] = []

    async def record_termination(
        process_group_id: int | None,
        *,
        grace_seconds: float,
    ) -> None:
        terminated.append(process_group_id)

    monkeypatch.setattr(
        "riftx.runner.supervisor._terminate_detached_process",
        record_termination,
    )
    inspector = MatchThenMissingInspector()
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        inspector=inspector,  # type: ignore[arg-type]
        termination_grace_seconds=0.01,
    )

    with pytest.raises(ProcessTerminationError, match="no durable kernel containment"):
        await supervisor.cancel(execution.id)

    assert terminated == [execution.process_group_id]
    assert inspector.calls == 2
    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is status
    await supervisor.close()
    await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
async def test_cancel_reaudits_historic_cancelled_record_with_live_group(
    tmp_path: Path,
) -> None:
    database, executions, first_supervisor = await make_supervisor(tmp_path)
    heartbeat = tmp_path / "historic-cancelled-heartbeat"
    started = await first_supervisor.start(
        launch_request(
            tmp_path,
            "historic-cancelled-key",
            "stubborn-child",
            "--seconds",
            "30",
            "--heartbeat",
            str(heartbeat),
        )
    )
    detached_supervisor: ProcessSupervisor | None = None
    try:
        assert started.pid is not None
        await asyncio.to_thread(wait_for_nonempty_file, Path(started.stdout_path))
        await asyncio.to_thread(wait_for_nonempty_file, heartbeat)
        output = await first_supervisor.read_output(started.id)
        child_pid = int(output.stdout.data.decode().strip().splitlines()[0])
        await first_supervisor.close(cancel_running=False)

        historic = await executions.get(started.id)
        assert historic is not None
        historic.transition_to(ExecutionStatus.CANCELLED)
        await executions.save(historic)
        assert process_exists(started.pid)
        assert process_exists(child_pid)

        detached_supervisor = ProcessSupervisor(
            executions,
            RunnerPaths(tmp_path / "historic-cancelled-state"),
            termination_grace_seconds=0.1,
        )
        safety = RunSafetyStopService(
            execution_repository=executions,
            execution_runner=detached_supervisor,
            execution_cancel_timeout_seconds=1.0,
            execution_cancel_poll_seconds=0.01,
            require_all_resource_stoppers=False,
        )
        stop_result = await safety.stop_run("run-1")

        execution_stop = stop_result.resources["executions"]
        assert execution_stop.succeeded is True
        assert execution_stop.attempted_ids == (started.id,)
        assert execution_stop.confirmed_statuses == {started.id: ExecutionStatus.CANCELLED.value}
        await asyncio.to_thread(wait_for_process_exit, started.pid)
        await asyncio.to_thread(wait_for_process_exit, child_pid)
    finally:
        kill_process_group(started.process_group_id)
        await first_supervisor.close(cancel_running=False)
        if detached_supervisor is not None:
            await detached_supervisor.close()
        await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
async def test_detached_cancel_terminates_orphaned_group_after_leader_exits(
    tmp_path: Path,
) -> None:
    database, executions, first_supervisor = await make_supervisor(tmp_path)
    heartbeat = tmp_path / "orphaned-group-heartbeat"
    started = await first_supervisor.start(
        launch_request(
            tmp_path,
            "orphaned-group-key",
            "stubborn-child",
            "--seconds",
            "0.05",
            "--heartbeat",
            str(heartbeat),
        )
    )
    detached_supervisor: ProcessSupervisor | None = None
    try:
        assert started.pid is not None
        await asyncio.to_thread(wait_for_nonempty_file, Path(started.stdout_path))
        await asyncio.to_thread(wait_for_nonempty_file, heartbeat)
        output = await first_supervisor.read_output(started.id)
        child_pid = int(output.stdout.data.decode().strip().splitlines()[0])
        await asyncio.to_thread(wait_for_process_exit, started.pid)

        while_leader_absent = await executions.get(started.id)
        assert while_leader_absent is not None
        assert while_leader_absent.status is ExecutionStatus.RUNNING
        assert process_exists(child_pid)

        await first_supervisor.close(cancel_running=False)
        detached_supervisor = ProcessSupervisor(
            executions,
            RunnerPaths(tmp_path / "orphaned-group-state"),
            termination_grace_seconds=0.1,
        )

        cancelled = await detached_supervisor.cancel(started.id)

        assert cancelled.status is ExecutionStatus.CANCELLED
        await asyncio.to_thread(wait_for_process_exit, child_pid)
    finally:
        kill_process_group(started.process_group_id)
        await first_supervisor.close(cancel_running=False)
        if detached_supervisor is not None:
            await detached_supervisor.close()
        await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
async def test_detached_shell_exec_replacement_is_cancelled_by_new_supervisor(
    tmp_path: Path,
) -> None:
    database, executions, first_supervisor = await make_supervisor(tmp_path)
    started = await first_supervisor.start(
        shell_launch_request(
            tmp_path,
            "detached-shell-exec-key",
            script="sleep 30",
        )
    )
    detached_supervisor: ProcessSupervisor | None = None
    try:
        assert started.pid is not None
        inspector = ProcessInspector()
        deadline = asyncio.get_running_loop().time() + 2.0
        identity = await inspector.inspect(started.pid)
        while (
            identity is None
            or identity.command is None
            or Path(identity.command.split(maxsplit=1)[0]).name != "sleep"
        ):
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("shell did not exec-replace itself with sleep")
            await asyncio.sleep(0.01)
            identity = await inspector.inspect(started.pid)

        assert identity.process_group_id == started.process_group_id
        assert await inspector.matches(started) is True

        await first_supervisor.close(cancel_running=False)
        detached_supervisor = ProcessSupervisor(
            executions,
            RunnerPaths(tmp_path / "state"),
            termination_grace_seconds=0.1,
        )

        cancelled = await detached_supervisor.cancel(started.id)

        assert cancelled.status is ExecutionStatus.CANCELLED
        await asyncio.to_thread(wait_for_process_exit, started.pid)
    finally:
        kill_process_group(started.process_group_id)
        await first_supervisor.close(cancel_running=False)
        if detached_supervisor is not None:
            await detached_supervisor.close()
        await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
async def test_detached_cancel_terminates_and_verifies_real_process_group(
    tmp_path: Path,
) -> None:
    database, executions, first_supervisor = await make_supervisor(tmp_path)
    heartbeat = tmp_path / "child-heartbeat"
    started = await first_supervisor.start(
        launch_request(
            tmp_path,
            "detached-group-key",
            "stubborn-child",
            "--seconds",
            "30",
            "--heartbeat",
            str(heartbeat),
        )
    )
    await asyncio.to_thread(wait_for_nonempty_file, Path(started.stdout_path))
    await asyncio.to_thread(wait_for_nonempty_file, heartbeat)
    output = await first_supervisor.read_output(started.id)
    child_pid = int(output.stdout.data.decode().strip().splitlines()[0])
    assert started.pid is not None

    await first_supervisor.close()
    detached_supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        termination_grace_seconds=0.1,
    )

    try:
        cancelled = await detached_supervisor.cancel(started.id)

        assert cancelled.status is ExecutionStatus.CANCELLED
        await asyncio.to_thread(wait_for_process_exit, started.pid)
        await asyncio.to_thread(wait_for_process_exit, child_pid)
    finally:
        kill_process_group(started.process_group_id)
        await detached_supervisor.close()
        await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
async def test_detached_cancel_does_not_treat_live_group_as_process_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, executions, _ = await make_supervisor(tmp_path)
    execution = Execution(
        id="identity-mismatch-live-group",
        execution_key="identity-mismatch-live-group-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "identity-mismatch.stdout"),
        stderr_path=str(tmp_path / "identity-mismatch.stderr"),
        status=ExecutionStatus.RUNNING,
        pid=424247,
        process_group_id=424247,
        process_created_at=datetime.now(UTC),
    )
    await executions.create_if_absent(execution)
    monkeypatch.setattr(
        "riftx.runner.supervisor._posix_process_group_exists",
        lambda process_group_id: process_group_id == execution.process_group_id,
    )
    monkeypatch.setattr(
        "riftx.runner.supervisor._pid_exists",
        lambda process_id: process_id == execution.pid,
    )
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "live-group-state"),
        inspector=NeverMatchingInspector(),  # type: ignore[arg-type]
    )

    with pytest.raises(ProcessTerminationError, match="still alive"):
        await supervisor.cancel(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.RUNNING
    await supervisor.close()
    await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
async def test_detached_cancel_requires_process_group_identity_for_absence(
    tmp_path: Path,
) -> None:
    database, executions, initial_supervisor = await make_supervisor(tmp_path)
    execution = Execution(
        id="missing-process-group",
        execution_key="missing-process-group-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "missing-process-group.stdout"),
        stderr_path=str(tmp_path / "missing-process-group.stderr"),
        status=ExecutionStatus.RUNNING,
        pid=424248,
        process_created_at=datetime.now(UTC),
    )
    await executions.create_if_absent(execution)
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "missing-process-group-state"),
        inspector=NeverMatchingInspector(),  # type: ignore[arg-type]
    )

    with pytest.raises(ProcessTerminationError, match="process group identity is missing"):
        await supervisor.cancel(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.RUNNING
    await initial_supervisor.close()
    await supervisor.close()
    await database.dispose()


async def test_non_posix_detached_leader_absence_is_not_tree_stop_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, executions, _ = await make_supervisor(tmp_path)
    execution = Execution(
        id="windows-detached-unknown-tree",
        execution_key="windows-detached-unknown-tree-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=["powershell.exe", "-Command", "Start-Sleep 30"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "windows-detached.stdout"),
        stderr_path=str(tmp_path / "windows-detached.stderr"),
        status=ExecutionStatus.EXITED,
        pid=424248,
        process_group_id=424248,
        process_created_at=datetime.now(UTC),
    )
    await executions.create_if_absent(execution)
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "windows-detached-state"),
        inspector=NeverMatchingInspector(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "riftx.runner.supervisor._supports_posix_process_groups",
        lambda: False,
    )

    with pytest.raises(ProcessTerminationError, match="kernel-owned process-tree identity"):
        await supervisor.cancel(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.EXITED
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize(
    "status",
    [
        ExecutionStatus.STARTING,
        ExecutionStatus.RUNNING,
        ExecutionStatus.FAILED,
        ExecutionStatus.LOST,
    ],
)
async def test_detached_cancel_does_not_claim_cancelled_when_process_still_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: ExecutionStatus,
) -> None:
    database, executions, _ = await make_supervisor(tmp_path)
    execution = Execution(
        id=f"unconfirmed-{status.value}",
        execution_key=f"unconfirmed-{status.value}-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"unconfirmed-{status.value}.stdout"),
        stderr_path=str(tmp_path / f"unconfirmed-{status.value}.stderr"),
        status=status,
        pid=424242,
        process_group_id=424242,
    )
    await executions.create_if_absent(execution)

    async def leave_process_running(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        "riftx.runner.supervisor._terminate_detached_process",
        leave_process_running,
    )
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        inspector=AlwaysMatchingInspector(),  # type: ignore[arg-type]
        termination_grace_seconds=0.01,
    )

    with pytest.raises(ProcessTerminationError, match=execution.id):
        await supervisor.cancel(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is status
    await supervisor.close()
    await database.dispose()


async def test_failed_detached_cancel_keeps_failed_when_inspection_fails(
    tmp_path: Path,
) -> None:
    database, executions, _ = await make_supervisor(tmp_path)
    execution = Execution(
        id="failed-inspection-error",
        execution_key="failed-inspection-error-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "failed-inspection-error.stdout"),
        stderr_path=str(tmp_path / "failed-inspection-error.stderr"),
        status=ExecutionStatus.FAILED,
        pid=424245,
        process_group_id=424245,
    )
    await executions.create_if_absent(execution)
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        inspector=FailingInspector(),  # type: ignore[arg-type]
    )

    with pytest.raises(ProcessTerminationError, match="Could not verify"):
        await supervisor.cancel(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.FAILED
    await supervisor.close()
    await database.dispose()


async def test_failed_detached_cancel_keeps_failed_when_termination_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, executions, _ = await make_supervisor(tmp_path)
    execution = Execution(
        id="failed-termination-error",
        execution_key="failed-termination-error-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "failed-termination-error.stdout"),
        stderr_path=str(tmp_path / "failed-termination-error.stderr"),
        status=ExecutionStatus.FAILED,
        pid=424246,
        process_group_id=424246,
    )
    await executions.create_if_absent(execution)

    async def fail_termination(*_: object, **__: object) -> None:
        raise RuntimeError("termination failed")

    monkeypatch.setattr(
        "riftx.runner.supervisor._terminate_detached_process",
        fail_termination,
    )
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        inspector=AlwaysMatchingInspector(),  # type: ignore[arg-type]
    )

    with pytest.raises(ProcessTerminationError, match="Failed to terminate"):
        await supervisor.cancel(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.FAILED
    await supervisor.close()
    await database.dispose()


async def test_large_output_can_be_resumed_without_rereading(tmp_path: Path) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(launch_request(tmp_path, "large-key", "large"))
    await supervisor.wait(started.id)

    cursor = 0
    chunks: list[bytes] = []
    while True:
        output = await supervisor.read_output(started.id, stdout_cursor=cursor, max_bytes=16_384)
        chunks.append(output.stdout.data)
        cursor = output.stdout.next_cursor
        if output.stdout.eof:
            break

    assert cursor == 200_000
    assert sum(map(len, chunks)) == 200_000
    assert set(b"".join(chunks)) == {ord("x")}
    await supervisor.close()
    await database.dispose()


async def test_recovery_marks_unidentifiable_active_execution_lost(tmp_path: Path) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    execution = Execution(
        id="orphaned",
        execution_key="orphaned-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "missing.py"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "orphaned.stdout"),
        stderr_path=str(tmp_path / "orphaned.stderr"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    await executions.create_if_absent(execution)

    recovered = await supervisor.recover()

    assert len(recovered) == 1
    assert recovered[0].status is ExecutionStatus.LOST
    persisted = await executions.get("orphaned")
    assert persisted is not None
    assert persisted.status is ExecutionStatus.LOST
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize("operation", ["recover", "reconcile"])
async def test_late_recovery_absence_does_not_overwrite_cancelled_execution(
    tmp_path: Path,
    operation: str,
) -> None:
    database, executions, original_supervisor = await make_supervisor(tmp_path)
    await original_supervisor.close()
    execution = Execution(
        id=f"late-{operation}",
        execution_key=f"late-{operation}-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "missing.py"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"late-{operation}.stdout"),
        stderr_path=str(tmp_path / f"late-{operation}.stderr"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    await executions.create_if_absent(execution)
    inspector = BlockingNeverMatchingInspector()
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        inspector=inspector,  # type: ignore[arg-type]
    )

    if operation == "recover":
        pending = asyncio.create_task(supervisor.recover())
    else:
        pending = asyncio.create_task(supervisor.reconcile(execution.id))
    await inspector.entered.wait()

    current = await executions.get(execution.id)
    assert current is not None
    current.transition_to(ExecutionStatus.CANCELLED)
    current, saved = await executions.save_if_status(
        current,
        expected={ExecutionStatus.STARTING},
    )
    assert saved is True
    inspector.release.set()

    result = await pending
    reconciled = result[0] if operation == "recover" else result
    assert reconciled.status is ExecutionStatus.CANCELLED
    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    await supervisor.close()
    await database.dispose()


async def test_recovery_skips_execution_that_has_not_started(tmp_path: Path) -> None:
    database, executions, original_supervisor = await make_supervisor(tmp_path)
    await original_supervisor.close()
    execution = Execution(
        id="created-not-started",
        execution_key="created-not-started-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "missing.py"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "created-not-started.stdout"),
        stderr_path=str(tmp_path / "created-not-started.stderr"),
    )
    await executions.create_if_absent(execution)
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        inspector=NeverMatchingInspector(),  # type: ignore[arg-type]
    )

    assert await supervisor.recover() == []
    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CREATED
    await supervisor.close()
    await database.dispose()


async def test_completed_output_survives_supervisor_and_database_restart(
    tmp_path: Path,
) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(launch_request(tmp_path, "restart-key", "success"))
    completed = await supervisor.wait(started.id)
    await supervisor.close()
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    reopened_supervisor = ProcessSupervisor(
        SQLAlchemyExecutionRepository(reopened.session_factory),
        RunnerPaths(tmp_path / "state"),
    )
    restored = await reopened_supervisor.get(completed.id)
    output = await reopened_supervisor.read_output(completed.id)

    assert restored.status is ExecutionStatus.EXITED
    assert restored.exit_code == 0
    assert b"supervised" in output.stdout.data
    await reopened_supervisor.close()
    await reopened.dispose()
