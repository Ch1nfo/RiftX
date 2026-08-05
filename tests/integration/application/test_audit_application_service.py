from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.unit.domain.test_audit_domain import _contract as domain_contract

import riftx.persistence.audit_uow as audit_uow_module
from riftx.application.errors import (
    ApplicationConflictError,
    RepositoryIntegrityError,
    ResourceNotAccessibleError,
    ServiceUnavailableError,
)
from riftx.application.ports import (
    AuditAggregate,
    AuditCreationUnitOfWork,
    AuditDraftAggregateFactory,
    AuditDraftCreationEnvelope,
    AuditEngagementScope,
)
from riftx.application.services import (
    AuditApplicationService,
    AuditContractBlueprint,
    CreateAuditDraft,
)
from riftx.domain import (
    AuditContract,
    AuditLifecycleStatus,
    AuditProject,
    Engagement,
    RunStatus,
    SourceTarget,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAuditAggregateReadRepository,
    SQLAlchemyAuditContractRepository,
    SQLAlchemyAuditCreationUnitOfWork,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunEventRepository,
    compare_and_set_audit_scan,
)
from riftx.persistence.orm import AuditProjectRecord
from riftx.persistence.transactions import serialized_write

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)
SOURCE_PATH = "/definitely/not-present/riftx-code-audit/source"

REQUEST_ONE = "11111111-1111-4111-8111-111111111111"
REQUEST_TWO = "22222222-2222-4222-8222-222222222222"
REQUEST_THREE = "33333333-3333-4333-8333-333333333333"

_CREATION_TABLES = (
    "engagements",
    "audit_projects",
    "runs",
    "run_events",
    "audit_contracts",
    "audit_scans",
    "audit_client_requests",
)
_EMPTY_AUDIT_TABLES = (
    "source_snapshots",
    "audit_start_intents",
    "audit_phase_runs",
    "audit_scope_units",
    "audit_work_items",
)
_EXPECTED_CREATED_COUNTS = {
    "engagements": 1,
    "audit_projects": 1,
    "runs": 1,
    "run_events": 2,
    "audit_contracts": 1,
    "audit_scans": 1,
    "audit_client_requests": 1,
}
_FAILPOINTS = (
    "after_engagement",
    "after_project",
    "after_run",
    "after_run_event",
    "after_contract",
    "after_scan",
    "after_contract_scan",
    "after_audit_event",
    "after_client_request",
)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _replace_model[T](value: T, **updates: object) -> T:
    payload = value.model_dump(mode="python")  # type: ignore[attr-defined]
    payload.update(updates)
    return type(value).model_validate(payload)  # type: ignore[attr-defined,no-any-return]


def _rebind_envelope_workspace(
    envelope: AuditDraftCreationEnvelope,
    workspace_root: Path,
) -> AuditDraftCreationEnvelope:
    return replace(
        envelope,
        run=_replace_model(
            envelope.run,
            workspace_path=str(workspace_root / envelope.audit.id),
        ),
    )


class _DelegatingAuditFactory:
    def __init__(self, delegate: AuditDraftAggregateFactory) -> None:
        self._delegate = delegate

    @property
    def client_request_id(self) -> str:
        return self._delegate.client_request_id

    @property
    def request_digest(self) -> str:
        return self._delegate.request_digest

    @property
    def repository_identity_digest(self) -> str:
        return self._delegate.repository_identity_digest

    @property
    def requested_engagement_id(self) -> str | None:
        return self._delegate.requested_engagement_id

    @property
    def authorization_reference(self) -> str:
        return self._delegate.authorization_reference

    @property
    def authorized_engagement_scope(self) -> AuditEngagementScope:
        return self._delegate.authorized_engagement_scope

    @property
    def workspace_root(self) -> str:
        return self._delegate.workspace_root

    @property
    def source_repository_path(self) -> str:
        return self._delegate.source_repository_path

    def build_engagement(self) -> Engagement:
        return self._delegate.build_engagement()

    def build_project(self, engagement: Engagement) -> AuditProject:
        return self._delegate.build_project(engagement)

    def build(
        self,
        project: AuditProject,
        engagement: Engagement,
    ) -> AuditDraftCreationEnvelope:
        return self._delegate.build(project, engagement)


class _LyingSourceFactory(_DelegatingAuditFactory):
    def __init__(
        self,
        delegate: AuditDraftAggregateFactory,
        *,
        dangerous_workspace_root: Path,
        declared_source_path: Path,
    ) -> None:
        super().__init__(delegate)
        self._dangerous_workspace_root = dangerous_workspace_root
        self._declared_source_path = declared_source_path

    @property
    def workspace_root(self) -> str:
        return str(self._dangerous_workspace_root)

    @property
    def source_repository_path(self) -> str:
        return str(self._declared_source_path)

    def build(
        self,
        project: AuditProject,
        engagement: Engagement,
    ) -> AuditDraftCreationEnvelope:
        return _rebind_envelope_workspace(
            super().build(project, engagement),
            self._dangerous_workspace_root,
        )


class _StatefulWorkspaceFactory(_DelegatingAuditFactory):
    def __init__(
        self,
        delegate: AuditDraftAggregateFactory,
        *,
        dangerous_workspace_root: Path,
    ) -> None:
        super().__init__(delegate)
        self._safe_workspace_root = delegate.workspace_root
        self._dangerous_workspace_root = dangerous_workspace_root
        self.workspace_root_reads = 0

    @property
    def workspace_root(self) -> str:
        self.workspace_root_reads += 1
        if self.workspace_root_reads == 1:
            return self._safe_workspace_root
        return str(self._dangerous_workspace_root)

    def build(
        self,
        project: AuditProject,
        engagement: Engagement,
    ) -> AuditDraftCreationEnvelope:
        return _rebind_envelope_workspace(
            super().build(project, engagement),
            self._dangerous_workspace_root,
        )


class _FactoryWrappingCreationUnitOfWork:
    def __init__(
        self,
        delegate: AuditCreationUnitOfWork,
        factory_wrapper: Callable[[AuditDraftAggregateFactory], AuditDraftAggregateFactory],
    ) -> None:
        self._delegate = delegate
        self._factory_wrapper = factory_wrapper

    async def create_draft(
        self,
        factory: AuditDraftAggregateFactory,
    ) -> tuple[AuditAggregate, bool]:
        return await self._delegate.create_draft(self._factory_wrapper(factory))


class _FakeScalarRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _FakeScalarsSession:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    async def scalars(self, _statement: object) -> _FakeScalarRows:
        return _FakeScalarRows(self._rows)


def _blueprint(*, source_path: str = SOURCE_PATH) -> AuditContractBlueprint:
    template = domain_contract()
    source_target = _replace_model(
        template.source_target,
        repository_path=source_path,
    )
    contract = _replace_model(template, source_target=source_target)
    assert isinstance(contract, AuditContract)
    assert isinstance(source_target, SourceTarget)
    return AuditContractBlueprint.from_contract(contract)


def _command(
    client_request_id: str = REQUEST_ONE,
    *,
    repository_seed: str = "repository-one",
    authorization_seed: str = "authorization-one",
    project_name: str = "RiftX",
    engagement_id: str | None = None,
    source_path: str = SOURCE_PATH,
) -> CreateAuditDraft:
    return CreateAuditDraft(
        client_request_id=client_request_id,
        project_name=project_name,
        repository_identity_digest=_digest(repository_seed),
        authorization_reference=_digest(authorization_seed),
        contract=_blueprint(source_path=source_path),
        engagement_id=engagement_id,
        default_branch="main",
    )


def _service(
    database: Database,
    workspace_root: Path,
    *,
    failpoint: Callable[[str], None] | None = None,
    factory_wrapper: Callable[[AuditDraftAggregateFactory], AuditDraftAggregateFactory]
    | None = None,
) -> AuditApplicationService:
    creation_uow: AuditCreationUnitOfWork = SQLAlchemyAuditCreationUnitOfWork(
        database.session_factory,
        creation_failpoint=failpoint,
    )
    if factory_wrapper is not None:
        creation_uow = _FactoryWrappingCreationUnitOfWork(
            creation_uow,
            factory_wrapper,
        )
    return AuditApplicationService(
        creation_uow=creation_uow,
        aggregate_repository=SQLAlchemyAuditAggregateReadRepository(database.session_factory),
        feature_enabled=True,
        workspace_root=workspace_root,
        clock=lambda: NOW,
    )


async def _database(path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.create_schema()
    return database


async def _table_counts(
    database: Database,
    tables: tuple[str, ...] = _CREATION_TABLES + _EMPTY_AUDIT_TABLES,
) -> dict[str, int]:
    async with database.engine.connect() as connection:
        return {
            table: int(await connection.scalar(text(f'SELECT count(*) FROM "{table}"')) or 0)
            for table in tables
        }


async def _assert_created_counts(database: Database, *, audits: int = 1) -> None:
    counts = await _table_counts(database)
    expected = {
        "engagements": 1,
        "audit_projects": 1,
        "runs": audits,
        "run_events": audits * 2,
        "audit_contracts": audits,
        "audit_scans": audits,
        "audit_client_requests": audits,
    }
    assert {table: counts[table] for table in _CREATION_TABLES} == expected
    assert {table: counts[table] for table in _EMPTY_AUDIT_TABLES} == {
        table: 0 for table in _EMPTY_AUDIT_TABLES
    }


async def _advance_to_queued(database: Database, aggregate: object) -> None:
    stored = aggregate.audit  # type: ignore[attr-defined]
    stored_contract = aggregate.contract  # type: ignore[attr-defined]
    sealed_contract = stored_contract.value.seal(at=NOW)
    persisted_contract, contract_changed = await SQLAlchemyAuditContractRepository(
        database.session_factory
    ).compare_and_set(stored_contract, sealed_contract)
    assert contract_changed is True
    assert persisted_contract.value == sealed_contract
    queued = stored.value.transition_to(  # type: ignore[union-attr]
        AuditLifecycleStatus.QUEUED,
        at=NOW + timedelta(seconds=1),
    )
    async with serialized_write(database.session_factory) as session:
        await session.execute(
            text("UPDATE runs SET status=:status WHERE id=:run_id"),
            {
                "status": RunStatus.PREPARING.value,
                "run_id": aggregate.run.id,  # type: ignore[attr-defined]
            },
        )
        persisted, changed = await compare_and_set_audit_scan(session, stored, queued)
        assert changed is True
        assert persisted.state_version == stored.state_version + 1


async def _raw_update_without_foreign_keys(
    database: Database,
    statement: str,
    parameters: dict[str, object],
) -> None:
    async with database.engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            await connection.execute(text(statement), parameters)
            await connection.commit()
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")


async def test_create_draft_persists_one_complete_minimal_aggregate_and_safe_events(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path / "audit-create.db")
    command = _command()
    service = _service(database, tmp_path / "audit-workspaces")
    try:
        result = await service.create_draft(command)

        assert result.created is True
        aggregate = result.aggregate
        assert aggregate.audit.value.lifecycle_status is AuditLifecycleStatus.DRAFT
        assert aggregate.audit.state_version == 1
        assert aggregate.contract.state_version == 1
        assert aggregate.project.state_version == 1
        assert aggregate.run.status is RunStatus.CREATED
        assert aggregate.run.id == aggregate.audit.value.run_id
        assert aggregate.run.engagement_id == aggregate.engagement.id
        assert aggregate.project.value.engagement_id == aggregate.engagement.id
        assert aggregate.contract.value.audit_id == aggregate.audit.value.id
        assert aggregate.client_request.audit_id == aggregate.audit.value.id
        assert aggregate.client_request.client_request_id == REQUEST_ONE
        assert aggregate.run.workspace_path == str(
            tmp_path / "audit-workspaces" / aggregate.audit.value.id
        )

        await _assert_created_counts(database)
        events = list(
            await SQLAlchemyRunEventRepository(database.session_factory).list_after(
                aggregate.run.id
            )
        )
        assert [(event.sequence, event.event_type) for event in events] == [
            (1, "run.created"),
            (2, "audit.created"),
        ]
        assert events[0].payload == {"status": RunStatus.CREATED.value}
        assert events[1].payload == {
            "audit_id": aggregate.audit.value.id,
            "project_id": aggregate.project.value.id,
            "lifecycle_status": AuditLifecycleStatus.DRAFT.value,
            "mode": aggregate.audit.value.mode.value,
            "analysis_profile": aggregate.audit.value.analysis_profile.value,
            "contract_digest": aggregate.audit.value.contract_digest,
        }
        serialized_events = json.dumps(
            [event.payload for event in events],
            sort_keys=True,
        )
        assert SOURCE_PATH not in serialized_events
        assert command.authorization_reference not in serialized_events
        assert aggregate.contract.value.canonical_contract_json not in serialized_events
        assert "canonical_contract_json" not in serialized_events
        assert "authorization_reference" not in serialized_events
        assert "repository_path" not in serialized_events
    finally:
        await database.dispose()


async def test_exact_replay_returns_current_persisted_audit_without_new_rows(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path / "audit-replay.db")
    command = _command()
    service = _service(database, tmp_path / "audit-workspaces")
    try:
        first = await service.create_draft(command)
        await _advance_to_queued(database, first.aggregate)
        counts_before = await _table_counts(database)

        replay = await service.create_draft(command)

        assert replay.created is False
        assert replay.aggregate.audit.value.id == first.aggregate.audit.value.id
        assert replay.aggregate.run.id == first.aggregate.run.id
        assert replay.aggregate.audit.value.lifecycle_status is AuditLifecycleStatus.QUEUED
        assert replay.aggregate.audit.state_version == first.aggregate.audit.state_version + 1
        assert await _table_counts(database) == counts_before
    finally:
        await database.dispose()


async def test_same_request_id_with_different_payload_is_a_stable_bounded_conflict(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path / "audit-request-conflict.db")
    command = _command()
    service = _service(database, tmp_path / "audit-workspaces")
    try:
        original = await service.create_draft(command)
        counts_before = await _table_counts(database)
        changed = replace(command, project_name="Different Project")

        with pytest.raises(ApplicationConflictError) as captured:
            await service.create_draft(changed)

        assert captured.value.code == "audit_idempotency_conflict"
        assert captured.value.details == {}
        assert SOURCE_PATH not in str(captured.value)
        assert (
            original.aggregate.audit.value.id
            == (await service.get(original.aggregate.audit.value.id)).audit.value.id
        )
        assert await _table_counts(database) == counts_before
    finally:
        await database.dispose()


async def test_different_requests_for_one_repository_reuse_exactly_one_project(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path / "audit-project-reuse.db")
    service = _service(database, tmp_path / "audit-workspaces")
    try:
        first = await service.create_draft(_command(REQUEST_ONE))
        second = await service.create_draft(_command(REQUEST_TWO))

        assert first.created is True and second.created is True
        assert first.aggregate.audit.value.id != second.aggregate.audit.value.id
        assert first.aggregate.run.id != second.aggregate.run.id
        assert first.aggregate.project.value.id == second.aggregate.project.value.id
        assert first.aggregate.engagement.id == second.aggregate.engagement.id
        await _assert_created_counts(database, audits=2)
    finally:
        await database.dispose()


async def test_explicit_authorized_engagement_is_reused_and_cross_domain_is_rejected(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path / "audit-explicit-engagement.db")
    authorization = _digest("authorization-one")
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    try:
        for engagement_id in ("engagement-authorized", "engagement-foreign"):
            await engagements.create(
                Engagement(
                    id=engagement_id,
                    name=engagement_id,
                    authorization_reference=authorization,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        service = _service(database, tmp_path / "audit-workspaces")
        authorized = await service.create_draft(
            _command(REQUEST_ONE, engagement_id="engagement-authorized")
        )
        counts_before = await _table_counts(database)

        assert authorized.aggregate.engagement.id == "engagement-authorized"
        assert counts_before["engagements"] == 2
        with pytest.raises(ApplicationConflictError) as captured:
            await service.create_draft(_command(REQUEST_TWO, engagement_id="engagement-foreign"))

        assert captured.value.code == "audit_creation_conflict"
        assert await _table_counts(database) == counts_before
        assert (
            await service.get(
                authorized.aggregate.audit.value.id,
                engagement_id="engagement-authorized",
            )
            == authorized.aggregate
        )
        with pytest.raises(Exception) as hidden:
            await service.get(
                authorized.aggregate.audit.value.id,
                engagement_id="engagement-foreign",
            )
        assert type(hidden.value).__name__ == "EntityNotFoundError"
    finally:
        await database.dispose()


@pytest.mark.parametrize("fail_stage", _FAILPOINTS)
async def test_every_creation_failpoint_rolls_back_the_entire_aggregate(
    tmp_path: Path,
    fail_stage: str,
) -> None:
    database = await _database(tmp_path / f"audit-failpoint-{fail_stage}.db")

    def failpoint(stage: str) -> None:
        if stage == fail_stage:
            raise RuntimeError(f"injected creation failure at {stage}")

    command = _command()
    failing = _service(
        database,
        tmp_path / "audit-workspaces",
        failpoint=failpoint,
    )
    try:
        with pytest.raises(RuntimeError, match=fail_stage):
            await failing.create_draft(command)

        counts = await _table_counts(database)
        assert counts == {table: 0 for table in counts}

        recovered = await _service(
            database,
            tmp_path / "audit-workspaces",
        ).create_draft(command)
        assert recovered.created is True
        await _assert_created_counts(database)
    finally:
        await database.dispose()


async def test_non_integrity_driver_failure_is_redacted_and_rolls_back(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path / "audit-driver-failure.db")
    command = _command()
    sensitive_contract = command.contract.template.model_dump_json()

    def failpoint(stage: str) -> None:
        if stage == "after_contract":
            raise OperationalError(
                "INSERT INTO audit_contracts (canonical_contract_json) VALUES (?)",
                {
                    "canonical_contract_json": sensitive_contract,
                    "source_path": SOURCE_PATH,
                },
                RuntimeError("simulated driver failure"),
            )

    service = _service(
        database,
        tmp_path / "audit-workspaces",
        failpoint=failpoint,
    )
    try:
        with pytest.raises(ServiceUnavailableError) as captured:
            await service.create_draft(command)

        assert captured.value.code == "audit_persistence_unavailable"
        exception: BaseException | None = captured.value
        while exception is not None:
            rendered = str(exception)
            assert SOURCE_PATH not in rendered
            assert sensitive_contract not in rendered
            exception = exception.__context__
        assert await _table_counts(database) == {
            table: 0 for table in _CREATION_TABLES + _EMPTY_AUDIT_TABLES
        }
    finally:
        await database.dispose()


async def test_dispose_reopen_preserves_get_list_and_request_replay(tmp_path: Path) -> None:
    database_path = tmp_path / "audit-reopen.db"
    database = await _database(database_path)
    command = _command()
    service = _service(database, tmp_path / "audit-workspaces")
    created = await service.create_draft(command)
    audit_id = created.aggregate.audit.value.id
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    try:
        reopened_service = _service(reopened, tmp_path / "audit-workspaces")
        loaded = await reopened_service.get(audit_id)
        replay = await reopened_service.create_draft(command)
        listed = await reopened_service.list(
            project_id=created.aggregate.project.value.id,
            engagement_id=created.aggregate.engagement.id,
        )

        assert loaded == created.aggregate
        assert replay.created is False
        assert replay.aggregate == created.aggregate
        assert list(listed) == [created.aggregate]
        await _assert_created_counts(reopened)
    finally:
        await reopened.dispose()


async def test_concurrent_exact_requests_create_one_aggregate(tmp_path: Path) -> None:
    database_path = tmp_path / "audit-concurrent-exact.db"
    first_database = await _database(database_path)
    second_database = Database(f"sqlite+aiosqlite:///{database_path}")
    command = _command()
    first_service = _service(first_database, tmp_path / "audit-workspaces")
    second_service = _service(second_database, tmp_path / "audit-workspaces")
    try:
        first, second = await asyncio.gather(
            first_service.create_draft(command),
            second_service.create_draft(command),
        )

        assert sorted((first.created, second.created)) == [False, True]
        assert first.aggregate.audit.value.id == second.aggregate.audit.value.id
        assert first.aggregate.run.id == second.aggregate.run.id
        assert first.aggregate.client_request == second.aggregate.client_request
        await _assert_created_counts(first_database)
    finally:
        await second_database.dispose()
        await first_database.dispose()


async def test_concurrent_distinct_requests_for_one_repository_share_project(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-concurrent-project.db"
    first_database = await _database(database_path)
    second_database = Database(f"sqlite+aiosqlite:///{database_path}")
    first_service = _service(first_database, tmp_path / "audit-workspaces")
    second_service = _service(second_database, tmp_path / "audit-workspaces")
    try:
        first, second = await asyncio.gather(
            first_service.create_draft(_command(REQUEST_ONE)),
            second_service.create_draft(_command(REQUEST_TWO)),
        )

        assert first.created is True and second.created is True
        assert first.aggregate.audit.value.id != second.aggregate.audit.value.id
        assert first.aggregate.project.value.id == second.aggregate.project.value.id
        assert first.aggregate.engagement.id == second.aggregate.engagement.id
        await _assert_created_counts(first_database, audits=2)
    finally:
        await second_database.dispose()
        await first_database.dispose()


async def test_project_race_recovers_exact_request_without_leaking_temporary_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This deterministic interleaving covers the PostgreSQL READ COMMITTED
    # no-gap-lock recovery branch locally. A real PostgreSQL barrier test remains
    # a release-CI gate; this SQLite test intentionally does not claim that proof.
    database = await _database(tmp_path / "audit-project-race-exact.db")
    command = _command()
    winner = await _service(
        database,
        tmp_path / "audit-workspaces",
    ).create_draft(command)
    counts_before = await _table_counts(database)
    request_locks: list[bool] = []
    project_locks: list[bool] = []
    stages: list[str] = []
    original_request_reader = audit_uow_module._read_client_request
    original_project_reader = audit_uow_module._project_by_repository_identity

    async def stale_request_once(
        session: AsyncSession,
        identity: audit_uow_module._AuditFactoryIdentity,
        *,
        for_update: bool,
    ) -> AuditAggregate | None:
        request_locks.append(for_update)
        if len(request_locks) == 1:
            return None
        return await original_request_reader(
            session,
            identity,
            for_update=for_update,
        )

    async def stale_project_once(
        session: AsyncSession,
        repository_identity_digest: str,
        *,
        for_update: bool,
    ) -> AuditProjectRecord | None:
        project_locks.append(for_update)
        if len(project_locks) == 1:
            return None
        return await original_project_reader(
            session,
            repository_identity_digest,
            for_update=for_update,
        )

    monkeypatch.setattr(audit_uow_module, "_read_client_request", stale_request_once)
    monkeypatch.setattr(
        audit_uow_module,
        "_project_by_repository_identity",
        stale_project_once,
    )
    service = _service(
        database,
        tmp_path / "audit-workspaces",
        failpoint=stages.append,
    )
    try:
        replay = await service.create_draft(command)

        assert request_locks == [True, False]
        assert project_locks == [True, False]
        assert replay.created is False
        assert replay.aggregate == winner.aggregate
        assert stages == ["after_engagement"]
        assert await _table_counts(database) == counts_before
    finally:
        await database.dispose()


async def test_project_race_retries_distinct_request_against_committed_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = await _database(tmp_path / "audit-project-race-distinct.db")
    winner = await _service(
        database,
        tmp_path / "audit-workspaces",
    ).create_draft(_command(REQUEST_ONE))
    project_locks: list[bool] = []
    stages: list[str] = []
    original_project_reader = audit_uow_module._project_by_repository_identity

    async def stale_project_once(
        session: AsyncSession,
        repository_identity_digest: str,
        *,
        for_update: bool,
    ) -> AuditProjectRecord | None:
        project_locks.append(for_update)
        if len(project_locks) == 1:
            return None
        return await original_project_reader(
            session,
            repository_identity_digest,
            for_update=for_update,
        )

    monkeypatch.setattr(
        audit_uow_module,
        "_project_by_repository_identity",
        stale_project_once,
    )
    service = _service(
        database,
        tmp_path / "audit-workspaces",
        failpoint=stages.append,
    )
    try:
        created = await service.create_draft(_command(REQUEST_TWO))

        assert project_locks == [True, False, True]
        assert created.created is True
        assert created.aggregate.audit.value.id != winner.aggregate.audit.value.id
        assert created.aggregate.run.id != winner.aggregate.run.id
        assert created.aggregate.client_request != winner.aggregate.client_request
        assert created.aggregate.project == winner.aggregate.project
        assert created.aggregate.engagement == winner.aggregate.engagement
        assert stages.count("after_engagement") == 1
        assert "after_project" not in stages
        await _assert_created_counts(database, audits=2)
    finally:
        await database.dispose()


async def test_create_draft_never_reads_git_connects_temporal_or_makes_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = await _database(tmp_path / "audit-no-effects.db")
    workspace_root = tmp_path / "never-created-workspaces"
    source = tmp_path / "no-effects-source"
    source_path = str(source)
    command = _command(source_path=source_path)
    service = _service(database, workspace_root)

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("draft creation attempted a forbidden host effect")

    async def forbidden_async(*_: object, **__: object) -> None:
        raise AssertionError("draft creation attempted a forbidden async host effect")

    import riftx.temporal.connection as temporal_connection
    from riftx.temporal.runtime import TemporalRunClient

    for method in (
        "exists",
        "is_dir",
        "iterdir",
        "mkdir",
        "open",
        "read_bytes",
        "read_text",
        "resolve",
        "stat",
    ):
        monkeypatch.setattr(Path, method, forbidden)
    for operation in ("Popen", "check_call", "check_output", "run"):
        monkeypatch.setattr(subprocess, operation, forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_async)
    monkeypatch.setattr(temporal_connection, "connect_temporal", forbidden_async)
    monkeypatch.setattr(TemporalRunClient, "start_run", forbidden_async)

    try:
        result = await service.create_draft(command)
        monkeypatch.undo()

        assert result.created is True
        assert not source.exists()
        assert not workspace_root.exists()
        counts = await _table_counts(database)
        assert counts["audit_start_intents"] == 0
        events = await SQLAlchemyRunEventRepository(database.session_factory).list_after(
            result.aggregate.run.id
        )
        assert {event.event_type for event in events} == {"run.created", "audit.created"}
    finally:
        await database.dispose()


async def test_factory_cannot_hide_contract_source_behind_a_safe_declared_path(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path / "audit-lying-source-factory.db")
    source_root = tmp_path / "sensitive-source"
    declared_source = tmp_path / "claimed-source"
    workspace_root = tmp_path / "audit-workspaces"
    service = _service(
        database,
        workspace_root,
        factory_wrapper=lambda delegate: _LyingSourceFactory(
            delegate,
            dangerous_workspace_root=source_root,
            declared_source_path=declared_source,
        ),
    )
    try:
        with pytest.raises(ApplicationConflictError) as captured:
            await service.create_draft(_command(source_path=str(source_root)))

        assert captured.value.code == "audit_creation_conflict"
        assert str(source_root) not in str(captured.value)
        counts = await _table_counts(database)
        assert counts == {table: 0 for table in counts}
    finally:
        await database.dispose()


async def test_factory_identity_is_frozen_before_stateful_workspace_can_change(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path / "audit-stateful-factory.db")
    source_root = tmp_path / "sensitive-source"
    wrapped_factory: _StatefulWorkspaceFactory | None = None

    def wrap_factory(
        delegate: AuditDraftAggregateFactory,
    ) -> AuditDraftAggregateFactory:
        nonlocal wrapped_factory
        wrapped_factory = _StatefulWorkspaceFactory(
            delegate,
            dangerous_workspace_root=source_root,
        )
        return wrapped_factory

    service = _service(
        database,
        tmp_path / "audit-workspaces",
        factory_wrapper=wrap_factory,
    )
    try:
        with pytest.raises(ApplicationConflictError) as captured:
            await service.create_draft(_command(source_path=str(source_root)))

        assert captured.value.code == "audit_creation_conflict"
        assert wrapped_factory is not None
        assert wrapped_factory.workspace_root_reads == 1
        counts = await _table_counts(database)
        assert counts == {table: 0 for table in counts}
    finally:
        await database.dispose()


async def test_run_projection_tamper_is_rejected_by_get_list_and_replay(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path / "audit-projection-tamper.db")
    command = _command()
    service = _service(database, tmp_path / "audit-workspaces")
    try:
        created = await service.create_draft(command)
        audit_id = created.aggregate.audit.value.id
        async with database.engine.begin() as connection:
            await connection.execute(
                text("UPDATE runs SET status=:status WHERE id=:run_id"),
                {
                    "status": RunStatus.PREPARING.value,
                    "run_id": created.aggregate.run.id,
                },
            )

        for operation in (
            lambda: service.get(audit_id),
            lambda: service.list(run_id=created.aggregate.run.id),
            lambda: service.create_draft(command),
        ):
            with pytest.raises(ApplicationConflictError) as captured:
                await operation()
            assert captured.value.code == "audit_run_state_conflict"
    finally:
        await database.dispose()


async def test_corrupted_client_request_binding_fails_closed_on_aggregate_reads(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path / "audit-aggregate-corruption.db")
    service = _service(database, tmp_path / "audit-workspaces")
    try:
        created = await service.create_draft(_command())
        audit_id = created.aggregate.audit.value.id
        await _raw_update_without_foreign_keys(
            database,
            "UPDATE audit_client_requests "
            "SET temporal_workflow_id=:workflow_id "
            "WHERE client_request_id=:client_request_id",
            {
                "workflow_id": "riftx-code-audit-wrong-audit",
                "client_request_id": REQUEST_ONE,
            },
        )

        for operation in (
            lambda: service.get(audit_id),
            lambda: service.list(project_id=created.aggregate.project.value.id),
        ):
            with pytest.raises(RepositoryIntegrityError) as captured:
                await operation()
            assert captured.value.entity == "AuditClientRequest"
            assert SOURCE_PATH not in str(captured.value)
    finally:
        await database.dispose()


async def test_duplicate_project_natural_identity_rowset_fails_closed() -> None:
    # SQLite's UNIQUE constraint prevents materializing this state normally.
    # The controlled rowset models a missing or damaged production constraint.
    session = cast(
        AsyncSession,
        _FakeScalarsSession([AuditProjectRecord(), AuditProjectRecord()]),
    )

    with pytest.raises(RepositoryIntegrityError) as captured:
        await audit_uow_module._project_by_repository_identity(
            session,
            _digest("duplicate-repository"),
            for_update=True,
        )

    assert captured.value.entity == "AuditProject"
    assert captured.value.reason_code == "ambiguous_natural_identity"


async def test_duplicate_request_by_audit_rowset_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This likewise simulates a database whose per-Audit request uniqueness was
    # never installed or was damaged, without weakening the real test schema.
    async def fake_bundle(
        _session: AsyncSession,
        _audit_id: str,
        *,
        for_update: bool = False,
    ) -> tuple[object, object, object, object, object]:
        del for_update
        return (object(), object(), object(), object(), object())

    monkeypatch.setattr(
        audit_uow_module,
        "load_validated_audit_scan",
        fake_bundle,
    )
    session = cast(
        AsyncSession,
        _FakeScalarsSession([object(), object()]),
    )

    with pytest.raises(RepositoryIntegrityError) as captured:
        await audit_uow_module._read_aggregate(session, "duplicate-request-audit")

    assert captured.value.entity == "AuditScan"
    assert captured.value.reason_code == "ambiguous_client_request"


async def test_duplicate_request_by_client_key_rowset_fails_closed() -> None:
    session = cast(
        AsyncSession,
        _FakeScalarsSession([object(), object()]),
    )
    identity = audit_uow_module._AuditFactoryIdentity(
        client_request_id=REQUEST_ONE,
        request_digest=_digest("request"),
        repository_identity_digest=_digest("repository"),
        authorization_reference=_digest("authorization"),
        authorized_engagement_scope=AuditEngagementScope.profile_a(),
        requested_engagement_id=None,
        workspace_root="/var/lib/riftx/audit/tmp",
        source_repository_path=SOURCE_PATH,
    )

    with pytest.raises(RepositoryIntegrityError) as captured:
        await audit_uow_module._read_client_request(
            session,
            identity,
            for_update=True,
        )

    assert captured.value.entity == "AuditClientRequest"
    assert captured.value.reason_code == "ambiguous_client_request"


async def test_engagement_filter_does_not_hide_redundant_owner_corruption(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path / "audit-list-owner-corruption.db")
    service = _service(database, tmp_path / "audit-workspaces")
    try:
        created = await service.create_draft(_command())
        await _raw_update_without_foreign_keys(
            database,
            "UPDATE audit_scans SET engagement_id=:engagement_id WHERE id=:audit_id",
            {
                "engagement_id": "tampered-engagement",
                "audit_id": created.aggregate.audit.value.id,
            },
        )

        with pytest.raises(RepositoryIntegrityError) as captured:
            await service.list(engagement_id=created.aggregate.engagement.id)

        assert captured.value.reason_code == "owner_binding_mismatch"
    finally:
        await database.dispose()


async def test_list_query_count_is_constant_across_page_sizes(tmp_path: Path) -> None:
    database = await _database(tmp_path / "audit-list-query-count.db")
    service = _service(database, tmp_path / "audit-workspaces")
    try:
        for index in range(1, 9):
            await service.create_draft(_command(str(UUID(int=index, version=4))))

        select_statements: list[str] = []

        def track_selects(*args: object) -> None:
            statement = args[2]
            if isinstance(statement, str) and statement.lstrip().upper().startswith("SELECT"):
                select_statements.append(statement)

        event.listen(
            database.engine.sync_engine,
            "before_cursor_execute",
            track_selects,
        )
        try:
            select_statements.clear()
            one = await service.list(limit=1)
            one_page_selects = len(select_statements)

            select_statements.clear()
            eight = await service.list(limit=8)
            eight_page_selects = len(select_statements)
        finally:
            event.remove(
                database.engine.sync_engine,
                "before_cursor_execute",
                track_selects,
            )

        assert len(one) == 1
        assert len(eight) == 8
        assert one_page_selects == 2
        assert eight_page_selects == 2
    finally:
        await database.dispose()


async def test_authorization_denial_happens_before_sensitive_aggregate_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = await _database(tmp_path / "audit-binding-before-contract.db")
    service = _service(database, tmp_path / "audit-workspaces")
    try:
        created = await service.create_draft(_command())
        repository = SQLAlchemyAuditAggregateReadRepository(database.session_factory)
        aggregate_loader_calls = 0

        async def forbidden_loader(*_args: object, **_kwargs: object) -> Sequence[AuditAggregate]:
            nonlocal aggregate_loader_calls
            aggregate_loader_calls += 1
            raise AssertionError("sensitive aggregate loader must not run before authorization")

        monkeypatch.setattr(audit_uow_module, "_read_aggregates", forbidden_loader)

        def deny(_binding: object) -> None:
            raise ResourceNotAccessibleError(
                "resource_not_accessible",
                "The requested resource was not found",
            )

        with pytest.raises(ResourceNotAccessibleError):
            await repository.get_authorized(
                created.aggregate.audit.value.id,
                authorize=deny,
            )

        assert aggregate_loader_calls == 0
    finally:
        await database.dispose()


async def test_authorized_engagement_scope_is_applied_before_sort_and_pagination(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path / "audit-scope-before-page.db")
    service = _service(database, tmp_path / "audit-workspaces")
    try:
        allowed = await service.create_draft(
            _command(
                REQUEST_ONE,
                repository_seed="allowed-repository",
                authorization_seed="allowed-authorization",
            )
        )
        denied = await service.create_draft(
            _command(
                REQUEST_TWO,
                repository_seed="denied-repository",
                authorization_seed="denied-authorization",
            )
        )
        await _raw_update_without_foreign_keys(
            database,
            "UPDATE audit_scans SET created_at=:created_at WHERE id=:audit_id",
            {
                "created_at": NOW + timedelta(days=1),
                "audit_id": denied.aggregate.audit.value.id,
            },
        )
        repository = SQLAlchemyAuditAggregateReadRepository(database.session_factory)
        scope = AuditEngagementScope(
            all_engagements=False,
            engagement_ids=frozenset({allowed.aggregate.engagement.id}),
            can_create_engagement=False,
        )

        page = await repository.list_authorized(
            authorized_scope=scope,
            limit=1,
        )
        contradictory = await repository.list_authorized(
            authorized_scope=scope,
            engagement_id=denied.aggregate.engagement.id,
            limit=1,
        )

        assert [item.audit.value.id for item in page] == [allowed.aggregate.audit.value.id]
        assert contradictory == ()
    finally:
        await database.dispose()
