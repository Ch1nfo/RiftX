from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from riftx.domain import (
    Execution,
    ExecutionStatus,
    ExecutorType,
    RunKind,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandOwnership,
    RunnerCommandOwnershipState,
    RunnerCommandStatus,
    RunnerEffectBinding,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
    runner_payload_digest,
)
from riftx.runner.paths import RunnerPaths
from riftx.runner.remote import RemoteExecutionSupervisor
from riftx.runner.state import FileExecutionRepository

_OWNER = RunnerPrincipal(instance_id="runner-instance-a", epoch=1)


class FakeControlService:
    def __init__(self, *, local_execution_id: str | None = None) -> None:
        self.enqueued: list[tuple[str, RunnerCommandKind, str, dict[str, object]]] = []
        self.commands: dict[str, RunnerCommand] = {}
        self.waited: list[str] = []
        self.local_execution_id = local_execution_id

    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
        run_id: str,
        origin: RunnerCommandOrigin,
        operation_family: RunnerOperationFamily,
        resource_kind: RunnerResourceKind,
        resource_id: str,
        execution_id: str | None = None,
        output_contract: RunnerOutputContract | None = None,
        target: RunnerPrincipal | None = None,
    ) -> tuple[RunnerCommand, bool]:
        self.enqueued.append((node_id, kind, idempotency_key, payload))
        assert target is not None
        command_id = f"command-{len(self.enqueued)}"
        binding = RunnerEffectBinding(
            id=f"binding-{len(self.enqueued)}",
            run_id=run_id,
            run_kind=RunKind.GENERAL,
            node_id=node_id,
            target=target,
            origin=origin,
            operation_family=operation_family,
            execution_id=execution_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        ownership = RunnerCommandOwnership(
            command_id=command_id,
            effect_binding=binding,
            operation=kind,
            operation_family=operation_family,
            payload_digest=runner_payload_digest(payload),
            output_contract=output_contract or RunnerOutputContract(),
        )
        command = RunnerCommand(
            id=command_id,
            node_id=node_id,
            target=target,
            kind=kind,
            idempotency_key=idempotency_key,
            ownership=ownership,
            ownership_state=RunnerCommandOwnershipState.VERIFIED,
            quarantine_reason="",
            payload=payload,
        )
        self.commands[command.id] = command
        return command, True

    async def wait_command(
        self,
        command_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> RunnerCommand:
        del timeout_seconds, poll_interval_seconds
        self.waited.append(command_id)
        command = self.commands[command_id]
        return command.model_copy(
            update={
                "status": RunnerCommandStatus.COMPLETED,
                "result": {
                    "execution_id": command.payload["execution_id"],
                    "local_execution_id": (
                        self.local_execution_id or command.payload["execution_id"]
                    ),
                    "execution_key": command.payload["execution_key"],
                    "owner": (
                        command.target.model_dump(mode="json")
                        if command.target is not None
                        else None
                    ),
                    "status": ExecutionStatus.CANCELLED.value,
                    "physical_stop_confirmed": True,
                },
            }
        )


def make_execution(tmp_path: Path, status: ExecutionStatus) -> Execution:
    execution = Execution(
        id=f"execution-{status.value}",
        execution_key=f"key-{status.value}",
        run_id="run-1",
        node_id="remote-node",
        owner=_OWNER,
        executor_type=ExecutorType.PROCESS,
        argv=["sleep", "30"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"{status.value}.stdout"),
        stderr_path=str(tmp_path / f"{status.value}.stderr"),
    )
    if status is ExecutionStatus.CANCELLED:
        execution.transition_to(status)
        return execution
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    if status is not ExecutionStatus.RUNNING:
        execution.transition_to(status)
    return execution


@pytest.mark.parametrize(
    ("status", "expected_status"),
    [
        (ExecutionStatus.LOST, ExecutionStatus.CANCELLED),
        (ExecutionStatus.FAILED, ExecutionStatus.CANCELLED),
        (ExecutionStatus.COMPLETED, ExecutionStatus.COMPLETED),
        (ExecutionStatus.EXITED, ExecutionStatus.EXITED),
        (ExecutionStatus.HARD_TIMEOUT, ExecutionStatus.HARD_TIMEOUT),
    ],
)
async def test_terminal_remote_execution_still_gets_cancel_tombstone(
    tmp_path: Path,
    status: ExecutionStatus,
    expected_status: ExecutionStatus,
) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    execution = make_execution(tmp_path, status)
    await repository.create_if_absent(execution)
    control = FakeControlService()
    supervisor = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / "runner"),
        control,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    returned = await supervisor.cancel(execution.id)

    assert returned.status is expected_status
    assert returned.physical_stop_confirmed_at is not None
    persisted = await repository.get(execution.id)
    assert persisted is not None
    assert persisted.status is expected_status
    assert persisted.physical_stop_confirmed_at is not None
    assert len(control.enqueued) == 1
    node_id, kind, idempotency_key, payload = control.enqueued[0]
    assert node_id == execution.node_id
    assert kind is RunnerCommandKind.CANCEL
    assert idempotency_key.startswith(f"cancel:{execution.id}:")
    assert payload == {
        "execution_id": execution.id,
        "execution_key": execution.execution_key,
    }
    assert len(control.waited) == 1
    persisted_command = control.commands[control.waited[0]]
    assert persisted_command.kind is RunnerCommandKind.CANCEL
    assert persisted_command.target == _OWNER


async def test_cancelled_remote_execution_short_circuits_idempotently(tmp_path: Path) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    execution = make_execution(tmp_path, ExecutionStatus.CANCELLED)
    await repository.create_if_absent(execution)
    control = FakeControlService()
    supervisor = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / "runner"),
        control,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    first = await supervisor.cancel(execution.id)
    second = await supervisor.cancel(execution.id)

    assert first.status is ExecutionStatus.CANCELLED
    assert second.status is ExecutionStatus.CANCELLED
    assert control.enqueued == []


async def test_cancelled_remote_containment_without_proof_requires_owner_ack(
    tmp_path: Path,
) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    execution = make_execution(tmp_path, ExecutionStatus.CANCELLED)
    execution.containment_id = "riftx/terminal/cancelled-unconfirmed"
    await repository.create_if_absent(execution)
    control = FakeControlService()
    supervisor = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / "runner"),
        control,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    returned = await supervisor.cancel(execution.id)

    assert returned.status is ExecutionStatus.CANCELLED
    assert returned.physical_stop_confirmed_at is not None
    assert len(control.enqueued) == 1
    assert control.enqueued[0][1] is RunnerCommandKind.CANCEL


async def test_confirmed_natural_remote_outcome_does_not_repeat_cancel(
    tmp_path: Path,
) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    execution = make_execution(tmp_path, ExecutionStatus.EXITED)
    execution.physical_stop_confirmed_at = datetime.now(UTC)
    await repository.create_if_absent(execution)
    control = FakeControlService()
    supervisor = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / "runner"),
        control,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    returned = await supervisor.cancel(execution.id)

    assert returned.status is ExecutionStatus.EXITED
    assert returned.physical_stop_confirmed_at is not None
    assert control.enqueued == []


async def test_remote_cancel_rejects_mismatched_local_execution_ack(tmp_path: Path) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    execution = make_execution(tmp_path, ExecutionStatus.RUNNING)
    await repository.create_if_absent(execution)
    control = FakeControlService(local_execution_id="different-local-execution")
    supervisor = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / "runner"),
        control,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="local execution ID mismatch"):
        await supervisor.cancel(execution.id)
