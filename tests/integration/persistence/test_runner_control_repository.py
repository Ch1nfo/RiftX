from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from riftx.application.errors import RepositoryConflictError
from riftx.application.services import NodeApplicationService, NodeRegistration
from riftx.domain import RunnerCommand, RunnerCommandKind, RunnerCommandStatus
from riftx.domain.base import utc_now
from riftx.persistence import (
    Database,
    SQLAlchemyNodeRepository,
    SQLAlchemyRunnerCommandRepository,
)


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
            lease_id="lease-a",
            leased_until=now + timedelta(seconds=1),
            now=now,
        ),
        repository.lease_next(
            "runner-a",
            lease_id="lease-b",
            leased_until=now + timedelta(seconds=1),
            now=now,
        ),
    )
    leased = [item for item in (first, second) if item is not None]
    assert len(leased) == 1
    assert leased[0].attempts == 1

    reclaimed = await repository.lease_next(
        "runner-a",
        lease_id="lease-reclaimed",
        leased_until=now + timedelta(seconds=3),
        now=now + timedelta(seconds=2),
    )
    assert reclaimed is not None
    assert reclaimed.id == command.id
    assert reclaimed.attempts == 2

    with pytest.raises(RepositoryConflictError, match="lease does not match"):
        await repository.finish(
            command.id,
            lease_id=leased[0].lease_id or "",
            status=RunnerCommandStatus.COMPLETED,
            result={},
            error="",
            completed_at=now + timedelta(seconds=2),
        )

    completed = await repository.finish(
        command.id,
        lease_id="lease-reclaimed",
        status=RunnerCommandStatus.COMPLETED,
        result={"accepted": True},
        error="",
        completed_at=now + timedelta(seconds=2),
    )
    repeated = await repository.finish(
        command.id,
        lease_id="lease-reclaimed",
        status=RunnerCommandStatus.COMPLETED,
        result={"ignored": True},
        error="ignored",
        completed_at=now + timedelta(seconds=4),
    )
    assert completed.status is RunnerCommandStatus.COMPLETED
    assert repeated.result == {"accepted": True}
    await database.dispose()
