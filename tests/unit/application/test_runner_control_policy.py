from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

import riftx.application.run_kind_effects as effect_policy
from riftx.application.errors import (
    ApplicationConflictError,
    AuthenticationError,
    ServiceUnavailableError,
)
from riftx.application.services.runner_control import RunnerControlService
from riftx.domain import (
    RUNNER_COMMAND_OWNERSHIP_CAPABILITY,
    RUNNER_STOP_ACK_EXECUTION_SCHEMA,
    BrowserSessionStatus,
    Execution,
    ExecutorType,
    Node,
    NodeStatus,
    Objective,
    Run,
    RunKind,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandOwnership,
    RunnerCommandOwnershipState,
    RunnerCommandStatus,
    RunnerCredential,
    RunnerEffectBinding,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
    RunnerStopReceipt,
    TerminalSession,
    runner_command_protocol,
    runner_payload_digest,
)
from riftx.domain.base import utc_now
from riftx.runner.paths import RunnerPaths

_NODE_ID = "runner-policy-node"
_OWNER = RunnerPrincipal(instance_id="runner-policy-owner", epoch=1)
_FOREIGN_OWNER = RunnerPrincipal(instance_id="runner-policy-foreign", epoch=2)


def _run(*, kind: RunKind = RunKind.GENERAL) -> Run:
    return Run(
        id=f"run-{kind.value}",
        engagement_id="engagement-runner-policy",
        node_id=_NODE_ID,
        objective=Objective(description="Runner policy test"),
        kind=kind,
        workspace_path="/tmp/runner-policy",
        temporal_workflow_id=f"riftx-run-{kind.value}",
    )


def _execution(run: Run, *, owner: RunnerPrincipal = _OWNER) -> Execution:
    return Execution(
        id=f"execution-{run.kind.value}",
        execution_key=f"execution-key-{run.kind.value}",
        run_id=run.id,
        node_id=run.node_id,
        owner=owner,
        executor_type=ExecutorType.PROCESS,
        argv=["true"],
        cwd=run.workspace_path,
        stdout_path=f"{run.workspace_path}/stdout",
        stderr_path=f"{run.workspace_path}/stderr",
    )


def _credential(owner: RunnerPrincipal = _OWNER) -> RunnerCredential:
    return RunnerCredential(
        node_id=_NODE_ID,
        principal=owner,
        token_hash="a" * 64,
        token_prefix="runner",
        protocol_capabilities=(RUNNER_COMMAND_OWNERSHIP_CAPABILITY,),
    )


def _command(execution: Execution) -> RunnerCommand:
    command_id = "runner-policy-command"
    payload = {
        "execution_id": execution.id,
        "execution_key": execution.execution_key,
    }
    binding = RunnerEffectBinding(
        id="runner-policy-binding",
        run_id=execution.run_id,
        run_kind=RunKind.GENERAL,
        node_id=execution.node_id,
        target=_OWNER,
        origin=RunnerCommandOrigin.APPLICATION_SERVICE,
        operation_family=RunnerOperationFamily.SAFETY_STOP,
        execution_id=execution.id,
        resource_kind=RunnerResourceKind.EXECUTION,
        resource_id=execution.id,
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
    )
    return RunnerCommand(
        id=command_id,
        node_id=execution.node_id,
        target=_OWNER,
        kind=RunnerCommandKind.CANCEL,
        idempotency_key="runner-policy-command",
        ownership=ownership,
        ownership_state=RunnerCommandOwnershipState.VERIFIED,
        quarantine_reason="",
        payload=payload,
    )


def _leased_execute_command(
    execution: Execution,
    *,
    max_result_bytes: int = 64 * 1024,
) -> RunnerCommand:
    command_id = f"runner-policy-execute-{max_result_bytes}"
    payload = {
        "execution_id": execution.id,
        "request": {
            "run_id": execution.run_id,
            "node_id": execution.node_id,
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
            "runner_principal": _OWNER.model_dump(mode="json"),
        },
    }
    binding = RunnerEffectBinding(
        id=f"{command_id}-binding",
        run_id=execution.run_id,
        run_kind=RunKind.GENERAL,
        node_id=execution.node_id,
        target=_OWNER,
        origin=RunnerCommandOrigin.APPLICATION_SERVICE,
        operation_family=RunnerOperationFamily.EXECUTION,
        execution_id=execution.id,
        resource_kind=RunnerResourceKind.EXECUTION,
        resource_id=execution.id,
    )
    ownership = RunnerCommandOwnership(
        command_id=command_id,
        effect_binding=binding,
        operation=RunnerCommandKind.EXECUTE,
        operation_family=RunnerOperationFamily.EXECUTION,
        payload_digest=runner_payload_digest(payload),
        output_contract=RunnerOutputContract(
            max_result_bytes=max_result_bytes,
            max_output_bytes=100_000_000,
            allowed_streams=("stderr", "stdout"),
            result_schema="riftx.runner-result/execution-start/v1",
        ),
    )
    return RunnerCommand(
        id=command_id,
        node_id=execution.node_id,
        target=_OWNER,
        kind=RunnerCommandKind.EXECUTE,
        idempotency_key=command_id,
        ownership=ownership,
        ownership_state=RunnerCommandOwnershipState.VERIFIED,
        quarantine_reason="",
        payload=payload,
        status=RunnerCommandStatus.LEASED,
        attempts=1,
        lease_id=f"lease-{command_id}",
        lease_expires_at=utc_now() + timedelta(minutes=1),
        state_version=3,
    )


def _legacy_leased_stop(
    *,
    kind: RunnerCommandKind = RunnerCommandKind.CANCEL,
    target: RunnerPrincipal = _OWNER,
) -> RunnerCommand:
    return RunnerCommand(
        id=f"legacy-{kind.value}",
        node_id=_NODE_ID,
        target=target,
        kind=kind,
        idempotency_key=f"legacy-{kind.value}",
        payload={"must_not_be_trusted": "resource-id"},
        status=RunnerCommandStatus.LEASED,
        attempts=1,
        lease_id=f"lease-legacy-{kind.value}",
        lease_expires_at=utc_now() - timedelta(minutes=1),
    )


def _legacy_execution_stop_ack(
    *,
    owner: RunnerPrincipal = _OWNER,
) -> dict[str, object]:
    return {
        "execution_id": "runner-local-execution",
        "local_execution_id": "runner-local-execution",
        "execution_key": "runner-local-key",
        "owner": owner.model_dump(mode="json"),
        "status": "cancelled",
        "physical_stop_confirmed": True,
    }


def _receipt(command: RunnerCommand) -> RunnerStopReceipt:
    assert command.ownership is not None
    binding = command.ownership.effect_binding
    return RunnerStopReceipt(
        id="runner-policy-receipt",
        command_id=command.id,
        effect_binding_id=binding.id,
        envelope_digest=command.ownership.envelope_digest,
        binding_digest=binding.binding_digest,
        operation=command.kind,
        operation_family=command.ownership.operation_family,
        resource_kind=binding.resource_kind,
        resource_id=binding.resource_id,
        execution_id=binding.execution_id,
        node_id=binding.node_id,
        principal=binding.target,
        ack_digest="b" * 64,
    )


def _service(
    tmp_path: Path,
    *,
    run: Run,
    execution: Execution | None,
    node_status: NodeStatus = NodeStatus.ONLINE,
    commands: object | None = None,
    browser_sessions: object | None = None,
    tool_call_intents: object | None = None,
    terminals: object | None = None,
    stop_projection_executions: object | None = None,
) -> tuple[RunnerControlService, SimpleNamespace, SimpleNamespace]:
    run_repository = SimpleNamespace(
        get=AsyncMock(return_value=run),
        list=AsyncMock(return_value=[run] if run.kind is RunKind.GENERAL else []),
    )
    execution_repository = SimpleNamespace(
        get=AsyncMock(return_value=execution),
        list=AsyncMock(return_value=([execution] if execution is not None else [])),
    )
    credential_repository = SimpleNamespace(
        get_by_principal=AsyncMock(return_value=_credential()),
        get_current=AsyncMock(return_value=_credential()),
    )
    node = Node(
        id=_NODE_ID,
        name="Runner Policy",
        platform="linux",
        architecture="x86_64",
        status=node_status,
        current_owner=_OWNER,
    )
    node_service = SimpleNamespace(get=AsyncMock(return_value=node))
    command_repository = commands or SimpleNamespace(enqueue=AsyncMock())
    service = RunnerControlService(
        credentials=credential_repository,  # type: ignore[arg-type]
        commands=command_repository,  # type: ignore[arg-type]
        nodes=node_service,  # type: ignore[arg-type]
        executions=execution_repository,  # type: ignore[arg-type]
        stop_projection_executions=stop_projection_executions,  # type: ignore[arg-type]
        runs=run_repository,  # type: ignore[arg-type]
        paths=RunnerPaths(tmp_path / "runner-state"),
        registration_token=None,
        terminals=terminals,  # type: ignore[arg-type]
        browser_sessions=browser_sessions,  # type: ignore[arg-type]
        tool_call_intents=tool_call_intents,  # type: ignore[arg-type]
    )
    return service, command_repository, execution_repository


def test_stop_projection_repository_never_falls_back_to_ordinary_execution_writes(
    tmp_path: Path,
) -> None:
    run = _run()
    execution = _execution(run)
    service, _, _ = _service(tmp_path, run=run, execution=execution)

    with pytest.raises(ServiceUnavailableError) as missing:
        service._require_stop_projection_executions()
    assert missing.value.code == "runner_stop_projection_unavailable"

    service._stop_projection_executions = SimpleNamespace(  # type: ignore[assignment]
        emits_workflow_signal_intents=True
    )
    with pytest.raises(ServiceUnavailableError) as emitting:
        service._require_stop_projection_executions()
    assert emitting.value.code == "runner_stop_projection_unavailable"

    with pytest.raises(ServiceUnavailableError) as missing_completion:
        service._require_completion_executions()
    assert missing_completion.value.code == "runner_completion_projection_unavailable"

    service._executions = SimpleNamespace(  # type: ignore[assignment]
        emits_workflow_signal_intents=False
    )
    with pytest.raises(ServiceUnavailableError) as non_emitting_completion:
        service._require_completion_executions()
    assert (
        non_emitting_completion.value.code
        == "runner_completion_projection_unavailable"
    )


@pytest.mark.asyncio
async def test_runner_without_ownership_capability_cannot_poll_before_lease(
    tmp_path: Path,
) -> None:
    commands = SimpleNamespace(lease_next=AsyncMock())
    service, _, _ = _service(
        tmp_path,
        run=_run(),
        execution=None,
        commands=commands,
    )
    service.authenticate = AsyncMock(  # type: ignore[method-assign]
        return_value=_credential().model_copy(update={"protocol_capabilities": ()})
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.poll(_NODE_ID, "legacy-token")

    assert captured.value.code == "runner_protocol_capability_missing"
    commands.lease_next.assert_not_awaited()
    assert not (tmp_path / "runner-state").exists()


@pytest.mark.asyncio
async def test_runner_without_ownership_capability_cannot_finish_before_lookup_or_write(
    tmp_path: Path,
) -> None:
    commands = SimpleNamespace(
        get=AsyncMock(),
        finish=AsyncMock(),
        record_legacy_stop_ack=AsyncMock(),
    )
    service, _, _ = _service(
        tmp_path,
        run=_run(),
        execution=None,
        commands=commands,
    )
    service.authenticate = AsyncMock(  # type: ignore[method-assign]
        return_value=_credential().model_copy(update={"protocol_capabilities": ()})
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.finish_command(
            _NODE_ID,
            "legacy-token",
            "verified-command",
            lease_id="verified-lease",
            state_version=0,
            envelope_digest="a" * 64,
            binding_digest="b" * 64,
            succeeded=True,
            result={},
        )

    assert captured.value.code == "runner_protocol_capability_missing"
    commands.get.assert_not_awaited()
    commands.finish.assert_not_awaited()
    commands.record_legacy_stop_ack.assert_not_awaited()
    assert not (tmp_path / "runner-state").exists()


@pytest.mark.parametrize(
    "projection_updates",
    [
        {"id": "foreign-execution"},
        {"run_id": "foreign-run"},
        {"node_id": "foreign-node"},
        {"owner": _FOREIGN_OWNER},
        {"audit_id": "foreign-audit", "plan_digest": "c" * 64},
    ],
)
@pytest.mark.asyncio
async def test_stop_receipt_requires_full_projection_execution_owner(
    tmp_path: Path,
    projection_updates: dict[str, object],
) -> None:
    run = _run()
    execution = _execution(run)
    command = _command(execution)
    receipt = _receipt(command)
    commands = SimpleNamespace(
        get=AsyncMock(return_value=command),
        mark_stop_receipt_projected=AsyncMock(),
    )
    projection_repository = SimpleNamespace(
        emits_workflow_signal_intents=False,
        get=AsyncMock(return_value=execution.model_copy(update=projection_updates)),
        save_if_status=AsyncMock(),
    )
    service, _, _ = _service(
        tmp_path,
        run=run,
        execution=execution,
        commands=commands,
        stop_projection_executions=projection_repository,
    )

    assert await service._project_stop_receipt(receipt) is False
    projection_repository.save_if_status.assert_not_awaited()
    commands.mark_stop_receipt_projected.assert_not_awaited()


@pytest.mark.asyncio
async def test_code_audit_m1_enqueue_is_zero_before_node_or_credential_state(
    tmp_path: Path,
) -> None:
    run = _run(kind=RunKind.CODE_AUDIT)
    commands = SimpleNamespace(enqueue=AsyncMock())
    service, _, _ = _service(
        tmp_path,
        run=run,
        execution=None,
        node_status=NodeStatus.OFFLINE,
        commands=commands,
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.enqueue(
            _NODE_ID,
            kind=RunnerCommandKind.CANCEL,
            idempotency_key="audit-m1-must-not-enqueue",
            payload={"execution_id": "audit-execution"},
            run_id=run.id,
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=RunnerOperationFamily.SAFETY_STOP,
            resource_kind=RunnerResourceKind.EXECUTION,
            resource_id="audit-execution",
            execution_id="audit-execution",
            target=_OWNER,
        )

    assert captured.value.code == "code_audit_runner_admission_denied"
    commands.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_wrong_owner_precedes_offline_state_and_has_zero_mutation(
    tmp_path: Path,
) -> None:
    run = _run()
    execution = _execution(run, owner=_FOREIGN_OWNER)
    commands = SimpleNamespace(enqueue=AsyncMock())
    service, _, _ = _service(
        tmp_path,
        run=run,
        execution=execution,
        node_status=NodeStatus.OFFLINE,
        commands=commands,
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.enqueue(
            _NODE_ID,
            kind=RunnerCommandKind.EXECUTE,
            idempotency_key="wrong-owner-before-state",
            payload={
                "execution_id": execution.id,
                "request": {
                    "run_id": run.id,
                    "node_id": run.node_id,
                    "execution_key": execution.execution_key,
                },
            },
            run_id=run.id,
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=RunnerOperationFamily.EXECUTION,
            resource_kind=RunnerResourceKind.EXECUTION,
            resource_id=execution.id,
            execution_id=execution.id,
            output_contract=RunnerOutputContract(
                max_output_bytes=100_000_000,
                allowed_streams=("stderr", "stdout"),
                result_schema="riftx.runner-result/execution-start/v1",
            ),
            target=_OWNER,
        )

    assert captured.value.code == "runner_effect_execution_mismatch"
    commands.enqueue.assert_not_awaited()


@pytest.mark.parametrize(
    "kind",
    [
        RunnerCommandKind.EXECUTE,
        RunnerCommandKind.TERMINAL_START,
        RunnerCommandKind.TARGET_HTTP,
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        RunnerCommandKind.BROWSER,
        RunnerCommandKind.BROWSER_CLOSE,
    ],
)
@pytest.mark.asyncio
async def test_control_plane_rejects_cross_kind_or_incomplete_payload_before_enqueue(
    tmp_path: Path,
    kind: RunnerCommandKind,
) -> None:
    run = _run()
    execution = _execution(run)
    protocol = runner_command_protocol(kind)
    commands = SimpleNamespace(enqueue=AsyncMock())
    resource_ids = {
        RunnerResourceKind.EXECUTION: execution.id,
        RunnerResourceKind.TERMINAL_SESSION: "terminal-policy-1",
        RunnerResourceKind.TARGET_HTTP_INTENT: "intent-policy-1",
        RunnerResourceKind.BROWSER_SESSION: "browser-policy-1",
    }
    resource_id = resource_ids[protocol.resource_kind]
    terminals = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(run_id=run.id, execution_id=execution.id)
        )
    )
    intents = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(run_id=run.id))
    )
    browsers = SimpleNamespace(
        get_session=AsyncMock(
            return_value=SimpleNamespace(run_id=run.id, node_id=run.node_id)
        )
    )
    service, _, _ = _service(
        tmp_path,
        run=run,
        execution=(
            execution
            if protocol.resource_kind
            in {RunnerResourceKind.EXECUTION, RunnerResourceKind.TERMINAL_SESSION}
            else None
        ),
        commands=commands,
        terminals=terminals,
        tool_call_intents=intents,
        browser_sessions=browsers,
    )
    if kind is RunnerCommandKind.EXECUTE:
        payload: dict[str, object] = {"execution_id": execution.id}
    elif kind is RunnerCommandKind.TERMINAL_START:
        payload = {
            "session_id": resource_id,
            "execution_id": execution.id,
        }
    elif kind is RunnerCommandKind.TARGET_HTTP:
        payload = {"run_id": run.id, "tool_call_ids": [resource_id]}
    elif kind is RunnerCommandKind.TARGET_HTTP_CANCEL:
        payload = {
            "launch": {
                "run_id": run.id,
                "node_id": run.node_id,
                "tool_call_id": resource_id,
            }
        }
    elif kind is RunnerCommandKind.BROWSER:
        payload = {
            "operation": "observe",
            "command": {"session_id": resource_id, "run_id": run.id},
        }
    else:
        payload = {
            "operation": "observe",
            "command": {
                "session_id": resource_id,
                "run_id": run.id,
                "node_id": run.node_id,
            },
        }
    contract_kwargs: dict[str, object] = {
        "result_schema": protocol.result_schema,
        "stop_ack_schema": protocol.stop_ack_schema,
    }
    if protocol.output_mode == "execution":
        contract_kwargs.update(
            {"max_output_bytes": 1024, "allowed_streams": ("stderr", "stdout")}
        )
    elif protocol.output_mode == "command":
        contract_kwargs.update(
            {"max_output_bytes": 1024, "allowed_streams": ("command",)}
        )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.enqueue(
            _NODE_ID,
            kind=kind,
            idempotency_key=f"invalid-payload-{kind.value}",
            payload=payload,
            run_id=run.id,
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=protocol.operation_family,
            resource_kind=protocol.resource_kind,
            resource_id=resource_id,
            execution_id=(
                execution.id
                if protocol.resource_kind
                in {RunnerResourceKind.EXECUTION, RunnerResourceKind.TERMINAL_SESSION}
                else None
            ),
            output_contract=RunnerOutputContract(**contract_kwargs),
            target=_OWNER,
        )

    assert captured.value.code == "runner_command_payload_binding_mismatch"
    commands.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_finish_policy_denial_precedes_lease_state_and_has_zero_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    execution = _execution(run)
    command = _command(execution)
    commands = SimpleNamespace(
        get=AsyncMock(return_value=command),
        finish=AsyncMock(),
    )
    service, _, _ = _service(
        tmp_path,
        run=run,
        execution=execution,
        commands=commands,
    )
    service.authenticate = AsyncMock(return_value=_credential())  # type: ignore[method-assign]

    def deny(*_args: object, **_kwargs: object) -> None:
        raise effect_policy.RunKindEffectPolicyDenied(
            effect_policy.PolicyDenialReason.RUN_KIND_UNSUPPORTED
        )

    monkeypatch.setattr(effect_policy, "require_run_kind_effect_policy", deny)
    assert command.ownership is not None
    with pytest.raises(ApplicationConflictError) as captured:
        await service.finish_command(
            _NODE_ID,
            "runner-token",
            command.id,
            lease_id="not-leased",
            state_version=0,
            envelope_digest=command.ownership.envelope_digest,
            binding_digest=command.ownership.effect_binding.binding_digest,
            succeeded=False,
        )

    assert captured.value.code == "run_kind_effect_policy_denied"
    commands.finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_finish_accepts_empty_result_with_zero_byte_contract(
    tmp_path: Path,
) -> None:
    run = _run()
    execution = _execution(run)
    command = _leased_execute_command(execution, max_result_bytes=0)
    commands = SimpleNamespace(
        get=AsyncMock(return_value=command),
        finish=AsyncMock(return_value=command),
    )
    service, _, _ = _service(
        tmp_path,
        run=run,
        execution=execution,
        commands=commands,
    )
    service.authenticate = AsyncMock(return_value=_credential())  # type: ignore[method-assign]
    assert command.ownership is not None

    await service.finish_command(
        _NODE_ID,
        "runner-token",
        command.id,
        lease_id=str(command.lease_id),
        state_version=command.state_version,
        envelope_digest=command.ownership.envelope_digest,
        binding_digest=command.ownership.effect_binding.binding_digest,
        succeeded=False,
        result={},
        error="local admission failed",
    )

    commands.finish.assert_awaited_once()
    assert commands.finish.await_args.kwargs["result"] == {}


@pytest.mark.asyncio
async def test_success_result_is_validated_before_repository_finish(
    tmp_path: Path,
) -> None:
    run = _run()
    execution = _execution(run)
    command = _leased_execute_command(execution)
    commands = SimpleNamespace(
        get=AsyncMock(return_value=command),
        finish=AsyncMock(return_value=command),
    )
    service, _, _ = _service(
        tmp_path,
        run=run,
        execution=execution,
        commands=commands,
    )
    service.authenticate = AsyncMock(return_value=_credential())  # type: ignore[method-assign]
    assert command.ownership is not None

    with pytest.raises(ApplicationConflictError) as captured:
        await service.finish_command(
            _NODE_ID,
            "runner-token",
            command.id,
            lease_id=str(command.lease_id),
            state_version=command.state_version,
            envelope_digest=command.ownership.envelope_digest,
            binding_digest=command.ownership.effect_binding.binding_digest,
            succeeded=True,
            result={"execution_id": execution.id},
        )

    assert captured.value.code == "runner_result_invalid"
    assert set(captured.value.details["invalid_fields"]) == {
        "execution_key",
        "local_execution_id",
        "owner",
        "status",
    }
    commands.finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_stop_ack_accepts_expired_original_lease_without_protocol_capability(
    tmp_path: Path,
) -> None:
    run = _run()
    command = _legacy_leased_stop()
    persisted = command.model_copy(update={"state_version": command.state_version + 1})
    commands = SimpleNamespace(
        get=AsyncMock(return_value=command),
        record_legacy_stop_ack=AsyncMock(return_value=persisted),
    )
    service, _, _ = _service(
        tmp_path,
        run=run,
        execution=None,
        commands=commands,
    )
    legacy_credential = _credential().model_copy(
        update={"protocol_capabilities": ()}
    )
    service.authenticate = AsyncMock(return_value=legacy_credential)  # type: ignore[method-assign]
    ack = _legacy_execution_stop_ack()

    result = await service.record_legacy_stop_ack(
        _NODE_ID,
        "legacy-token",
        command.id,
        lease_id=str(command.lease_id),
        succeeded=True,
        result=ack,
    )

    assert result is persisted
    commands.record_legacy_stop_ack.assert_awaited_once_with(
        command.id,
        principal=_OWNER,
        lease_id=command.lease_id,
        expected_state_version=command.state_version,
        ack=ack,
        received_at=ANY,
    )


@pytest.mark.parametrize(
    ("command", "lease_id", "succeeded", "ack", "expected_code"),
    [
        (
            _legacy_leased_stop(),
            "foreign-lease",
            True,
            _legacy_execution_stop_ack(),
            "runner_command_lease_mismatch",
        ),
        (
            _legacy_leased_stop(kind=RunnerCommandKind.EXECUTE),
            "lease-legacy-execute",
            True,
            _legacy_execution_stop_ack(),
            "runner_legacy_stop_ack_not_allowed",
        ),
        (
            _command(_execution(_run())),
            "verified-command-lease",
            True,
            _legacy_execution_stop_ack(),
            "runner_legacy_stop_ack_not_allowed",
        ),
        (
            _legacy_leased_stop(),
            "lease-legacy-cancel",
            False,
            _legacy_execution_stop_ack(),
            "runner_legacy_stop_ack_not_affirmative",
        ),
        (
            _legacy_leased_stop(),
            "lease-legacy-cancel",
            True,
            {"physical_stop_confirmed": True},
            "runner_stop_ack_invalid",
        ),
    ],
)
@pytest.mark.asyncio
async def test_legacy_stop_ack_rejects_non_narrow_callbacks(
    tmp_path: Path,
    command: RunnerCommand,
    lease_id: str,
    succeeded: bool,
    ack: dict[str, object],
    expected_code: str,
) -> None:
    commands = SimpleNamespace(
        get=AsyncMock(return_value=command),
        record_legacy_stop_ack=AsyncMock(),
    )
    service, _, _ = _service(
        tmp_path,
        run=_run(),
        execution=None,
        commands=commands,
    )
    service.authenticate = AsyncMock(return_value=_credential())  # type: ignore[method-assign]

    with pytest.raises(ApplicationConflictError) as captured:
        await service.record_legacy_stop_ack(
            _NODE_ID,
            "legacy-token",
            command.id,
            lease_id=lease_id,
            succeeded=succeeded,
            result=ack,
        )

    assert captured.value.code == expected_code
    commands.record_legacy_stop_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_stop_ack_rejects_foreign_authenticated_principal(
    tmp_path: Path,
) -> None:
    command = _legacy_leased_stop()
    commands = SimpleNamespace(
        get=AsyncMock(return_value=command),
        record_legacy_stop_ack=AsyncMock(),
    )
    service, _, _ = _service(
        tmp_path,
        run=_run(),
        execution=None,
        commands=commands,
    )
    service.authenticate = AsyncMock(  # type: ignore[method-assign]
        return_value=_credential(_FOREIGN_OWNER)
    )

    with pytest.raises(AuthenticationError) as captured:
        await service.record_legacy_stop_ack(
            _NODE_ID,
            "foreign-token",
            command.id,
            lease_id=str(command.lease_id),
            succeeded=True,
            result=_legacy_execution_stop_ack(owner=_FOREIGN_OWNER),
        )

    assert captured.value.code == "runner_command_scope_mismatch"
    commands.record_legacy_stop_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_quarantine_creates_verified_stop_from_authority_not_payload(
    tmp_path: Path,
) -> None:
    run = _run()
    execution = _execution(run)
    quarantined = RunnerCommand(
        id="legacy-quarantine",
        node_id=_NODE_ID,
        kind=RunnerCommandKind.BROWSER,
        idempotency_key="untrusted-legacy-text",
        payload={
            "execution_id": "attacker-selected-execution",
            "run_id": "attacker-selected-run",
        },
    )
    enqueued: list[RunnerCommand] = []

    async def enqueue(command: RunnerCommand) -> tuple[RunnerCommand, bool]:
        enqueued.append(command)
        return command, len(enqueued) == 1

    commands = SimpleNamespace(
        list_quarantined=AsyncMock(return_value=[quarantined]),
        enqueue=AsyncMock(side_effect=enqueue),
        mark_quarantine_reconciled=AsyncMock(),
    )
    browsers = SimpleNamespace(list_active_sessions=AsyncMock(return_value=[]))
    intents = SimpleNamespace(active_for_run=AsyncMock(return_value=[]))
    terminals = SimpleNamespace(list_active=AsyncMock(return_value=[]))
    service, _, _ = _service(
        tmp_path,
        run=run,
        execution=execution,
        commands=commands,
        browser_sessions=browsers,
        tool_call_intents=intents,
        terminals=terminals,
    )

    assert await service.reconcile_quarantined_commands() == 1
    replacement = enqueued[0]
    assert replacement.kind is RunnerCommandKind.CANCEL
    assert replacement.ownership_state is RunnerCommandOwnershipState.VERIFIED
    assert replacement.payload == {
        "execution_id": execution.id,
        "execution_key": execution.execution_key,
    }
    assert "attacker-selected" not in repr(replacement)
    commands.mark_quarantine_reconciled.assert_awaited_once_with(
        quarantined.id,
        replacement_command_id=replacement.id,
        reconciled_at=ANY,
    )
    browsers.list_active_sessions.assert_awaited_once_with(node_id=_NODE_ID)
    intents.active_for_run.assert_awaited_once_with(
        run.id,
        tool_ids=frozenset({"request_target_url"}),
    )


@pytest.mark.parametrize(
    ("ledger_case", "expected_kind"),
    [
        ("unique_match", RunnerCommandKind.TERMINAL_CLOSE),
        ("duplicate", RunnerCommandKind.CANCEL),
        ("run_mismatch", RunnerCommandKind.CANCEL),
        ("runner_mismatch", RunnerCommandKind.CANCEL),
    ],
)
@pytest.mark.asyncio
async def test_legacy_terminal_replacement_requires_one_fully_owned_session(
    tmp_path: Path,
    ledger_case: str,
    expected_kind: RunnerCommandKind,
) -> None:
    run = _run()
    execution = _execution(run).model_copy(update={"executor_type": ExecutorType.PTY})
    terminal = TerminalSession(
        id="legacy-terminal-owned",
        run_id=run.id,
        execution_id=execution.id,
        runner_id=execution.node_id,
    )
    if ledger_case == "duplicate":
        active_terminals = [
            terminal,
            terminal.model_copy(update={"id": "legacy-terminal-duplicate"}),
        ]
    elif ledger_case == "run_mismatch":
        active_terminals = [
            terminal.model_copy(update={"run_id": "foreign-run"})
        ]
    elif ledger_case == "runner_mismatch":
        active_terminals = [
            terminal.model_copy(update={"runner_id": "foreign-runner"})
        ]
    else:
        active_terminals = [terminal]
    terminals = SimpleNamespace(list_active=AsyncMock(return_value=active_terminals))
    service, _, _ = _service(
        tmp_path,
        run=run,
        execution=execution,
        terminals=terminals,
    )

    targets = await service._legacy_replacement_stops(_NODE_ID)

    assert len(targets) == 1
    target = targets[0]
    assert target.kind is expected_kind
    if expected_kind is RunnerCommandKind.TERMINAL_CLOSE:
        assert target.resource_kind is RunnerResourceKind.TERMINAL_SESSION
        assert target.resource_id == terminal.id
        assert target.payload["session_id"] == terminal.id
    else:
        assert target.resource_kind is RunnerResourceKind.EXECUTION
        assert target.resource_id == execution.id
        assert "session_id" not in target.payload


@pytest.mark.asyncio
async def test_unprovable_browser_and_target_http_legacy_resources_remain_manual(
    tmp_path: Path,
) -> None:
    run = _run()
    quarantined = RunnerCommand(
        id="legacy-unprovable",
        node_id=_NODE_ID,
        kind=RunnerCommandKind.TARGET_HTTP,
        idempotency_key="legacy-unprovable",
        payload={"tool_call_ids": ["must-not-be-trusted"]},
    )
    commands = SimpleNamespace(
        list_quarantined=AsyncMock(return_value=[quarantined]),
        enqueue=AsyncMock(),
        mark_quarantine_reconciled=AsyncMock(),
    )
    browsers = SimpleNamespace(
        list_active_sessions=AsyncMock(
            return_value=[SimpleNamespace(status=BrowserSessionStatus.ACTIVE)]
        )
    )
    intents = SimpleNamespace(active_for_run=AsyncMock(return_value=[object()]))
    terminals = SimpleNamespace(list_active=AsyncMock(return_value=[]))
    service, _, _ = _service(
        tmp_path,
        run=run,
        execution=None,
        commands=commands,
        browser_sessions=browsers,
        tool_call_intents=intents,
        terminals=terminals,
    )

    assert await service.reconcile_quarantined_commands() == 1
    commands.enqueue.assert_not_awaited()
    commands.mark_quarantine_reconciled.assert_awaited_once_with(
        quarantined.id,
        replacement_command_id=None,
        reconciled_at=ANY,
    )


@pytest.mark.asyncio
async def test_legacy_replacement_identity_is_restart_stable(tmp_path: Path) -> None:
    run = _run()
    execution = _execution(run)
    quarantined = RunnerCommand(
        id="legacy-restart",
        node_id=_NODE_ID,
        kind=RunnerCommandKind.EXECUTE,
        idempotency_key="legacy-restart",
        payload={},
        updated_at=utc_now() - timedelta(minutes=1),
    )
    persisted: RunnerCommand | None = None
    observed_ids: list[str] = []

    async def enqueue(command: RunnerCommand) -> tuple[RunnerCommand, bool]:
        nonlocal persisted
        observed_ids.append(command.id)
        if persisted is None:
            persisted = command
            return command, True
        return persisted, False

    commands = SimpleNamespace(
        list_quarantined=AsyncMock(return_value=[quarantined]),
        enqueue=AsyncMock(side_effect=enqueue),
        mark_quarantine_reconciled=AsyncMock(),
    )
    service, _, _ = _service(
        tmp_path,
        run=run,
        execution=execution,
        commands=commands,
        browser_sessions=SimpleNamespace(list_active_sessions=AsyncMock(return_value=[])),
        tool_call_intents=SimpleNamespace(active_for_run=AsyncMock(return_value=[])),
        terminals=SimpleNamespace(list_active=AsyncMock(return_value=[])),
    )

    assert await service.reconcile_quarantined_commands() == 1
    assert await service.reconcile_quarantined_commands() == 1
    assert len(observed_ids) == 2
    assert len(set(observed_ids)) == 1
