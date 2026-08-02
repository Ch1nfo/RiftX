from __future__ import annotations

import pytest

from riftx.domain import (
    RunnerCommand,
    RunnerCommandKind,
    RunnerCredential,
    RunnerPrincipal,
)
from riftx.persistence.mappers import (
    apply_runner_credential_to_record,
    runner_command_from_record,
    runner_command_to_record,
    runner_credential_from_record,
    runner_credential_to_record,
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
    command = RunnerCommand(
        node_id="node-1",
        target=RunnerPrincipal(instance_id="instance-1", epoch=3),
        kind=RunnerCommandKind.EXECUTE,
        idempotency_key="execute:1",
    )

    assert runner_command_from_record(runner_command_to_record(command)) == command
