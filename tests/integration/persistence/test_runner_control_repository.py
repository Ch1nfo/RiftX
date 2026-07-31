from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from riftx.application.errors import RepositoryConflictError
from riftx.application.services import NodeApplicationService, NodeRegistration
from riftx.domain import (
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandStatus,
    RunnerPrincipal,
)
from riftx.domain.base import utc_now
from riftx.persistence import (
    Database,
    SQLAlchemyNodeRepository,
    SQLAlchemyRunnerCommandRepository,
    SQLAlchemyRunnerCredentialRepository,
)

_PRINCIPAL_A = RunnerPrincipal(instance_id="runner-instance-a", epoch=1)
_PRINCIPAL_B = RunnerPrincipal(instance_id="runner-instance-b", epoch=2)


@pytest.mark.asyncio
async def test_runner_command_leases_are_idempotent_scoped_and_reclaimable(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runner-control.db'}")
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
    repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    now = utc_now()
    command = RunnerCommand(
        node_id="runner-a",
        target=_PRINCIPAL_A,
        kind=RunnerCommandKind.EXECUTE,
        idempotency_key="execute:key-1",
        payload={"execution_id": "execution-1"},
        created_at=now,
        updated_at=now,
    )
    created, was_created = await repository.enqueue(command)
    duplicate, duplicate_created = await repository.enqueue(
        command.model_copy(update={"id": "other"})
    )
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
        ),
        repository.lease_next(
            "runner-a",
            principal=_PRINCIPAL_A,
            lease_id="lease-b",
            leased_until=now + timedelta(seconds=1),
            now=now,
        ),
    )
    leased = [item for item in (first, second) if item is not None]
    assert len(leased) == 1
    assert leased[0].attempts == 1

    renewed = await repository.renew_lease(
        command.id,
        principal=_PRINCIPAL_A,
        lease_id=leased[0].lease_id or "",
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
    )
    assert not_reclaimed is None

    reclaimed = await repository.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id="lease-reclaimed",
        leased_until=now + timedelta(seconds=6),
        now=now + timedelta(seconds=4),
    )
    assert reclaimed is not None
    assert reclaimed.id == command.id
    assert reclaimed.attempts == 2

    with pytest.raises(RepositoryConflictError, match="lease does not match"):
        await repository.finish(
            command.id,
            principal=_PRINCIPAL_A,
            lease_id=leased[0].lease_id or "",
            status=RunnerCommandStatus.COMPLETED,
            result={},
            error="",
            completed_at=now + timedelta(seconds=4),
        )

    completed = await repository.finish(
        command.id,
        principal=_PRINCIPAL_A,
        lease_id="lease-reclaimed",
        status=RunnerCommandStatus.COMPLETED,
        result={"accepted": True},
        error="",
        completed_at=now + timedelta(seconds=4),
    )
    repeated = await repository.finish(
        command.id,
        principal=_PRINCIPAL_A,
        lease_id="lease-reclaimed",
        status=RunnerCommandStatus.COMPLETED,
        result={"ignored": True},
        error="ignored",
        completed_at=now + timedelta(seconds=4),
    )
    assert completed.status is RunnerCommandStatus.COMPLETED
    assert repeated.result == {"accepted": True}

    with pytest.raises(RepositoryConflictError, match="lease does not match or expired"):
        await repository.renew_lease(
            command.id,
            principal=_PRINCIPAL_A,
            lease_id="lease-reclaimed",
            leased_until=now + timedelta(seconds=10),
            now=now + timedelta(seconds=5),
        )

    pending_execute = RunnerCommand(
        node_id="runner-a",
        target=_PRINCIPAL_A,
        kind=RunnerCommandKind.EXECUTE,
        idempotency_key="execute:pending",
        created_at=now + timedelta(seconds=5),
        updated_at=now + timedelta(seconds=5),
    )
    pending_cancel = RunnerCommand(
        node_id="runner-a",
        target=_PRINCIPAL_A,
        kind=RunnerCommandKind.CANCEL,
        idempotency_key="cancel:pending",
        created_at=now + timedelta(seconds=6),
        updated_at=now + timedelta(seconds=6),
    )
    await repository.enqueue(pending_execute)
    await repository.enqueue(pending_cancel)

    safety_first = await repository.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id="lease-safety",
        leased_until=now + timedelta(seconds=20),
        now=now + timedelta(seconds=10),
    )

    assert safety_first is not None
    assert safety_first.id == pending_cancel.id
    assert safety_first.kind is RunnerCommandKind.CANCEL
    await database.dispose()


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
    await nodes.register(
        NodeRegistration(
            node_id="runner-a",
            name="Runner A",
            platform="linux",
            architecture="x86_64",
        )
    )
    repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    now = utc_now()
    await repository.enqueue(
        RunnerCommand(
            node_id="runner-a",
            target=_PRINCIPAL_A,
            kind=RunnerCommandKind.TARGET_HTTP,
            idempotency_key=f"effect:{kind.value}",
            created_at=now,
            updated_at=now,
        )
    )
    no_safety = await repository.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id=f"no-safety:{kind.value}",
        leased_until=now + timedelta(seconds=30),
        now=now + timedelta(milliseconds=500),
        safety_only=True,
    )
    assert no_safety is None
    safety = RunnerCommand(
        node_id="runner-a",
        target=_PRINCIPAL_A,
        kind=kind,
        idempotency_key=f"safety:{kind.value}",
        created_at=now + timedelta(seconds=1),
        updated_at=now + timedelta(seconds=1),
    )
    await repository.enqueue(safety)

    leased = await repository.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id=f"lease:{kind.value}",
        leased_until=now + timedelta(seconds=30),
        now=now + timedelta(seconds=2),
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
    await nodes.register(
        NodeRegistration(
            node_id="runner-a",
            name="Runner A",
            platform="linux",
            architecture="x86_64",
        )
    )
    repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    now = utc_now()
    command = RunnerCommand(
        node_id="runner-a",
        target=_PRINCIPAL_A,
        kind=RunnerCommandKind.EXECUTE,
        idempotency_key="execute:fenced",
        created_at=now,
        updated_at=now,
    )
    await repository.enqueue(command)

    assert (
        await repository.lease_next(
            "runner-a",
            principal=_PRINCIPAL_B,
            lease_id="lease-b",
            leased_until=now + timedelta(seconds=10),
            now=now,
        )
        is None
    )
    leased = await repository.lease_next(
        "runner-a",
        principal=_PRINCIPAL_A,
        lease_id="lease-a",
        leased_until=now + timedelta(seconds=10),
        now=now,
    )
    assert leased is not None

    with pytest.raises(RepositoryConflictError, match="lease does not match or expired"):
        await repository.renew_lease(
            command.id,
            principal=_PRINCIPAL_B,
            lease_id="lease-a",
            leased_until=now + timedelta(seconds=20),
            now=now + timedelta(seconds=1),
        )
    with pytest.raises(RepositoryConflictError, match="owner does not match"):
        await repository.finish(
            command.id,
            principal=_PRINCIPAL_B,
            lease_id="lease-a",
            status=RunnerCommandStatus.COMPLETED,
            result={},
            error="",
            completed_at=now + timedelta(seconds=1),
        )

    completed = await repository.finish(
        command.id,
        principal=_PRINCIPAL_A,
        lease_id="lease-a",
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
