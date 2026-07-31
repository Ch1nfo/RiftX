from __future__ import annotations

import asyncio
import shlex
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from riftx.domain import (
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Node,
    NodeStatus,
    Objective,
    Run,
)
from riftx.execution import ExecutionReconciler
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyNodeRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import (
    ProcessIdentity,
    ProcessInspector,
)


class IdentityInspector(ProcessInspector):
    def __init__(self, identities: dict[int, ProcessIdentity | None]) -> None:
        self.identities = identities
        self.calls: list[int] = []

    async def inspect(self, pid: int) -> ProcessIdentity | None:
        self.calls.append(pid)
        return self.identities.get(pid)


class ConcurrentInspector(ProcessInspector):
    def __init__(self, outcomes: dict[int, bool]) -> None:
        self.outcomes = outcomes
        self.active = 0
        self.max_active = 0

    async def matches(self, execution: Execution) -> bool:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return self.outcomes.get(execution.pid or 0, False)
        finally:
            self.active -= 1


class BlockingMissingInspector(ProcessInspector):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def matches(self, execution: Execution) -> bool:
        self.entered.set()
        await self.release.wait()
        return False


async def build_database(
    tmp_path: Path,
) -> tuple[
    Database,
    SQLAlchemyExecutionRepository,
    SQLAlchemyNodeRepository,
    SQLAlchemyRunEventRepository,
]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'reconcile.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Reconcile durable processes"),
            workspace_path=str(tmp_path),
        )
    )
    return (
        database,
        SQLAlchemyExecutionRepository(database.session_factory),
        SQLAlchemyNodeRepository(database.session_factory),
        SQLAlchemyRunEventRepository(database.session_factory),
    )


def running_execution(
    tmp_path: Path,
    execution_id: str,
    *,
    pid: int,
    node_id: str = "local",
    executor_type: ExecutorType = ExecutorType.PROCESS,
    created_at: datetime | None = None,
) -> Execution:
    execution = Execution(
        id=execution_id,
        execution_key=f"key-{execution_id}",
        run_id="run-1",
        node_id=node_id,
        executor_type=executor_type,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"{execution_id}.stdout"),
        stderr_path=str(tmp_path / f"{execution_id}.stderr"),
        pid=pid,
        process_group_id=pid,
        process_created_at=created_at or datetime.now(UTC),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    return execution


async def test_runner_restart_reassociates_matching_live_process(tmp_path: Path) -> None:
    database, executions, nodes, events = await build_database(tmp_path)
    execution = running_execution(tmp_path, "restart", pid=3131)
    await executions.create_if_absent(execution)
    inspector = IdentityInspector(
        {
            3131: ProcessIdentity(
                pid=3131,
                created_at=execution.process_created_at,
                command=shlex.join(execution.argv),
                process_group_id=3131,
            )
        }
    )

    reconciled = await ExecutionReconciler(
        execution_repository=executions,
        local_node_id="local",
        process_inspector=inspector,
        node_repository=nodes,
        event_repository=events,
    ).reconcile_execution(execution.id)

    assert reconciled.status is ExecutionStatus.RUNNING
    assert inspector.calls == [3131]
    await database.dispose()


async def test_reused_pid_with_different_creation_time_becomes_lost(tmp_path: Path) -> None:
    database, executions, nodes, events = await build_database(tmp_path)
    execution = running_execution(tmp_path, "reused", pid=4242)
    await executions.create_if_absent(execution)
    inspector = IdentityInspector(
        {
            4242: ProcessIdentity(
                pid=4242,
                created_at=execution.process_created_at + timedelta(minutes=1),
                command=" ".join(execution.argv),
                process_group_id=4242,
            )
        }
    )

    reconciled = await ExecutionReconciler(
        execution_repository=executions,
        local_node_id="local",
        process_inspector=inspector,
        node_repository=nodes,
        event_repository=events,
    ).reconcile_execution(execution.id)

    assert reconciled.status is ExecutionStatus.LOST
    assert inspector.calls == [4242]
    await database.dispose()


async def test_missing_process_becomes_lost(tmp_path: Path) -> None:
    database, executions, nodes, _ = await build_database(tmp_path)
    execution = running_execution(tmp_path, "missing", pid=5252)
    await executions.create_if_absent(execution)

    reconciled = await ExecutionReconciler(
        execution_repository=executions,
        local_node_id="local",
        process_inspector=IdentityInspector({5252: None}),
        node_repository=nodes,
    ).reconcile_execution(execution.id)

    assert reconciled.status is ExecutionStatus.LOST
    await database.dispose()


async def test_late_missing_verdict_does_not_overwrite_cancelled_execution(
    tmp_path: Path,
) -> None:
    database, executions, nodes, _ = await build_database(tmp_path)
    execution = running_execution(tmp_path, "cancel-wins", pid=5353)
    await executions.create_if_absent(execution)
    inspector = BlockingMissingInspector()
    reconciler = ExecutionReconciler(
        execution_repository=executions,
        local_node_id="local",
        process_inspector=inspector,
        node_repository=nodes,
    )

    pending = asyncio.create_task(reconciler.reconcile_execution(execution.id))
    await inspector.entered.wait()
    current = await executions.get(execution.id)
    assert current is not None
    current.transition_to(ExecutionStatus.CANCELLED)
    current, saved = await executions.save_if_status(
        current,
        expected={ExecutionStatus.RUNNING},
    )
    assert saved is True
    inspector.release.set()

    reconciled = await pending
    assert reconciled.status is ExecutionStatus.CANCELLED
    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    await database.dispose()


async def test_completed_execution_is_never_modified_or_inspected(tmp_path: Path) -> None:
    database, executions, nodes, _ = await build_database(tmp_path)
    execution = running_execution(tmp_path, "completed", pid=6262)
    execution.transition_to(ExecutionStatus.COMPLETED, exit_code=0)
    await executions.create_if_absent(execution)
    inspector = IdentityInspector({})

    reconciled = await ExecutionReconciler(
        execution_repository=executions,
        local_node_id="local",
        process_inspector=inspector,
        node_repository=nodes,
    ).reconcile_execution(execution.id)

    assert reconciled.status is ExecutionStatus.COMPLETED
    assert reconciled.exit_code == 0
    assert inspector.calls == []
    await database.dispose()


async def test_remote_runner_offline_marks_active_execution_lost(tmp_path: Path) -> None:
    database, executions, nodes, _ = await build_database(tmp_path)
    await nodes.create(
        Node(
            id="remote-1",
            name="Remote",
            platform="linux",
            architecture="x86_64",
            status=NodeStatus.OFFLINE,
        )
    )
    execution = running_execution(tmp_path, "remote", pid=7272, node_id="remote-1")
    await executions.create_if_absent(execution)

    reconciled = await ExecutionReconciler(
        execution_repository=executions,
        local_node_id="local",
        node_repository=nodes,
    ).reconcile_execution(execution.id)

    assert reconciled.status is ExecutionStatus.LOST
    await database.dispose()


async def test_reconcile_run_handles_multiple_executions_concurrently(tmp_path: Path) -> None:
    database, executions, nodes, _ = await build_database(tmp_path)
    items = [
        running_execution(tmp_path, "one", pid=8001),
        running_execution(tmp_path, "two", pid=8002),
        running_execution(tmp_path, "three", pid=8003),
    ]
    for item in items:
        await executions.create_if_absent(item)
    inspector = ConcurrentInspector({8001: True, 8002: False, 8003: True})

    reconciled = await ExecutionReconciler(
        execution_repository=executions,
        local_node_id="local",
        process_inspector=inspector,
        node_repository=nodes,
        max_concurrency=3,
    ).reconcile_run("run-1")

    assert {item.id: item.status for item in reconciled} == {
        "one": ExecutionStatus.RUNNING,
        "two": ExecutionStatus.LOST,
        "three": ExecutionStatus.RUNNING,
    }
    assert inspector.max_active > 1
    await database.dispose()


async def test_native_pty_recovery_is_explicitly_deferred(tmp_path: Path) -> None:
    database, executions, nodes, _ = await build_database(tmp_path)
    execution = running_execution(
        tmp_path,
        "pty",
        pid=9001,
        executor_type=ExecutorType.PTY,
    )
    await executions.create_if_absent(execution)
    inspector = IdentityInspector({})

    reconciled = await ExecutionReconciler(
        execution_repository=executions,
        local_node_id="local",
        process_inspector=inspector,
        node_repository=nodes,
    ).reconcile_execution(execution.id)

    assert reconciled.status is ExecutionStatus.RUNNING
    assert inspector.calls == []
    await database.dispose()


async def test_reused_pid_with_different_command_becomes_lost(tmp_path: Path) -> None:
    database, executions, nodes, _ = await build_database(tmp_path)
    execution = running_execution(tmp_path, "wrong-command", pid=4343)
    await executions.create_if_absent(execution)
    inspector = IdentityInspector(
        {
            4343: ProcessIdentity(
                pid=4343,
                created_at=execution.process_created_at,
                command="/usr/bin/other-tool --different-command",
                process_group_id=4343,
            )
        }
    )

    reconciled = await ExecutionReconciler(
        execution_repository=executions,
        local_node_id="local",
        process_inspector=inspector,
        node_repository=nodes,
    ).reconcile_execution(execution.id)

    assert reconciled.status is ExecutionStatus.LOST
    await database.dispose()


async def test_remote_runner_online_keeps_execution_running(tmp_path: Path) -> None:
    database, executions, nodes, _ = await build_database(tmp_path)
    await nodes.create(
        Node(
            id="remote-online",
            name="Remote Online",
            platform="linux",
            architecture="x86_64",
            status=NodeStatus.ONLINE,
        )
    )
    execution = running_execution(
        tmp_path,
        "remote-online-execution",
        pid=7373,
        node_id="remote-online",
    )
    await executions.create_if_absent(execution)

    reconciled = await ExecutionReconciler(
        execution_repository=executions,
        local_node_id="local",
        node_repository=nodes,
    ).reconcile_execution(execution.id)

    assert reconciled.status is ExecutionStatus.RUNNING
    await database.dispose()
