from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from riftx.application.errors import ApplicationConflictError, RepositoryConflictError
from riftx.application.services import (
    NodeApplicationService,
    NodeRegistration,
    RunnerControlService,
)
from riftx.application.services.runner_control import ExecutionStatusReport
from riftx.domain import (
    RUNNER_COMMAND_OWNERSHIP_CAPABILITY,
    RUNNER_STOP_ACK_BROWSER_SCHEMA,
    RUNNER_STOP_ACK_EXECUTION_SCHEMA,
    RUNNER_STOP_ACK_TARGET_HTTP_SCHEMA,
    RUNNER_STOP_ACK_TERMINAL_SCHEMA,
    BrowserMode,
    BrowserSession,
    BrowserSessionStatus,
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
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
    RunnerStopReceipt,
    TerminalSession,
    TerminalStatus,
    runner_payload_digest,
    runner_stop_ack_digest,
)
from riftx.domain.base import new_id, utc_now
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyBrowserRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyNodeRepository,
    SQLAlchemyRunnerCommandRepository,
    SQLAlchemyRunnerCredentialRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
    SQLAlchemyToolCallIntentRepository,
)
from riftx.persistence.mappers import (
    runner_command_ownership_to_record,
    runner_command_to_record,
)
from riftx.persistence.orm import RunnerCommandRecord
from riftx.persistence.workflow_signals import WorkflowSignalIntentRecord
from riftx.runner.paths import RunnerPaths
from riftx.runtime import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    ToolCallIntent,
    ToolCallStatus,
)

_PRINCIPAL_A = RunnerPrincipal(instance_id="runner-instance-a", epoch=1)
_PRINCIPAL_B = RunnerPrincipal(instance_id="runner-instance-b", epoch=2)
_RUN_ID = "runner-control-run"


async def _seed_authority(database: Database, nodes: NodeApplicationService) -> None:
    await nodes.register(
        NodeRegistration(
            node_id="runner-a",
            name="Runner A",
            platform="linux",
            architecture="x86_64",
        )
    )
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="runner-control-engagement", name="Runner control")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id=_RUN_ID,
            engagement_id="runner-control-engagement",
            node_id="runner-a",
            objective=Objective(description="Exercise Runner ownership"),
            kind=RunKind.GENERAL,
            workspace_path="/tmp/runner-control",
            temporal_workflow_id="riftx-run-runner-control",
        )
    )
    credential = await SQLAlchemyRunnerCredentialRepository(database.session_factory).issue(
        "runner-a",
        token_hash="a" * 64,
        token_prefix="token-a",
        issued_at=utc_now(),
        instance_id=_PRINCIPAL_A.instance_id,
        protocol_capabilities=(RUNNER_COMMAND_OWNERSHIP_CAPABILITY,),
    )
    assert credential.principal == _PRINCIPAL_A


async def _seed_runtime_hierarchy(
    database: Database,
) -> tuple[AgentSession, AgentCycle, AgentStep]:
    session = AgentSession(
        id="runner-control-session",
        run_id=_RUN_ID,
        model_profile="runner-control-test",
    )
    cycle = AgentCycle(
        id="runner-control-cycle",
        run_id=_RUN_ID,
        session_id=session.id,
        sequence=1,
    )
    step = AgentStep(
        id="runner-control-step",
        cycle_id=cycle.id,
        sequence=1,
        step_type=AgentStepType.TOOL_EXECUTION,
    )
    await SQLAlchemyAgentSessionRepository(database.session_factory).create(session)
    await SQLAlchemyAgentCycleRepository(database.session_factory).create(cycle)
    await SQLAlchemyAgentStepRepository(database.session_factory).create(step)
    return session, cycle, step


async def _workflow_signal_intent_count(database: Database) -> int:
    async with database.session_factory() as session:
        return len((await session.scalars(select(WorkflowSignalIntentRecord.id))).all())


def _verified_command(
    *,
    kind: RunnerCommandKind,
    idempotency_key: str,
    created_at: datetime,
) -> RunnerCommand:
    command_id = new_id()
    resource_id = f"resource-{kind.value}"
    family = (
        RunnerOperationFamily.SAFETY_STOP
        if kind
        in {
            RunnerCommandKind.TARGET_HTTP_CANCEL,
            RunnerCommandKind.BROWSER_CLOSE,
            RunnerCommandKind.TERMINAL_CLOSE,
        }
        else RunnerOperationFamily.TARGET_HTTP
    )
    if kind in {RunnerCommandKind.TARGET_HTTP, RunnerCommandKind.TARGET_HTTP_CANCEL}:
        resource_kind = RunnerResourceKind.TARGET_HTTP_INTENT
        payload = (
            {
                "run_id": _RUN_ID,
                "tool_call_ids": [resource_id],
            }
            if family is RunnerOperationFamily.SAFETY_STOP
            else {
                "launch": {
                    "run_id": _RUN_ID,
                    "node_id": "runner-a",
                    "tool_call_id": resource_id,
                }
            }
        )
        stop_schema = (
            RUNNER_STOP_ACK_TARGET_HTTP_SCHEMA
            if family is RunnerOperationFamily.SAFETY_STOP
            else None
        )
        execution_id = None
    elif kind is RunnerCommandKind.BROWSER_CLOSE:
        resource_kind = RunnerResourceKind.BROWSER_SESSION
        payload = {
            "operation": "close",
            "command": {
                "session_id": resource_id,
                "run_id": _RUN_ID,
                "node_id": "runner-a",
            },
        }
        stop_schema = RUNNER_STOP_ACK_BROWSER_SCHEMA
        execution_id = None
    elif kind is RunnerCommandKind.TERMINAL_CLOSE:
        resource_kind = RunnerResourceKind.TERMINAL_SESSION
        execution_id = f"execution-{kind.value}"
        payload = {
            "session_id": resource_id,
            "execution_id": execution_id,
            "execution_key": f"terminal:{resource_id}",
        }
        stop_schema = RUNNER_STOP_ACK_TERMINAL_SCHEMA
    else:  # pragma: no cover - helper is intentionally closed over this test matrix
        raise AssertionError(f"unsupported test command {kind}")
    binding = RunnerEffectBinding(
        id=new_id(),
        run_id=_RUN_ID,
        run_kind=RunKind.GENERAL,
        node_id="runner-a",
        target=_PRINCIPAL_A,
        origin=RunnerCommandOrigin.APPLICATION_SERVICE,
        operation_family=family,
        execution_id=execution_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        created_at=created_at,
    )
    ownership = RunnerCommandOwnership(
        command_id=command_id,
        effect_binding=binding,
        operation=kind,
        operation_family=family,
        payload_digest=runner_payload_digest(payload),
        output_contract=RunnerOutputContract(
            max_output_bytes=(
                100_000_000 if kind is RunnerCommandKind.TARGET_HTTP else 0
            ),
            allowed_streams=(
                ("command",) if kind is RunnerCommandKind.TARGET_HTTP else ()
            ),
            result_schema={
                RunnerCommandKind.TARGET_HTTP: "riftx.runner-result/target-http/v1",
                RunnerCommandKind.TARGET_HTTP_CANCEL: (
                    "riftx.runner-result/target-http-stop/v1"
                ),
                RunnerCommandKind.BROWSER_CLOSE: "riftx.runner-result/browser-stop/v1",
                RunnerCommandKind.TERMINAL_CLOSE: "riftx.runner-result/terminal-stop/v1",
            }[kind],
            stop_ack_schema=stop_schema,
        ),
        created_at=created_at,
    )
    return RunnerCommand(
        id=command_id,
        node_id="runner-a",
        target=_PRINCIPAL_A,
        kind=kind,
        idempotency_key=idempotency_key,
        ownership=ownership,
        ownership_state=RunnerCommandOwnershipState.VERIFIED,
        quarantine_reason="",
        payload=payload,
        created_at=created_at,
        updated_at=created_at,
    )


def _verified_execution_stop_command(
    execution: Execution,
    *,
    created_at: datetime,
) -> RunnerCommand:
    command_id = new_id()
    payload = {
        "execution_id": execution.id,
        "execution_key": execution.execution_key,
    }
    binding = RunnerEffectBinding(
        id=new_id(),
        run_id=execution.run_id,
        run_kind=RunKind.GENERAL,
        node_id=execution.node_id,
        target=_PRINCIPAL_A,
        origin=RunnerCommandOrigin.APPLICATION_SERVICE,
        operation_family=RunnerOperationFamily.SAFETY_STOP,
        execution_id=execution.id,
        resource_kind=RunnerResourceKind.EXECUTION,
        resource_id=execution.id,
        created_at=created_at,
    )
    ownership = RunnerCommandOwnership(
        command_id=command_id,
        effect_binding=binding,
        operation=RunnerCommandKind.CANCEL,
        operation_family=RunnerOperationFamily.SAFETY_STOP,
        payload_digest=runner_payload_digest(payload),
        output_contract=RunnerOutputContract(
            result_schema="riftx.runner-result/execution-stop/v1",
            stop_ack_schema=RUNNER_STOP_ACK_EXECUTION_SCHEMA,
        ),
        created_at=created_at,
    )
    return RunnerCommand(
        id=command_id,
        node_id=execution.node_id,
        target=_PRINCIPAL_A,
        kind=RunnerCommandKind.CANCEL,
        idempotency_key=f"execution-stop:{execution.id}",
        ownership=ownership,
        ownership_state=RunnerCommandOwnershipState.VERIFIED,
        quarantine_reason="",
        payload=payload,
        created_at=created_at,
        updated_at=created_at,
    )


def _verified_execution_launch_command(
    execution: Execution,
    *,
    created_at: datetime,
) -> RunnerCommand:
    command_id = new_id()
    payload = {
        "execution_id": execution.id,
        "request": {
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
            "run_id": execution.run_id,
            "node_id": execution.node_id,
            "runner_principal": _PRINCIPAL_A.model_dump(mode="json"),
        },
    }
    binding = RunnerEffectBinding(
        id=new_id(),
        run_id=execution.run_id,
        run_kind=RunKind.GENERAL,
        node_id=execution.node_id,
        target=_PRINCIPAL_A,
        origin=RunnerCommandOrigin.APPLICATION_SERVICE,
        operation_family=RunnerOperationFamily.EXECUTION,
        execution_id=execution.id,
        resource_kind=RunnerResourceKind.EXECUTION,
        resource_id=execution.id,
        created_at=created_at,
    )
    ownership = RunnerCommandOwnership(
        command_id=command_id,
        effect_binding=binding,
        operation=RunnerCommandKind.EXECUTE,
        operation_family=RunnerOperationFamily.EXECUTION,
        payload_digest=runner_payload_digest(payload),
        output_contract=RunnerOutputContract(
            max_output_bytes=100_000_000,
            allowed_streams=("stderr", "stdout"),
            result_schema="riftx.runner-result/execution-start/v1",
        ),
        created_at=created_at,
    )
    return RunnerCommand(
        id=command_id,
        node_id=execution.node_id,
        target=_PRINCIPAL_A,
        kind=RunnerCommandKind.EXECUTE,
        idempotency_key=f"execution-start:{execution.id}",
        ownership=ownership,
        ownership_state=RunnerCommandOwnershipState.VERIFIED,
        quarantine_reason="",
        payload=payload,
        created_at=created_at,
        updated_at=created_at,
    )


def _stop_receipt(
    command: RunnerCommand,
    *,
    ack: dict[str, object],
    operation: RunnerCommandKind | None = None,
    operation_family: RunnerOperationFamily | None = None,
    execution_id: str | None = None,
) -> RunnerStopReceipt:
    assert command.ownership is not None
    binding = command.ownership.effect_binding
    return RunnerStopReceipt(
        id=f"receipt-{command.id}",
        command_id=command.id,
        effect_binding_id=binding.id,
        envelope_digest=command.ownership.envelope_digest,
        binding_digest=binding.binding_digest,
        operation=operation or command.kind,
        operation_family=operation_family or command.ownership.operation_family,
        resource_kind=binding.resource_kind,
        resource_id=binding.resource_id,
        execution_id=execution_id or binding.execution_id,
        node_id=binding.node_id,
        principal=binding.target,
        ack_digest=runner_stop_ack_digest(ack),
    )


def _callback_identity(command: RunnerCommand) -> dict[str, object]:
    assert command.ownership is not None
    return {
        "state_version": command.state_version,
        "envelope_digest": command.ownership.envelope_digest,
        "binding_digest": command.ownership.effect_binding.binding_digest,
    }


def _legacy_quarantined_command(
    *,
    kind: RunnerCommandKind = RunnerCommandKind.CANCEL,
    command_id: str | None = None,
) -> RunnerCommand:
    now = utc_now()
    resolved_id = command_id or f"legacy-{kind.value}"
    return RunnerCommand(
        id=resolved_id,
        node_id="runner-a",
        target=_PRINCIPAL_A,
        kind=kind,
        idempotency_key=resolved_id,
        payload={"untrusted_legacy_resource": "must-not-be-projected"},
        status=RunnerCommandStatus.LEASED,
        attempts=1,
        lease_id=f"lease-{resolved_id}",
        lease_expires_at=now - timedelta(minutes=1),
        result={"legacy_result": "preserved"},
        created_at=now - timedelta(minutes=2),
        updated_at=now - timedelta(minutes=2),
    )


async def _insert_legacy_quarantined_command(
    database: Database,
    command: RunnerCommand,
) -> None:
    async with database.session_factory() as session, session.begin():
        session.add(runner_command_to_record(command))
        await session.flush()
        session.add(runner_command_ownership_to_record(command))


async def _accept_candidate(_command: RunnerCommand) -> None:
    return None


@pytest.mark.asyncio
async def test_legacy_stop_ack_persists_only_isolated_evidence_and_replays_exactly(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy-stop-ack.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    command = _legacy_quarantined_command(command_id="legacy-stop-ack")
    await _insert_legacy_quarantined_command(database, command)
    repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    ack = {
        "execution_id": "runner-local-execution",
        "local_execution_id": "runner-local-execution",
        "execution_key": "runner-local-key",
        "owner": _PRINCIPAL_A.model_dump(mode="json"),
        "status": ExecutionStatus.CANCELLED.value,
        "physical_stop_confirmed": True,
    }

    recorded = await repository.record_legacy_stop_ack(
        command.id,
        principal=_PRINCIPAL_A,
        lease_id=str(command.lease_id),
        expected_state_version=command.state_version,
        ack=ack,
        received_at=utc_now(),
    )

    assert recorded.status is RunnerCommandStatus.LEASED
    assert recorded.lease_id == command.lease_id
    assert recorded.lease_expires_at == command.lease_expires_at
    assert recorded.completed_at is None
    assert recorded.state_version == command.state_version + 1
    assert recorded.result["legacy_result"] == "preserved"
    evidence = recorded.result["_riftx_legacy_stop_ack_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["schema_version"] == "riftx.runner-legacy-stop-ack-evidence/v1"
    assert evidence["principal"] == _PRINCIPAL_A.model_dump(mode="json")
    assert evidence["lease_id"] == command.lease_id
    assert evidence["ack"] == ack
    assert await repository.get_stop_receipt(command.id) is None
    assert [item.id for item in await repository.list_quarantined()] == [command.id]
    assert await _workflow_signal_intent_count(database) == 0

    replay = await repository.record_legacy_stop_ack(
        command.id,
        principal=_PRINCIPAL_A,
        lease_id=str(command.lease_id),
        expected_state_version=command.state_version,
        ack=ack,
        received_at=utc_now() + timedelta(minutes=1),
    )
    assert replay.state_version == recorded.state_version
    assert replay.result == recorded.result

    with pytest.raises(RepositoryConflictError):
        await repository.record_legacy_stop_ack(
            command.id,
            principal=_PRINCIPAL_A,
            lease_id=str(command.lease_id),
            expected_state_version=replay.state_version,
            ack={**ack, "execution_key": "drifted-key"},
            received_at=utc_now(),
        )
    assert await _workflow_signal_intent_count(database) == 0
    await database.dispose()


@pytest.mark.parametrize("recorded_from_state_version", [-1, 0])
@pytest.mark.asyncio
async def test_legacy_stop_ack_rejects_forged_pre_migration_evidence_collision(
    tmp_path: Path,
    recorded_from_state_version: int,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy-forged-ack.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    ack = {
        "execution_id": "runner-local-execution",
        "local_execution_id": "runner-local-execution",
        "execution_key": "runner-local-key",
        "owner": _PRINCIPAL_A.model_dump(mode="json"),
        "status": ExecutionStatus.CANCELLED.value,
        "physical_stop_confirmed": True,
    }
    command = _legacy_quarantined_command(command_id="legacy-forged-ack")
    command.result["_riftx_legacy_stop_ack_evidence"] = {
        "schema_version": "riftx.runner-legacy-stop-ack-evidence/v1",
        "command_id": command.id,
        "node_id": command.node_id,
        "operation": command.kind.value,
        "principal": _PRINCIPAL_A.model_dump(mode="json"),
        "lease_id": command.lease_id,
        "recorded_from_state_version": recorded_from_state_version,
        "ack_digest": runner_stop_ack_digest(ack),
        "ack": ack,
        "received_at": utc_now().isoformat(),
    }
    await _insert_legacy_quarantined_command(database, command)
    repository = SQLAlchemyRunnerCommandRepository(database.session_factory)

    with pytest.raises(RepositoryConflictError):
        await repository.record_legacy_stop_ack(
            command.id,
            principal=_PRINCIPAL_A,
            lease_id=str(command.lease_id),
            expected_state_version=command.state_version,
            ack=ack,
            received_at=utc_now(),
        )

    persisted = await repository.get(command.id)
    assert persisted is not None
    assert persisted.state_version == command.state_version
    assert persisted.result == command.result
    assert await repository.get_stop_receipt(command.id) is None
    assert await _workflow_signal_intent_count(database) == 0
    await database.dispose()


@pytest.mark.parametrize(
    ("kind", "principal", "lease_id"),
    [
        (RunnerCommandKind.EXECUTE, _PRINCIPAL_A, "lease-legacy-rejected"),
        (RunnerCommandKind.CANCEL, _PRINCIPAL_B, "lease-legacy-rejected"),
        (RunnerCommandKind.CANCEL, _PRINCIPAL_A, "wrong-lease"),
    ],
)
@pytest.mark.asyncio
async def test_legacy_stop_ack_repository_rejects_non_legacy_ownership_tuple(
    tmp_path: Path,
    kind: RunnerCommandKind,
    principal: RunnerPrincipal,
    lease_id: str,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'legacy-reject-{kind.value}.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    command = _legacy_quarantined_command(
        kind=kind,
        command_id="legacy-rejected",
    )
    await _insert_legacy_quarantined_command(database, command)
    repository = SQLAlchemyRunnerCommandRepository(database.session_factory)

    with pytest.raises(RepositoryConflictError):
        await repository.record_legacy_stop_ack(
            command.id,
            principal=principal,
            lease_id=lease_id,
            expected_state_version=command.state_version,
            ack={"physical_stop_confirmed": True},
            received_at=utc_now(),
        )

    persisted = await repository.get(command.id)
    assert persisted is not None
    assert persisted.result == {"legacy_result": "preserved"}
    assert persisted.state_version == command.state_version
    assert await repository.get_stop_receipt(command.id) is None
    assert await _workflow_signal_intent_count(database) == 0
    await database.dispose()


@pytest.mark.parametrize(
    (
        "kind",
        "operation_family",
        "origin",
        "resource_kind",
        "payload",
        "expected_code",
    ),
    [
        (
            RunnerCommandKind.TARGET_HTTP_CANCEL,
            RunnerOperationFamily.TARGET_HTTP,
            RunnerCommandOrigin.APPLICATION_SERVICE,
            RunnerResourceKind.TARGET_HTTP_INTENT,
            {"run_id": _RUN_ID, "tool_call_ids": ["target-intent"]},
            "runner_command_family_mismatch",
        ),
        (
            RunnerCommandKind.BROWSER_CLOSE,
            RunnerOperationFamily.BROWSER,
            RunnerCommandOrigin.APPLICATION_SERVICE,
            RunnerResourceKind.BROWSER_SESSION,
            {
                "operation": "close",
                "command": {
                    "session_id": "browser-session",
                    "run_id": _RUN_ID,
                    "node_id": "runner-a",
                },
            },
            "runner_command_family_mismatch",
        ),
        (
            RunnerCommandKind.TERMINAL_CLOSE,
            RunnerOperationFamily.TERMINAL,
            RunnerCommandOrigin.APPLICATION_SERVICE,
            RunnerResourceKind.TERMINAL_SESSION,
            {
                "session_id": "terminal-session",
                "execution_id": "terminal-execution",
                "execution_key": "terminal:terminal-session",
            },
            "runner_command_family_mismatch",
        ),
        (
            RunnerCommandKind.CANCEL,
            RunnerOperationFamily.SAFETY_STOP,
            RunnerCommandOrigin.APPLICATION_SERVICE,
            RunnerResourceKind.TERMINAL_SESSION,
            {
                "session_id": "terminal-session",
                "execution_id": "terminal-execution",
                "execution_key": "terminal:terminal-session",
            },
            "runner_command_resource_kind_mismatch",
        ),
        (
            RunnerCommandKind.TARGET_HTTP,
            RunnerOperationFamily.TARGET_HTTP,
            RunnerCommandOrigin.TEMPORAL_WORKER,
            RunnerResourceKind.TARGET_HTTP_INTENT,
            {
                "launch": {
                    "run_id": _RUN_ID,
                    "node_id": "runner-a",
                    "tool_call_id": "target-intent",
                }
            },
            "runner_command_origin_mismatch",
        ),
        (
            RunnerCommandKind.TARGET_HTTP_CANCEL,
            RunnerOperationFamily.SAFETY_STOP,
            RunnerCommandOrigin.WORKER_RECONCILER,
            RunnerResourceKind.TARGET_HTTP_INTENT,
            {"run_id": _RUN_ID, "tool_call_ids": ["target-intent"]},
            "runner_command_origin_mismatch",
        ),
        (
            RunnerCommandKind.TARGET_HTTP,
            RunnerOperationFamily.TARGET_HTTP,
            RunnerCommandOrigin.APPLICATION_SERVICE,
            RunnerResourceKind.TARGET_HTTP_INTENT,
            {
                "launch": {
                    "run_id": _RUN_ID,
                    "node_id": "runner-a",
                    "tool_call_id": "target-intent",
                }
            },
            "runner_result_contract_mismatch",
        ),
        (
            RunnerCommandKind.BROWSER,
            RunnerOperationFamily.BROWSER,
            RunnerCommandOrigin.APPLICATION_SERVICE,
            RunnerResourceKind.BROWSER_SESSION,
            {
                "operation": "observe",
                "command": {
                    "session_id": "browser-session",
                    "run_id": _RUN_ID,
                    "node_id": "runner-a",
                },
            },
            "runner_result_contract_mismatch",
        ),
    ],
)
@pytest.mark.asyncio
async def test_runner_command_contract_rejects_unregistered_authority_or_output(
    tmp_path: Path,
    kind: RunnerCommandKind,
    operation_family: RunnerOperationFamily,
    origin: RunnerCommandOrigin,
    resource_kind: RunnerResourceKind,
    payload: dict[str, object],
    expected_code: str,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'{kind.value}-contract.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    service = RunnerControlService(
        credentials=SQLAlchemyRunnerCredentialRepository(database.session_factory),
        commands=SQLAlchemyRunnerCommandRepository(database.session_factory),
        nodes=nodes,
        executions=SQLAlchemyExecutionRepository(database.session_factory),
        runs=SQLAlchemyRunRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "runner-state"),
        registration_token=None,
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.enqueue(
            "runner-a",
            kind=kind,
            idempotency_key=f"invalid-stop-contract:{kind.value}",
            payload=payload,
            run_id=_RUN_ID,
            origin=origin,
            operation_family=operation_family,
            resource_kind=resource_kind,
            resource_id=(
                "target-intent"
                if resource_kind is RunnerResourceKind.TARGET_HTTP_INTENT
                else "browser-session"
                if resource_kind is RunnerResourceKind.BROWSER_SESSION
                else "terminal-session"
            ),
            execution_id=(
                "terminal-execution"
                if resource_kind is RunnerResourceKind.TERMINAL_SESSION
                else None
            ),
        )

    assert captured.value.code == expected_code
    async with database.session_factory() as session:
        assert list(await session.scalars(select(RunnerCommandRecord.id))) == []
    await database.dispose()


@pytest.mark.asyncio
async def test_execution_stop_rejects_payload_key_that_is_not_authoritative(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'wrong-execution-key.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    execution = Execution(
        id="execution-authoritative-key",
        execution_key="execution-key-authoritative",
        run_id=_RUN_ID,
        node_id="runner-a",
        owner=_PRINCIPAL_A,
        executor_type=ExecutorType.PROCESS,
        argv=["sleep", "30"],
        cwd="/tmp/runner-control",
        stdout_path=str(tmp_path / "authoritative-key.stdout"),
        stderr_path=str(tmp_path / "authoritative-key.stderr"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    await executions.create_if_absent(execution)
    service = RunnerControlService(
        credentials=SQLAlchemyRunnerCredentialRepository(database.session_factory),
        commands=SQLAlchemyRunnerCommandRepository(database.session_factory),
        nodes=nodes,
        executions=executions,
        runs=SQLAlchemyRunRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "runner-state"),
        registration_token=None,
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.enqueue(
            "runner-a",
            kind=RunnerCommandKind.CANCEL,
            idempotency_key="wrong-execution-key",
            payload={
                "execution_id": execution.id,
                "execution_key": "execution-key-foreign",
            },
            run_id=execution.run_id,
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=RunnerOperationFamily.SAFETY_STOP,
            resource_kind=RunnerResourceKind.EXECUTION,
            resource_id=execution.id,
            execution_id=execution.id,
            output_contract=RunnerOutputContract(
                result_schema="riftx.runner-result/execution-stop/v1",
                stop_ack_schema=RUNNER_STOP_ACK_EXECUTION_SCHEMA,
            ),
        )

    assert captured.value.code == "runner_command_payload_binding_mismatch"
    assert captured.value.details == {"invalid_fields": ["execution_key"]}
    async with database.session_factory() as session:
        assert list(await session.scalars(select(RunnerCommandRecord.id))) == []
    await database.dispose()


@pytest.mark.asyncio
async def test_target_http_requires_its_registered_command_output_contract(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'target-output-contract.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    service = RunnerControlService(
        credentials=SQLAlchemyRunnerCredentialRepository(database.session_factory),
        commands=SQLAlchemyRunnerCommandRepository(database.session_factory),
        nodes=nodes,
        executions=SQLAlchemyExecutionRepository(database.session_factory),
        runs=SQLAlchemyRunRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "runner-state"),
        registration_token=None,
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.enqueue(
            "runner-a",
            kind=RunnerCommandKind.TARGET_HTTP,
            idempotency_key="target-http-without-output",
            payload={
                "launch": {
                    "run_id": _RUN_ID,
                    "node_id": "runner-a",
                    "tool_call_id": "target-intent",
                }
            },
            run_id=_RUN_ID,
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=RunnerOperationFamily.TARGET_HTTP,
            resource_kind=RunnerResourceKind.TARGET_HTTP_INTENT,
            resource_id="target-intent",
            output_contract=RunnerOutputContract(
                result_schema="riftx.runner-result/target-http/v1",
            ),
        )

    assert captured.value.code == "runner_output_contract_invalid"
    async with database.session_factory() as session:
        assert list(await session.scalars(select(RunnerCommandRecord.id))) == []
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_command_leases_are_idempotent_scoped_and_reclaimable(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runner-control.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    now = utc_now()
    command = _verified_command(
        kind=RunnerCommandKind.TARGET_HTTP,
        idempotency_key="target-http:key-1",
        created_at=now,
    )
    created, was_created = await repository.enqueue(command)
    duplicate, duplicate_created = await repository.enqueue(command)
    assert was_created is True
    assert duplicate_created is False
    assert duplicate.id == created.id

    first, second = await asyncio.gather(
        repository.lease_next(
            "runner-a",
            principal=_PRINCIPAL_A,
            lease_id="lease-a",
            leased_until=now + timedelta(seconds=1),
            now=now,
            validate_candidate=_accept_candidate,
        ),
        repository.lease_next(
            "runner-a",
            principal=_PRINCIPAL_A,
            lease_id="lease-b",
            leased_until=now + timedelta(seconds=1),
            now=now,
            validate_candidate=_accept_candidate,
        ),
    )
    leased = [item for item in (first, second) if item is not None]
    assert len(leased) == 1
    assert leased[0].attempts == 1

    renewed = await repository.renew_lease(
        command.id,
        principal=_PRINCIPAL_A,
        lease_id=leased[0].lease_id or "",
        **_callback_identity(leased[0]),
        leased_until=now + timedelta(seconds=3),
        now=now + timedelta(milliseconds=500),
    )
    assert renewed.lease_expires_at == now + timedelta(seconds=3)

    not_reclaimed = await repository.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id="lease-too-early",
        leased_until=now + timedelta(seconds=4),
        now=now + timedelta(seconds=2),
        validate_candidate=_accept_candidate,
    )
    assert not_reclaimed is None

    reclaimed = await repository.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id="lease-reclaimed",
        leased_until=now + timedelta(seconds=6),
        now=now + timedelta(seconds=4),
        validate_candidate=_accept_candidate,
    )
    assert reclaimed is not None
    assert reclaimed.id == command.id
    assert reclaimed.attempts == 2

    with pytest.raises(RepositoryConflictError, match="lease does not match"):
        await repository.finish(
            command.id,
            principal=_PRINCIPAL_A,
            lease_id=leased[0].lease_id or "",
            **_callback_identity(renewed),
            status=RunnerCommandStatus.COMPLETED,
            result={},
            error="",
            completed_at=now + timedelta(seconds=4),
        )

    completed = await repository.finish(
        command.id,
        principal=_PRINCIPAL_A,
        lease_id="lease-reclaimed",
        **_callback_identity(reclaimed),
        status=RunnerCommandStatus.COMPLETED,
        result={"accepted": True},
        error="",
        completed_at=now + timedelta(seconds=4),
    )
    repeated = await repository.finish(
        command.id,
        principal=_PRINCIPAL_A,
        lease_id="lease-reclaimed",
        **_callback_identity(reclaimed),
        status=RunnerCommandStatus.COMPLETED,
        result={"accepted": True},
        error="",
        completed_at=now + timedelta(seconds=4),
    )
    assert completed.status is RunnerCommandStatus.COMPLETED
    assert repeated.result == {"accepted": True}

    with pytest.raises(RepositoryConflictError, match="lease does not match or expired"):
        await repository.renew_lease(
            command.id,
            principal=_PRINCIPAL_A,
            lease_id="lease-reclaimed",
            **_callback_identity(completed),
            leased_until=now + timedelta(seconds=10),
            now=now + timedelta(seconds=5),
        )

    pending_execute = _verified_command(
        kind=RunnerCommandKind.TARGET_HTTP,
        idempotency_key="target-http:pending",
        created_at=now + timedelta(seconds=5),
    )
    pending_cancel = _verified_command(
        kind=RunnerCommandKind.TARGET_HTTP_CANCEL,
        idempotency_key="target-http-cancel:pending",
        created_at=now + timedelta(seconds=6),
    )
    await repository.enqueue(pending_execute)
    await repository.enqueue(pending_cancel)

    safety_first = await repository.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id="lease-safety",
        leased_until=now + timedelta(seconds=20),
        now=now + timedelta(seconds=10),
        validate_candidate=_accept_candidate,
    )

    assert safety_first is not None
    assert safety_first.id == pending_cancel.id
    assert safety_first.kind is RunnerCommandKind.TARGET_HTTP_CANCEL
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_claim_validates_before_lease_and_quarantines_without_delivery(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runner-claim-validation.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    now = utc_now()
    rejected = _verified_command(
        kind=RunnerCommandKind.TARGET_HTTP,
        idempotency_key="target-http:rejected-before-lease",
        created_at=now,
    )
    accepted = _verified_command(
        kind=RunnerCommandKind.TARGET_HTTP,
        idempotency_key="target-http:accepted-after-quarantine",
        created_at=now + timedelta(milliseconds=1),
    )
    await repository.enqueue(rejected)
    await repository.enqueue(accepted)

    async def validate(command: RunnerCommand) -> str | None:
        if command.id == rejected.id:
            return "run_kind_effect_policy_denied"
        return None

    leased = await repository.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id="lease-after-validation",
        leased_until=now + timedelta(seconds=30),
        now=now + timedelta(seconds=1),
        validate_candidate=validate,
    )

    assert leased is not None and leased.id == accepted.id
    quarantined = await repository.get(rejected.id)
    assert quarantined is not None
    assert quarantined.ownership_state is RunnerCommandOwnershipState.QUARANTINED
    assert quarantined.quarantine_reason == "run_kind_effect_policy_denied"
    assert quarantined.status is RunnerCommandStatus.PENDING
    assert quarantined.lease_id is None
    assert quarantined.attempts == 0
    assert quarantined.state_version == 1
    assert [item.id for item in await repository.list_quarantined()] == [rejected.id]
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_claim_validator_crash_leaves_no_executable_lease(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runner-claim-crash.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    now = utc_now()
    command = _verified_command(
        kind=RunnerCommandKind.TARGET_HTTP,
        idempotency_key="target-http:validator-crash",
        created_at=now,
    )
    await repository.enqueue(command)

    async def crash(_command: RunnerCommand) -> str | None:
        raise RuntimeError("simulated authority lookup crash")

    with pytest.raises(RuntimeError, match="simulated authority lookup crash"):
        await repository.lease_next(
            "runner-a",
            principal=_PRINCIPAL_A,
            lease_id="lease-must-not-persist",
            leased_until=now + timedelta(seconds=30),
            now=now + timedelta(seconds=1),
            validate_candidate=crash,
        )

    persisted = await repository.get(command.id)
    assert persisted is not None
    assert persisted.status is RunnerCommandStatus.PENDING
    assert persisted.lease_id is None
    assert persisted.lease_expires_at is None
    assert persisted.attempts == 0
    assert persisted.state_version == 0
    await database.dispose()


@pytest.mark.asyncio
async def test_service_poll_quarantines_invalid_authority_before_leasing_next(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runner-service-poll.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    missing_execution = Execution(
        id="execution-missing-authority",
        execution_key="execution-key-missing-authority",
        run_id=_RUN_ID,
        node_id="runner-a",
        owner=_PRINCIPAL_A,
        executor_type=ExecutorType.PROCESS,
        argv=["true"],
        cwd="/tmp/runner-control",
        stdout_path=str(tmp_path / "missing.stdout"),
        stderr_path=str(tmp_path / "missing.stderr"),
    )
    valid_execution = missing_execution.model_copy(
        update={
            "id": "execution-valid-authority",
            "execution_key": "execution-key-valid-authority",
            "stdout_path": str(tmp_path / "valid.stdout"),
            "stderr_path": str(tmp_path / "valid.stderr"),
        }
    )
    await executions.create_if_absent(valid_execution)
    commands = SQLAlchemyRunnerCommandRepository(database.session_factory)
    invalid = _verified_execution_stop_command(
        missing_execution,
        created_at=utc_now(),
    )
    valid = _verified_execution_stop_command(
        valid_execution,
        created_at=utc_now() + timedelta(milliseconds=1),
    )
    await commands.enqueue(invalid)
    await commands.enqueue(valid)
    credential = await SQLAlchemyRunnerCredentialRepository(
        database.session_factory
    ).get_by_principal("runner-a", _PRINCIPAL_A)
    assert credential is not None
    service = RunnerControlService(
        credentials=SQLAlchemyRunnerCredentialRepository(database.session_factory),
        commands=commands,
        nodes=nodes,
        executions=executions,
        runs=SQLAlchemyRunRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "runner-state"),
        registration_token=None,
    )
    service.authenticate = AsyncMock(return_value=credential)  # type: ignore[method-assign]

    leased = await service.poll("runner-a", "runner-token")

    assert leased is not None and leased.id == valid.id
    rejected = await commands.get(invalid.id)
    assert rejected is not None
    assert rejected.ownership_state is RunnerCommandOwnershipState.QUARANTINED
    assert rejected.quarantine_reason == "runner_command_ownership_invalid"
    assert rejected.lease_id is None
    assert rejected.attempts == 0
    await database.dispose()


@pytest.mark.parametrize(
    ("status", "expected_intent_count"),
    [
        (ExecutionStatus.CANCELLED, 0),
        (ExecutionStatus.COMPLETED, 1),
        (ExecutionStatus.EXITED, 1),
    ],
)
@pytest.mark.asyncio
async def test_verified_execution_status_routes_only_cancel_to_safety_projection(
    tmp_path: Path,
    status: ExecutionStatus,
    expected_intent_count: int,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'status-{status.value}.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    workflow_executions = SQLAlchemyExecutionRepository(
        database.session_factory,
        emit_workflow_signal_intents=True,
    )
    safety_executions = SQLAlchemyExecutionRepository(
        database.session_factory,
        emit_workflow_signal_intents=False,
    )
    execution = Execution(
        id=f"execution-status-{status.value}",
        execution_key=f"execution-key-status-{status.value}",
        run_id=_RUN_ID,
        node_id="runner-a",
        owner=_PRINCIPAL_A,
        executor_type=ExecutorType.PROCESS,
        argv=["true"],
        cwd="/tmp/runner-control",
        stdout_path=str(tmp_path / f"status-{status.value}.stdout"),
        stderr_path=str(tmp_path / f"status-{status.value}.stderr"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    await workflow_executions.create_if_absent(execution)
    commands = SQLAlchemyRunnerCommandRepository(database.session_factory)
    launch = _verified_execution_launch_command(execution, created_at=utc_now())
    await commands.enqueue(launch)
    credential = await SQLAlchemyRunnerCredentialRepository(
        database.session_factory
    ).get_by_principal("runner-a", _PRINCIPAL_A)
    assert credential is not None and launch.ownership is not None
    service = RunnerControlService(
        credentials=SQLAlchemyRunnerCredentialRepository(database.session_factory),
        commands=commands,
        nodes=nodes,
        executions=workflow_executions,
        stop_projection_executions=safety_executions,
        runs=SQLAlchemyRunRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "runner-state"),
        registration_token=None,
    )
    service.authenticate = AsyncMock(return_value=credential)  # type: ignore[method-assign]

    reported = await service.report_execution(
        "runner-a",
        "runner-token",
        execution.id,
        ExecutionStatusReport(
            status=status,
            exit_code=(None if status is ExecutionStatus.CANCELLED else 0),
            physical_stop_confirmed=True,
        ),
        command_id=launch.id,
        effect_binding_id=launch.ownership.effect_binding.id,
        envelope_digest=launch.ownership.envelope_digest,
        binding_digest=launch.ownership.effect_binding.binding_digest,
    )

    assert reported.status is status
    assert await _workflow_signal_intent_count(database) == expected_intent_count
    if status is ExecutionStatus.CANCELLED:
        retried_natural_stop = await service.report_execution(
            "runner-a",
            "runner-token",
            execution.id,
            ExecutionStatusReport(
                status=ExecutionStatus.COMPLETED,
                exit_code=0,
                physical_stop_confirmed=True,
            ),
            command_id=launch.id,
            effect_binding_id=launch.ownership.effect_binding.id,
            envelope_digest=launch.ownership.envelope_digest,
            binding_digest=launch.ownership.effect_binding.binding_digest,
        )
        assert retried_natural_stop.status is ExecutionStatus.CANCELLED
        assert retried_natural_stop.exit_code == 0
        assert await _workflow_signal_intent_count(database) == 0
    await database.dispose()


@pytest.mark.asyncio
async def test_non_safety_command_cannot_authorize_stop_receipt_projection(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'non-safety-receipt.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    execution = Execution(
        id="execution-non-safety-receipt",
        execution_key="execution-key-non-safety-receipt",
        run_id=_RUN_ID,
        node_id="runner-a",
        owner=_PRINCIPAL_A,
        executor_type=ExecutorType.PROCESS,
        argv=["sleep", "30"],
        cwd="/tmp/runner-control",
        stdout_path=str(tmp_path / "non-safety.stdout"),
        stderr_path=str(tmp_path / "non-safety.stderr"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    await executions.create_if_absent(execution)
    commands = SQLAlchemyRunnerCommandRepository(database.session_factory)
    launch = _verified_execution_launch_command(execution, created_at=utc_now())
    await commands.enqueue(launch)
    leased = await commands.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id="lease-non-safety-receipt",
        leased_until=utc_now() + timedelta(seconds=30),
        now=utc_now(),
        validate_candidate=_accept_candidate,
    )
    assert leased is not None
    result: dict[str, object] = {
        "execution_id": execution.id,
        "status": ExecutionStatus.RUNNING.value,
    }
    receipt = _stop_receipt(leased, ack=result)
    await commands.finish(
        leased.id,
        principal=_PRINCIPAL_A,
        lease_id=leased.lease_id or "",
        **_callback_identity(leased),
        status=RunnerCommandStatus.COMPLETED,
        result=result,
        error="",
        completed_at=utc_now(),
        stop_receipt=receipt,
    )
    service = RunnerControlService(
        credentials=SQLAlchemyRunnerCredentialRepository(database.session_factory),
        commands=commands,
        nodes=nodes,
        executions=executions,
        stop_projection_executions=executions,
        runs=SQLAlchemyRunRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "runner-state"),
        registration_token=None,
    )

    assert await service.reconcile_stop_receipts() == 0
    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.RUNNING
    assert [item.id for item in await commands.list_pending_stop_receipts()] == [receipt.id]
    await database.dispose()


@pytest.mark.asyncio
async def test_terminal_stop_receipt_never_projects_an_ambiguous_session(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'ambiguous-terminal-stop.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    commands = SQLAlchemyRunnerCommandRepository(database.session_factory)
    command = _verified_command(
        kind=RunnerCommandKind.TERMINAL_CLOSE,
        idempotency_key="ambiguous-terminal-stop",
        created_at=utc_now(),
    )
    assert command.ownership is not None
    binding = command.ownership.effect_binding
    assert binding.execution_id is not None
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    execution = Execution(
        id=binding.execution_id,
        execution_key=str(command.payload["execution_key"]),
        run_id=binding.run_id,
        node_id=binding.node_id,
        owner=binding.target,
        executor_type=ExecutorType.PTY,
        argv=["sh"],
        cwd="/tmp/runner-control",
        stdout_path=str(tmp_path / "ambiguous-terminal.stdout"),
        stderr_path=str(tmp_path / "ambiguous-terminal.stderr"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    await executions.create_if_absent(execution)
    terminals = SQLAlchemyTerminalRepository(database.session_factory)
    await terminals.create(
        TerminalSession(
            id=binding.resource_id,
            run_id=binding.run_id,
            execution_id=execution.id,
        )
    )
    await terminals.create(
        TerminalSession(
            id="terminal-ambiguous-foreign",
            run_id=binding.run_id,
            execution_id=execution.id,
        )
    )
    await commands.enqueue(command)
    leased = await commands.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id="lease-ambiguous-terminal-stop",
        leased_until=utc_now() + timedelta(seconds=30),
        now=utc_now(),
        validate_candidate=_accept_candidate,
    )
    assert leased is not None
    ack: dict[str, object] = {
        "execution_id": execution.id,
        "local_execution_id": execution.id,
        "execution_key": execution.execution_key,
        "session_id": binding.resource_id,
        "owner": binding.target.model_dump(mode="json"),
        "status": ExecutionStatus.CANCELLED.value,
        "physical_stop_confirmed": True,
    }
    receipt = _stop_receipt(leased, ack=ack)
    await commands.finish(
        leased.id,
        principal=_PRINCIPAL_A,
        lease_id=leased.lease_id or "",
        **_callback_identity(leased),
        status=RunnerCommandStatus.COMPLETED,
        result=ack,
        error="",
        completed_at=utc_now(),
        stop_receipt=receipt,
    )
    service = RunnerControlService(
        credentials=SQLAlchemyRunnerCredentialRepository(database.session_factory),
        commands=commands,
        nodes=nodes,
        executions=executions,
        stop_projection_executions=executions,
        runs=SQLAlchemyRunRepository(database.session_factory),
        terminals=terminals,
        paths=RunnerPaths(tmp_path / "runner-state"),
        registration_token=None,
    )

    assert await service.reconcile_stop_receipts() == 0
    exact = await terminals.get(binding.resource_id)
    foreign = await terminals.get("terminal-ambiguous-foreign")
    assert exact is not None and exact.status is TerminalStatus.CREATED
    assert foreign is not None and foreign.status is TerminalStatus.CREATED
    assert [item.id for item in await commands.list_pending_stop_receipts()] == [receipt.id]
    await database.dispose()


@pytest.mark.parametrize(
    ("operation", "operation_family", "use_foreign_execution"),
    [
        (RunnerCommandKind.BROWSER_CLOSE, None, False),
        (None, RunnerOperationFamily.BROWSER, False),
        (None, None, True),
    ],
)
@pytest.mark.asyncio
async def test_corrupt_stop_receipt_never_projects_and_remains_pending(
    tmp_path: Path,
    operation: RunnerCommandKind | None,
    operation_family: RunnerOperationFamily | None,
    use_foreign_execution: bool,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'corrupt-stop-receipt.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            id="runner-control-foreign-run",
            engagement_id="runner-control-engagement",
            node_id="runner-a",
            objective=Objective(description="Foreign stop receipt target"),
            kind=RunKind.GENERAL,
            workspace_path="/tmp/runner-control-foreign",
            temporal_workflow_id="riftx-run-runner-control-foreign",
        )
    )
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    owned = Execution(
        id="execution-stop-owned",
        execution_key="execution-key-stop-owned",
        run_id=_RUN_ID,
        node_id="runner-a",
        owner=_PRINCIPAL_A,
        executor_type=ExecutorType.PROCESS,
        argv=["sleep", "30"],
        cwd="/tmp/runner-control",
        stdout_path=str(tmp_path / "owned.stdout"),
        stderr_path=str(tmp_path / "owned.stderr"),
    )
    foreign = owned.model_copy(
        update={
            "id": "execution-stop-foreign",
            "execution_key": "execution-key-stop-foreign",
            "run_id": "runner-control-foreign-run",
            "stdout_path": str(tmp_path / "foreign.stdout"),
            "stderr_path": str(tmp_path / "foreign.stderr"),
        }
    )
    for execution in (owned, foreign):
        execution.transition_to(ExecutionStatus.STARTING)
        execution.transition_to(ExecutionStatus.RUNNING)
        await executions.create_if_absent(execution)
    commands = SQLAlchemyRunnerCommandRepository(database.session_factory)
    command = _verified_execution_stop_command(owned, created_at=utc_now())
    await commands.enqueue(command)
    leased = await commands.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id="lease-corrupt-stop-receipt",
        leased_until=utc_now() + timedelta(seconds=30),
        now=utc_now(),
        validate_candidate=_accept_candidate,
    )
    assert leased is not None
    ack = {
        "execution_id": owned.id,
        "local_execution_id": owned.id,
        "execution_key": owned.execution_key,
        "owner": _PRINCIPAL_A.model_dump(mode="json"),
        "status": ExecutionStatus.CANCELLED.value,
        "physical_stop_confirmed": True,
    }
    receipt = _stop_receipt(
        leased,
        ack=ack,
        operation=operation,
        operation_family=operation_family,
        execution_id=(foreign.id if use_foreign_execution else owned.id),
    )
    await commands.finish(
        leased.id,
        principal=_PRINCIPAL_A,
        lease_id=leased.lease_id or "",
        **_callback_identity(leased),
        status=RunnerCommandStatus.COMPLETED,
        result=ack,
        error="",
        completed_at=utc_now(),
        stop_receipt=receipt,
    )
    service = RunnerControlService(
        credentials=SQLAlchemyRunnerCredentialRepository(database.session_factory),
        commands=commands,
        nodes=nodes,
        executions=executions,
        stop_projection_executions=executions,
        runs=runs,
        paths=RunnerPaths(tmp_path / "runner-state"),
        registration_token=None,
    )

    assert await service.reconcile_stop_receipts() == 0
    persisted_owned = await executions.get(owned.id)
    persisted_foreign = await executions.get(foreign.id)
    assert persisted_owned is not None
    assert persisted_owned.status is ExecutionStatus.RUNNING
    assert persisted_foreign is not None
    assert persisted_foreign.status is ExecutionStatus.RUNNING
    assert [item.id for item in await commands.list_pending_stop_receipts()] == [receipt.id]
    await database.dispose()


@pytest.mark.asyncio
async def test_pending_stop_receipt_converges_after_control_plane_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runner-stop-restart.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    executions = SQLAlchemyExecutionRepository(
        database.session_factory,
        emit_workflow_signal_intents=True,
    )
    stop_projection_executions = SQLAlchemyExecutionRepository(
        database.session_factory,
        emit_workflow_signal_intents=False,
    )
    execution = Execution(
        id="execution-stop-restart",
        execution_key="execution-key-stop-restart",
        run_id=_RUN_ID,
        node_id="runner-a",
        owner=_PRINCIPAL_A,
        executor_type=ExecutorType.PROCESS,
        argv=["sleep", "30"],
        cwd="/tmp/runner-control",
        stdout_path=str(tmp_path / "stop-restart.stdout"),
        stderr_path=str(tmp_path / "stop-restart.stderr"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    await executions.create_if_absent(execution)
    commands = SQLAlchemyRunnerCommandRepository(database.session_factory)
    command = _verified_execution_stop_command(execution, created_at=utc_now())
    await commands.enqueue(command)
    leased = await commands.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id="lease-stop-restart",
        leased_until=utc_now() + timedelta(seconds=30),
        now=utc_now(),
        validate_candidate=_accept_candidate,
    )
    assert leased is not None and leased.ownership is not None
    credential = await SQLAlchemyRunnerCredentialRepository(
        database.session_factory
    ).get_by_principal("runner-a", _PRINCIPAL_A)
    assert credential is not None
    service = RunnerControlService(
        credentials=SQLAlchemyRunnerCredentialRepository(database.session_factory),
        commands=commands,
        nodes=nodes,
        executions=executions,
        stop_projection_executions=stop_projection_executions,
        runs=SQLAlchemyRunRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "runner-state"),
        registration_token=None,
    )
    service.authenticate = AsyncMock(return_value=credential)  # type: ignore[method-assign]
    service._project_stop_receipt = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("simulated crash after receipt commit")
    )
    ack = {
        "execution_id": execution.id,
        "local_execution_id": execution.id,
        "execution_key": execution.execution_key,
        "owner": _PRINCIPAL_A.model_dump(mode="json"),
        "status": ExecutionStatus.CANCELLED.value,
        "physical_stop_confirmed": True,
    }
    with pytest.raises(RuntimeError, match="simulated crash after receipt commit"):
        await service.finish_command(
            "runner-a",
            "runner-token",
            command.id,
            lease_id=leased.lease_id or "",
            **_callback_identity(leased),
            succeeded=True,
            result=ack,
        )
    persisted_command = await commands.get(command.id)
    assert persisted_command is not None
    assert persisted_command.status is RunnerCommandStatus.COMPLETED
    assert await commands.get_stop_receipt(command.id) is not None
    assert len(await commands.list_pending_stop_receipts()) == 1
    assert await _workflow_signal_intent_count(database) == 0
    await database.dispose()

    restarted_database = Database(f"sqlite+aiosqlite:///{database_path}")
    restarted_commands = SQLAlchemyRunnerCommandRepository(restarted_database.session_factory)
    restarted_executions = SQLAlchemyExecutionRepository(
        restarted_database.session_factory,
        emit_workflow_signal_intents=True,
    )
    restarted_stop_projection_executions = SQLAlchemyExecutionRepository(
        restarted_database.session_factory,
        emit_workflow_signal_intents=False,
    )
    restarted = RunnerControlService(
        credentials=SQLAlchemyRunnerCredentialRepository(restarted_database.session_factory),
        commands=restarted_commands,
        nodes=NodeApplicationService(SQLAlchemyNodeRepository(restarted_database.session_factory)),
        executions=restarted_executions,
        stop_projection_executions=restarted_stop_projection_executions,
        runs=SQLAlchemyRunRepository(restarted_database.session_factory),
        paths=RunnerPaths(tmp_path / "runner-state-restarted"),
        registration_token=None,
    )

    assert await restarted.reconcile_stop_receipts() == 1
    converged = await restarted_executions.get(execution.id)
    assert converged is not None
    assert converged.status is ExecutionStatus.CANCELLED
    assert converged.physical_stop_confirmed_at is not None
    assert await _workflow_signal_intent_count(restarted_database) == 0
    assert await restarted_commands.list_pending_stop_receipts() == []
    assert await restarted.reconcile_stop_receipts() == 0
    assert await _workflow_signal_intent_count(restarted_database) == 0
    await restarted_database.dispose()


@pytest.mark.parametrize(
    "kind",
    [RunnerCommandKind.BROWSER_CLOSE, RunnerCommandKind.TARGET_HTTP_CANCEL],
)
@pytest.mark.asyncio
async def test_resource_stop_receipt_projects_authoritative_state_after_restart(
    tmp_path: Path,
    kind: RunnerCommandKind,
) -> None:
    database_path = tmp_path / f"runner-{kind.value}-stop-restart.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    session, cycle, step = await _seed_runtime_hierarchy(database)
    browser_sessions = SQLAlchemyBrowserRepository(database.session_factory)
    tool_call_intents = SQLAlchemyToolCallIntentRepository(database.session_factory)
    command = _verified_command(
        kind=kind,
        idempotency_key=f"resource-stop-restart:{kind.value}",
        created_at=utc_now(),
    )
    assert command.ownership is not None
    resource_id = command.ownership.effect_binding.resource_id
    if kind is RunnerCommandKind.BROWSER_CLOSE:
        authoritative_browser = BrowserSession(
            id=resource_id,
            run_id=_RUN_ID,
            agent_session_id=session.id,
            node_id="runner-a",
            mode=BrowserMode.MANAGED_EPHEMERAL,
            status=BrowserSessionStatus.ACTIVE,
        )
        await browser_sessions.create_session(authoritative_browser)
        closed_ack = authoritative_browser.model_copy(deep=True)
        closed_ack.transition_to(BrowserSessionStatus.CLOSED)
        ack: dict[str, object] = {"result": {"session": closed_ack.model_dump(mode="json")}}
    else:
        authoritative_intent = ToolCallIntent(
            id=resource_id,
            run_id=_RUN_ID,
            session_id=session.id,
            cycle_id=cycle.id,
            step_id=step.id,
            tool_id="request_target_url",
            status=ToolCallStatus.EXECUTING,
        )
        await tool_call_intents.create(authoritative_intent)
        ack = {
            "outcomes": [
                {
                    "tool_call_id": resource_id,
                    "confirmed": True,
                }
            ]
        }

    commands = SQLAlchemyRunnerCommandRepository(database.session_factory)
    await commands.enqueue(command)
    leased = await commands.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id=f"lease-{kind.value}-restart",
        leased_until=utc_now() + timedelta(seconds=30),
        now=utc_now(),
        validate_candidate=_accept_candidate,
    )
    assert leased is not None
    credential = await SQLAlchemyRunnerCredentialRepository(
        database.session_factory
    ).get_by_principal("runner-a", _PRINCIPAL_A)
    assert credential is not None
    service = RunnerControlService(
        credentials=SQLAlchemyRunnerCredentialRepository(database.session_factory),
        commands=commands,
        nodes=nodes,
        executions=SQLAlchemyExecutionRepository(database.session_factory),
        runs=SQLAlchemyRunRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / f"runner-state-{kind.value}"),
        registration_token=None,
        browser_sessions=browser_sessions,
        tool_call_intents=tool_call_intents,
    )
    service.authenticate = AsyncMock(return_value=credential)  # type: ignore[method-assign]
    service._project_stop_receipt = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("simulated crash after resource receipt commit")
    )

    with pytest.raises(
        RuntimeError,
        match="simulated crash after resource receipt commit",
    ):
        await service.finish_command(
            "runner-a",
            "runner-token",
            command.id,
            lease_id=leased.lease_id or "",
            **_callback_identity(leased),
            succeeded=True,
            result=ack,
        )
    assert await commands.get_stop_receipt(command.id) is not None
    assert len(await commands.list_pending_stop_receipts()) == 1
    await database.dispose()

    restarted_database = Database(f"sqlite+aiosqlite:///{database_path}")
    restarted_commands = SQLAlchemyRunnerCommandRepository(restarted_database.session_factory)
    restarted_browser_sessions = SQLAlchemyBrowserRepository(restarted_database.session_factory)
    restarted_tool_call_intents = SQLAlchemyToolCallIntentRepository(
        restarted_database.session_factory
    )
    restarted = RunnerControlService(
        credentials=SQLAlchemyRunnerCredentialRepository(restarted_database.session_factory),
        commands=restarted_commands,
        nodes=NodeApplicationService(SQLAlchemyNodeRepository(restarted_database.session_factory)),
        executions=SQLAlchemyExecutionRepository(restarted_database.session_factory),
        runs=SQLAlchemyRunRepository(restarted_database.session_factory),
        paths=RunnerPaths(tmp_path / f"runner-state-{kind.value}-restarted"),
        registration_token=None,
        browser_sessions=restarted_browser_sessions,
        tool_call_intents=restarted_tool_call_intents,
    )

    assert await restarted.reconcile_stop_receipts() == 1
    assert await restarted_commands.list_pending_stop_receipts() == []
    if kind is RunnerCommandKind.BROWSER_CLOSE:
        projected_browser = await restarted_browser_sessions.get_session(resource_id)
        assert projected_browser is not None
        assert projected_browser.status is BrowserSessionStatus.CLOSED
        projected_state = projected_browser.closed_at
        assert projected_state is not None
    else:
        projected_intent = await restarted_tool_call_intents.get(resource_id)
        assert projected_intent is not None
        assert projected_intent.status is ToolCallStatus.CANCELLED
        projected_state = projected_intent.status

    assert await restarted.reconcile_stop_receipts() == 0
    if kind is RunnerCommandKind.BROWSER_CLOSE:
        replayed_browser = await restarted_browser_sessions.get_session(resource_id)
        assert replayed_browser is not None
        assert replayed_browser.closed_at == projected_state
    else:
        replayed_intent = await restarted_tool_call_intents.get(resource_id)
        assert replayed_intent is not None
        assert replayed_intent.status is projected_state
    await restarted_database.dispose()


@pytest.mark.parametrize(
    "kind",
    [
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        RunnerCommandKind.BROWSER_CLOSE,
        RunnerCommandKind.TERMINAL_CLOSE,
    ],
)
@pytest.mark.asyncio
async def test_runner_safety_commands_preempt_older_effect_commands(
    tmp_path: Path,
    kind: RunnerCommandKind,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'{kind.value}.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    now = utc_now()
    await repository.enqueue(
        _verified_command(
            kind=RunnerCommandKind.TARGET_HTTP,
            idempotency_key=f"effect:{kind.value}",
            created_at=now,
        )
    )
    no_safety = await repository.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id=f"no-safety:{kind.value}",
        leased_until=now + timedelta(seconds=30),
        now=now + timedelta(milliseconds=500),
        validate_candidate=_accept_candidate,
        safety_only=True,
    )
    assert no_safety is None
    safety = _verified_command(
        kind=kind,
        idempotency_key=f"safety:{kind.value}",
        created_at=now + timedelta(seconds=1),
    )
    await repository.enqueue(safety)

    leased = await repository.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id=f"lease:{kind.value}",
        leased_until=now + timedelta(seconds=30),
        now=now + timedelta(seconds=2),
        validate_candidate=_accept_candidate,
        safety_only=True,
    )

    assert leased is not None
    assert leased.id == safety.id
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_commands_are_fenced_to_the_exact_principal(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runner-fencing.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await _seed_authority(database, nodes)
    repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    now = utc_now()
    command = _verified_command(
        kind=RunnerCommandKind.TARGET_HTTP,
        idempotency_key="target-http:fenced",
        created_at=now,
    )
    await repository.enqueue(command)

    assert (
        await repository.lease_next(
            "runner-a",
            principal=_PRINCIPAL_B,
            lease_id="lease-b",
            leased_until=now + timedelta(seconds=10),
            now=now,
            validate_candidate=_accept_candidate,
        )
        is None
    )
    leased = await repository.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id="lease-a",
        leased_until=now + timedelta(seconds=10),
        now=now,
        validate_candidate=_accept_candidate,
    )
    assert leased is not None

    with pytest.raises(RepositoryConflictError, match="owner does not match"):
        await repository.renew_lease(
            command.id,
            principal=_PRINCIPAL_B,
            lease_id="lease-a",
            **_callback_identity(leased),
            leased_until=now + timedelta(seconds=20),
            now=now + timedelta(seconds=1),
        )
    with pytest.raises(RepositoryConflictError, match="owner does not match"):
        await repository.finish(
            command.id,
            principal=_PRINCIPAL_B,
            lease_id="lease-a",
            **_callback_identity(leased),
            status=RunnerCommandStatus.COMPLETED,
            result={},
            error="",
            completed_at=now + timedelta(seconds=1),
        )

    completed = await repository.finish(
        command.id,
        principal=_PRINCIPAL_A,
        lease_id="lease-a",
        **_callback_identity(leased),
        status=RunnerCommandStatus.COMPLETED,
        result={"owner": "a"},
        error="",
        completed_at=now + timedelta(seconds=1),
    )
    assert completed.result == {"owner": "a"}
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_credential_issuance_advances_a_unique_epoch_atomically(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runner-epochs.db'}")
    await database.create_schema()
    nodes = NodeApplicationService(SQLAlchemyNodeRepository(database.session_factory))
    await nodes.register(
        NodeRegistration(
            node_id="runner-a",
            name="Runner A",
            platform="linux",
            architecture="x86_64",
        )
    )
    repository = SQLAlchemyRunnerCredentialRepository(database.session_factory)
    now = utc_now()

    issued = await asyncio.gather(
        *(
            repository.issue(
                "runner-a",
                token_hash=f"{index:064x}",
                token_prefix=f"token-{index}",
                issued_at=now + timedelta(milliseconds=index),
            )
            for index in range(1, 9)
        )
    )

    assert sorted(credential.principal.epoch for credential in issued) == list(range(1, 9))
    assert len({credential.principal.instance_id for credential in issued}) == 8
    for credential in issued:
        assert (await repository.get_by_principal("runner-a", credential.principal)) == credential
        assert (await repository.get_by_token_hash("runner-a", credential.token_hash)) == credential

    current = await repository.get_current("runner-a")
    assert current is not None
    assert current.principal.epoch == 8
    node = await SQLAlchemyNodeRepository(database.session_factory).get("runner-a")
    assert node is not None
    assert node.current_owner == current.principal
    await database.dispose()
