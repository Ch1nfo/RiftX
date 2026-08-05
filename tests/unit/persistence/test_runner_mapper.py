from __future__ import annotations

import pytest

from riftx.domain import (
    RunKind,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandOwnership,
    RunnerCommandOwnershipState,
    RunnerCredential,
    RunnerEffectBinding,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
    runner_payload_digest,
)
from riftx.persistence.mappers import (
    apply_runner_credential_to_record,
    runner_command_from_record,
    runner_command_ownership_to_record,
    runner_command_to_record,
    runner_credential_from_record,
    runner_credential_to_record,
    runner_effect_binding_to_record,
)


def test_runner_credential_mapper_round_trip_preserves_principal() -> None:
    credential = RunnerCredential(
        node_id="node-1",
        principal=RunnerPrincipal(instance_id="instance-1", epoch=3),
        token_hash="a" * 64,
        token_prefix="token",
    )

    assert runner_credential_from_record(runner_credential_to_record(credential)) == credential


def test_runner_credential_mapper_rejects_principal_mutation() -> None:
    credential = RunnerCredential(
        node_id="node-1",
        principal=RunnerPrincipal(instance_id="instance-1", epoch=3),
        token_hash="a" * 64,
        token_prefix="token",
    )
    record = runner_credential_to_record(credential)

    with pytest.raises(ValueError, match="principal is immutable"):
        apply_runner_credential_to_record(
            credential.model_copy(
                update={"principal": RunnerPrincipal(instance_id="instance-2", epoch=4)}
            ),
            record,
        )


def test_runner_command_mapper_round_trip_preserves_target() -> None:
    command_id = "command-1"
    principal = RunnerPrincipal(instance_id="instance-1", epoch=3)
    payload = {"launch": {"run_id": "run-1", "tool_call_id": "call-1"}}
    binding = RunnerEffectBinding(
        id="binding-1",
        run_id="run-1",
        run_kind=RunKind.GENERAL,
        node_id="node-1",
        target=principal,
        origin=RunnerCommandOrigin.APPLICATION_SERVICE,
        operation_family=RunnerOperationFamily.TARGET_HTTP,
        resource_kind=RunnerResourceKind.TARGET_HTTP_INTENT,
        resource_id="call-1",
    )
    command = RunnerCommand(
        id=command_id,
        node_id="node-1",
        target=principal,
        kind=RunnerCommandKind.TARGET_HTTP,
        idempotency_key="target-http:1",
        ownership=RunnerCommandOwnership(
            command_id=command_id,
            effect_binding=binding,
            operation=RunnerCommandKind.TARGET_HTTP,
            operation_family=RunnerOperationFamily.TARGET_HTTP,
            payload_digest=runner_payload_digest(payload),
            output_contract=RunnerOutputContract(
                result_schema="riftx.runner-result/test/v1"
            ),
        ),
        ownership_state=RunnerCommandOwnershipState.VERIFIED,
        quarantine_reason="",
        payload=payload,
    )

    assert (
        runner_command_from_record(
            runner_command_to_record(command),
            runner_command_ownership_to_record(command),
            runner_effect_binding_to_record(binding),
        )
        == command
    )
