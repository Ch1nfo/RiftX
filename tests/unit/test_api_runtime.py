from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from riftx.api.runtime import (
    APISettings,
    ControlPlane,
    _create_audit_preflight_availability_check,
    _create_audit_preflight_plan_service,
    _create_audit_preflight_service,
    _create_audit_service,
    _create_local_audit_job_service,
)
from riftx.api.schemas import CreateAuditPreflightRequest
from riftx.application.errors import ServiceUnavailableError
from riftx.application.services import (
    AuditApplicationService,
    AuditPreflightApplicationService,
    AuditPreflightPlanApplicationService,
)
from riftx.config import (
    AuditConfig,
    AuditSourceIngestConfig,
    audit_source_ingest_policy_digest,
)
from riftx.domain import (
    AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,
    LocalPrincipal,
    Node,
    NodeStatus,
    OperatorCapability,
    RunKind,
    RunnerCredential,
    RunnerPrincipal,
    RunStatus,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAuditAggregateReadRepository,
    SQLAlchemyAuditCreationUnitOfWork,
    SQLAlchemyAuditPreflightPlanRepository,
    SQLAlchemyAuditPreflightRepository,
)


async def test_audit_service_is_assembled_for_both_feature_flag_states(
    tmp_path: Path,
) -> None:
    for enabled in (False, True):
        audit = AuditConfig(
            enabled=enabled,
            snapshot_root=tmp_path / f"snapshots-{enabled}",
            temp_root=tmp_path / f"audit-workspaces-{enabled}",
            fix_root=tmp_path / f"fixes-{enabled}",
        )
        settings = APISettings(audit=audit)
        database = Database("sqlite+aiosqlite:///:memory:")
        try:
            service = _create_audit_service(settings, database)

            assert isinstance(service, AuditApplicationService)
            assert isinstance(
                service._creation_uow,
                SQLAlchemyAuditCreationUnitOfWork,
            )
            assert isinstance(
                service._aggregate_repository,
                SQLAlchemyAuditAggregateReadRepository,
            )
            assert service._creation_uow._session_factory is database.session_factory
            assert service._aggregate_repository._session_factory is database.session_factory
            assert service._feature_enabled is enabled
            assert service._workspace_root == audit.temp_root
            assert not audit.temp_root.exists()
        finally:
            await database.dispose()


async def test_local_audit_job_service_is_historical_read_only() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    service = await _create_local_audit_job_service(database)
    try:
        assert service.runnable is False
        assert await service.status("missing-local-audit") is None
    finally:
        await service.close()
        await database.dispose()


async def test_audit_preflight_service_is_always_assembled_and_backend_fail_closed(
    tmp_path: Path,
) -> None:
    image_digest = hashlib.sha256(b"pinned-source-ingest-image").hexdigest()
    principal = LocalPrincipal(
        id="operator-runtime-test",
        capabilities=frozenset(OperatorCapability),
    )

    class Authorizer:
        def preflight_authorization_scope_digest(
            self,
            requested_principal: LocalPrincipal,
            *,
            capability: OperatorCapability,
        ) -> str:
            assert requested_principal is principal
            assert capability is OperatorCapability.HOST_EXECUTE
            return hashlib.sha256(b"runtime-preflight-scope").hexdigest()

    for enabled in (False, True):
        source_root = tmp_path / f"source-{enabled}"
        repository_path = source_root / "repository"
        repository_path.mkdir(parents=True)
        audit = AuditConfig(
            enabled=enabled,
            source_roots=(source_root,),
            snapshot_root=tmp_path / f"snapshots-{enabled}",
            temp_root=tmp_path / f"audit-workspaces-{enabled}",
            fix_root=tmp_path / f"fixes-{enabled}",
            source_ingest=AuditSourceIngestConfig(image_digest=image_digest),
        )
        settings = APISettings(audit=audit)
        database = Database("sqlite+aiosqlite:///:memory:")
        await database.create_schema()
        try:
            service = _create_audit_preflight_service(settings, database)
            request = CreateAuditPreflightRequest.model_validate(
                {
                    "schema_version": "riftx.audit-preflight-request/v1",
                    "client_request_id": "123e4567-e89b-42d3-a456-426614174000",
                    "repository_path": str(repository_path),
                    "source_execution_target": {
                        "node_id": "local",
                        "source_ingest_backend": "linux_container",
                    },
                    "target": {
                        "kind": "working_tree",
                        "revision": "HEAD",
                        "include_untracked": False,
                    },
                    "include_paths": ["src"],
                    "exclude_paths": [],
                    "security_context": {
                        "input_id": None,
                        "repository_paths": [],
                        "discover_defaults": False,
                    },
                    "mode": "standard",
                }
            ).to_domain()

            assert isinstance(service, AuditPreflightApplicationService)
            assert isinstance(service._repository, SQLAlchemyAuditPreflightRepository)
            assert service._repository._session_factory is database.session_factory
            assert service._feature_enabled is enabled
            assert service._source_ingest_available is False
            assert service._source_roots == (source_root.resolve(),)
            assert service._image_digest == image_digest

            with pytest.raises(ServiceUnavailableError) as captured:
                await service.create_authorized(
                    request,
                    principal=principal,
                    authorizer=Authorizer(),  # type: ignore[arg-type]
                )
            assert captured.value.code == (
                "audit_sandbox_unavailable" if enabled else "audit_feature_disabled"
            )
        finally:
            await database.dispose()


async def test_audit_preflight_plan_service_uses_only_the_configured_secret_key(
    tmp_path: Path,
) -> None:
    for encoded_key in (None, "A" * 43):
        audit = AuditConfig(
            enabled=True,
            preflight_token_key_id="rotation-2026-08",
            preflight_token_key=encoded_key,
            snapshot_root=tmp_path / f"snapshots-{encoded_key is not None}",
            temp_root=tmp_path / f"audit-workspaces-{encoded_key is not None}",
            fix_root=tmp_path / f"fixes-{encoded_key is not None}",
        )
        settings = APISettings(audit=audit)
        database = Database("sqlite+aiosqlite:///:memory:")
        try:
            service = _create_audit_preflight_plan_service(settings, database)

            assert isinstance(service, AuditPreflightPlanApplicationService)
            assert isinstance(service._preflight_repository, SQLAlchemyAuditPreflightRepository)
            assert isinstance(service._plan_repository, SQLAlchemyAuditPreflightPlanRepository)
            assert service._preflight_repository._session_factory is database.session_factory
            assert service._plan_repository._session_factory is database.session_factory
            if encoded_key is None:
                assert service._token_codec is None
            else:
                assert service._token_codec is not None
                assert service._token_codec.key_id == "rotation-2026-08"
        finally:
            await database.dispose()


async def test_audit_preflight_availability_requires_exact_live_runner_facts(
    tmp_path: Path,
) -> None:
    image_digest = hashlib.sha256(b"runtime-image").hexdigest()
    source_root = tmp_path / "source"
    source_root.mkdir()
    audit = AuditConfig(
        enabled=True,
        source_roots=(source_root,),
        snapshot_root=tmp_path / "snapshots",
        temp_root=tmp_path / "audit-tmp",
        fix_root=tmp_path / "fixes",
        source_ingest=AuditSourceIngestConfig(image_digest=image_digest),
    )
    settings = APISettings(audit=audit)
    principal = RunnerPrincipal(instance_id="runtime-preflight-runner", epoch=4)
    now = datetime.now(UTC)
    labels = {
        "audit_source_ingest_available": "true",
        "audit_source_ingest_backend_id": audit.source_ingest.backend_id,
        "audit_source_ingest_image_digest": image_digest,
        "audit_source_ingest_policy_digest": audit_source_ingest_policy_digest(
            audit.source_ingest
        ),
    }
    node = Node(
        id="local",
        name="local",
        platform="linux",
        architecture="x86_64",
        status=NodeStatus.ONLINE,
        capabilities=[AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY],
        labels=labels,
        current_owner=principal,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    credential = RunnerCredential(
        node_id="local",
        principal=principal,
        token_hash="a" * 64,
        token_prefix="runtime",
        protocol_capabilities=(AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,),
        created_at=now,
        rotated_at=now,
    )
    node_service = SimpleNamespace(get=AsyncMock(return_value=node))
    credentials = SimpleNamespace(get_current=AsyncMock(return_value=credential))
    available = _create_audit_preflight_availability_check(
        settings,
        node_service=node_service,  # type: ignore[arg-type]
        credentials=credentials,  # type: ignore[arg-type]
    )

    assert await available() is True

    node_service.get.return_value = node.model_copy(update={"platform": "darwin"})
    assert await available() is False
    node_service.get.return_value = node.model_copy(
        update={"labels": {**labels, "audit_source_ingest_policy_digest": "b" * 64}}
    )
    assert await available() is False
    node_service.get.return_value = node.model_copy(update={"capabilities": []})
    assert await available() is False
    node_service.get.return_value = node
    credentials.get_current.return_value = credential.model_copy(
        update={"protocol_capabilities": ()}
    )
    assert await available() is False
    credentials.get_current.side_effect = RuntimeError("database unavailable")
    assert await available() is False


class RecordingCleanupRunService:
    def __init__(self, *, fail_first_list: bool = False) -> None:
        self.fail_first_list = fail_first_list
        self.list_attempts = 0
        self.returned_statuses: set[RunStatus] = set()
        self.stopped_run_ids: list[str] = []
        self.first_list_failed = asyncio.Event()
        self.all_fences_seen = asyncio.Event()
        self.all_stops_completed = asyncio.Event()

    async def list_runs_for_reconciliation(
        self,
        *,
        status: RunStatus,
        created_through: datetime,
        after_created_at: datetime | None,
        after_id: str | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        del created_through, after_created_at, after_id, limit
        self.list_attempts += 1
        if self.fail_first_list and self.list_attempts == 1:
            self.first_list_failed.set()
            raise RuntimeError("transient cleanup scan failure")
        if status in self.returned_statuses:
            return []
        self.returned_statuses.add(status)
        if self.returned_statuses == {
            RunStatus.PAUSING,
            RunStatus.CANCELLING,
            RunStatus.COMPLETING,
        }:
            self.all_fences_seen.set()
        return [SimpleNamespace(id=f"run-{status.value}", kind=RunKind.GENERAL)]

    async def stop_resources_for_cleanup(self, run_id: str) -> SimpleNamespace:
        self.stopped_run_ids.append(run_id)
        if len(self.stopped_run_ids) == 3:
            self.all_stops_completed.set()
        return SimpleNamespace(succeeded=True, failed_resource_types=())


class PagedCleanupRunService:
    def __init__(self, count: int) -> None:
        created = datetime(2025, 1, 1, tzinfo=UTC)
        self.remaining = {
            f"run-{index:03d}": SimpleNamespace(
                id=f"run-{index:03d}",
                kind=RunKind.GENERAL,
                created_at=created + timedelta(microseconds=index),
            )
            for index in range(count)
        }
        self.stopped_run_ids: list[str] = []
        self.all_stops_completed = asyncio.Event()

    async def list_runs_for_reconciliation(
        self,
        *,
        status: RunStatus,
        created_through: datetime,
        after_created_at: datetime | None,
        after_id: str | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        if status is not RunStatus.PAUSING:
            return []
        cursor = (
            (after_created_at, after_id)
            if after_created_at is not None and after_id is not None
            else None
        )
        candidates = sorted(
            (
                run
                for run in self.remaining.values()
                if run.created_at <= created_through
                and (cursor is None or (run.created_at, run.id) > cursor)
            ),
            key=lambda run: (run.created_at, run.id),
        )
        return candidates[:limit]

    async def stop_resources_for_cleanup(self, run_id: str) -> SimpleNamespace:
        self.stopped_run_ids.append(run_id)
        self.remaining.pop(run_id)
        if not self.remaining:
            self.all_stops_completed.set()
        return SimpleNamespace(succeeded=True, failed_resource_types=())


async def test_control_plane_owner_reconciler_covers_every_fence_and_stops_cleanly() -> None:
    runs = RecordingCleanupRunService()
    database = AsyncMock()
    terminal_supervisor = AsyncMock()
    placeholder = object()
    runtime = ControlPlane(
        settings=APISettings(),
        database=database,
        run_service=runs,  # type: ignore[arg-type]
        audit_service=placeholder,  # type: ignore[arg-type]
        action_service=placeholder,  # type: ignore[arg-type]
        event_service=placeholder,  # type: ignore[arg-type]
        execution_service=placeholder,  # type: ignore[arg-type]
        finding_service=placeholder,  # type: ignore[arg-type]
        node_service=placeholder,  # type: ignore[arg-type]
        runner_control_service=AsyncMock(),
        report_service=placeholder,  # type: ignore[arg-type]
        tool_service=placeholder,  # type: ignore[arg-type]
        model_profile_service=placeholder,  # type: ignore[arg-type]
        approval_service=placeholder,  # type: ignore[arg-type]
        artifact_service=placeholder,  # type: ignore[arg-type]
        context_service=placeholder,  # type: ignore[arg-type]
        memory_service=placeholder,  # type: ignore[arg-type]
        runtime_observability_service=placeholder,  # type: ignore[arg-type]
        terminal_service=placeholder,  # type: ignore[arg-type]
        terminal_supervisor=terminal_supervisor,
        graph_repository=placeholder,  # type: ignore[arg-type]
        traffic_repository=placeholder,  # type: ignore[arg-type]
    )

    runtime.start_cleanup_reconciler()
    task = runtime._cleanup_reconciler_task
    runtime.start_cleanup_reconciler()
    assert runtime._cleanup_reconciler_task is task
    await asyncio.wait_for(runs.all_fences_seen.wait(), timeout=1)
    await asyncio.wait_for(runs.all_stops_completed.wait(), timeout=1)

    await runtime.close()

    assert sorted(runs.stopped_run_ids) == [
        "run-cancelling",
        "run-completing",
        "run-pausing",
    ]
    assert task is not None and task.done()
    terminal_supervisor.close_all.assert_awaited_once_with()
    database.dispose.assert_awaited_once_with()


async def test_control_plane_runner_reconciler_retries_and_runs_both_paths() -> None:
    runner_control = AsyncMock()
    stop_attempts = 0
    reconciled = asyncio.Event()

    async def reconcile_stop_receipts() -> int:
        nonlocal stop_attempts
        stop_attempts += 1
        if stop_attempts == 1:
            raise RuntimeError("transient stop receipt failure")
        return 1

    async def reconcile_quarantined_commands() -> int:
        reconciled.set()
        return 1

    runner_control.reconcile_stop_receipts.side_effect = reconcile_stop_receipts
    runner_control.reconcile_quarantined_commands.side_effect = (
        reconcile_quarantined_commands
    )
    placeholder = object()
    runtime = ControlPlane(
        settings=APISettings(),
        database=AsyncMock(),
        run_service=placeholder,  # type: ignore[arg-type]
        audit_service=placeholder,  # type: ignore[arg-type]
        action_service=placeholder,  # type: ignore[arg-type]
        event_service=placeholder,  # type: ignore[arg-type]
        execution_service=placeholder,  # type: ignore[arg-type]
        finding_service=placeholder,  # type: ignore[arg-type]
        node_service=placeholder,  # type: ignore[arg-type]
        runner_control_service=runner_control,
        report_service=placeholder,  # type: ignore[arg-type]
        tool_service=placeholder,  # type: ignore[arg-type]
        model_profile_service=placeholder,  # type: ignore[arg-type]
        approval_service=placeholder,  # type: ignore[arg-type]
        artifact_service=placeholder,  # type: ignore[arg-type]
        context_service=placeholder,  # type: ignore[arg-type]
        memory_service=placeholder,  # type: ignore[arg-type]
        runtime_observability_service=placeholder,  # type: ignore[arg-type]
        terminal_service=placeholder,  # type: ignore[arg-type]
        terminal_supervisor=AsyncMock(),
        graph_repository=placeholder,  # type: ignore[arg-type]
        traffic_repository=placeholder,  # type: ignore[arg-type]
    )

    task = asyncio.create_task(runtime._reconcile_runner_state())
    try:
        await asyncio.wait_for(reconciled.wait(), timeout=1)
        assert not task.done()
        assert stop_attempts >= 2
        assert runner_control.reconcile_quarantined_commands.await_count >= 1
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_control_plane_audit_preflight_reconciler_retries_without_feature_gate() -> None:
    preflight_runner = AsyncMock()
    attempts = 0
    reconciled = asyncio.Event()

    async def reconcile_batch() -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient preflight reconciliation failure")
        reconciled.set()
        return 1

    preflight_runner.reconcile_batch.side_effect = reconcile_batch
    placeholder = object()
    runtime = ControlPlane(
        settings=APISettings(audit=AuditConfig(enabled=False)),
        database=AsyncMock(),
        run_service=placeholder,  # type: ignore[arg-type]
        audit_service=placeholder,  # type: ignore[arg-type]
        action_service=placeholder,  # type: ignore[arg-type]
        event_service=placeholder,  # type: ignore[arg-type]
        execution_service=placeholder,  # type: ignore[arg-type]
        finding_service=placeholder,  # type: ignore[arg-type]
        node_service=placeholder,  # type: ignore[arg-type]
        runner_control_service=AsyncMock(),
        report_service=placeholder,  # type: ignore[arg-type]
        tool_service=placeholder,  # type: ignore[arg-type]
        model_profile_service=placeholder,  # type: ignore[arg-type]
        approval_service=placeholder,  # type: ignore[arg-type]
        artifact_service=placeholder,  # type: ignore[arg-type]
        context_service=placeholder,  # type: ignore[arg-type]
        memory_service=placeholder,  # type: ignore[arg-type]
        runtime_observability_service=placeholder,  # type: ignore[arg-type]
        terminal_service=placeholder,  # type: ignore[arg-type]
        terminal_supervisor=AsyncMock(),
        graph_repository=placeholder,  # type: ignore[arg-type]
        traffic_repository=placeholder,  # type: ignore[arg-type]
        audit_preflight_runner_service=preflight_runner,
    )

    task = asyncio.create_task(runtime._reconcile_audit_preflight_jobs())
    try:
        await asyncio.wait_for(reconciled.wait(), timeout=1)
        assert attempts >= 2
        assert not task.done()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_control_plane_owner_reconciler_recovers_after_list_failure() -> None:
    runs = RecordingCleanupRunService(fail_first_list=True)
    database = AsyncMock()
    terminal_supervisor = AsyncMock()
    placeholder = object()
    runtime = ControlPlane(
        settings=APISettings(),
        database=database,
        run_service=runs,  # type: ignore[arg-type]
        audit_service=placeholder,  # type: ignore[arg-type]
        action_service=placeholder,  # type: ignore[arg-type]
        event_service=placeholder,  # type: ignore[arg-type]
        execution_service=placeholder,  # type: ignore[arg-type]
        finding_service=placeholder,  # type: ignore[arg-type]
        node_service=placeholder,  # type: ignore[arg-type]
        runner_control_service=AsyncMock(),
        report_service=placeholder,  # type: ignore[arg-type]
        tool_service=placeholder,  # type: ignore[arg-type]
        model_profile_service=placeholder,  # type: ignore[arg-type]
        approval_service=placeholder,  # type: ignore[arg-type]
        artifact_service=placeholder,  # type: ignore[arg-type]
        context_service=placeholder,  # type: ignore[arg-type]
        memory_service=placeholder,  # type: ignore[arg-type]
        runtime_observability_service=placeholder,  # type: ignore[arg-type]
        terminal_service=placeholder,  # type: ignore[arg-type]
        terminal_supervisor=terminal_supervisor,
        graph_repository=placeholder,  # type: ignore[arg-type]
        traffic_repository=placeholder,  # type: ignore[arg-type]
    )

    runtime.start_cleanup_reconciler()
    task = runtime._cleanup_reconciler_task
    await asyncio.wait_for(runs.first_list_failed.wait(), timeout=1)
    await asyncio.wait_for(runs.all_stops_completed.wait(), timeout=1)

    assert task is not None and not task.done()
    assert runs.list_attempts >= 4
    assert sorted(runs.stopped_run_ids) == [
        "run-cancelling",
        "run-completing",
        "run-pausing",
    ]

    await runtime.close()


async def test_control_plane_owner_reconciler_keyset_scan_does_not_skip_mutated_pages() -> None:
    runs = PagedCleanupRunService(205)
    database = AsyncMock()
    terminal_supervisor = AsyncMock()
    placeholder = object()
    runtime = ControlPlane(
        settings=APISettings(),
        database=database,
        run_service=runs,  # type: ignore[arg-type]
        audit_service=placeholder,  # type: ignore[arg-type]
        action_service=placeholder,  # type: ignore[arg-type]
        event_service=placeholder,  # type: ignore[arg-type]
        execution_service=placeholder,  # type: ignore[arg-type]
        finding_service=placeholder,  # type: ignore[arg-type]
        node_service=placeholder,  # type: ignore[arg-type]
        runner_control_service=AsyncMock(),
        report_service=placeholder,  # type: ignore[arg-type]
        tool_service=placeholder,  # type: ignore[arg-type]
        model_profile_service=placeholder,  # type: ignore[arg-type]
        approval_service=placeholder,  # type: ignore[arg-type]
        artifact_service=placeholder,  # type: ignore[arg-type]
        context_service=placeholder,  # type: ignore[arg-type]
        memory_service=placeholder,  # type: ignore[arg-type]
        runtime_observability_service=placeholder,  # type: ignore[arg-type]
        terminal_service=placeholder,  # type: ignore[arg-type]
        terminal_supervisor=terminal_supervisor,
        graph_repository=placeholder,  # type: ignore[arg-type]
        traffic_repository=placeholder,  # type: ignore[arg-type]
    )

    runtime.start_cleanup_reconciler()
    await asyncio.wait_for(runs.all_stops_completed.wait(), timeout=1)
    await runtime.close()

    assert len(runs.stopped_run_ids) == 205
    assert len(set(runs.stopped_run_ids)) == 205


async def test_control_plane_routes_code_audit_cleanup_to_dedicated_reconciler() -> None:
    delivered = False
    reconciled = asyncio.Event()
    run_service = AsyncMock()

    async def list_runs_for_reconciliation(**filters: object) -> list[SimpleNamespace]:
        nonlocal delivered
        if filters["status"] is RunStatus.PAUSING and not delivered:
            delivered = True
            return [
                SimpleNamespace(
                    id="audit-run-1",
                    kind=RunKind.CODE_AUDIT,
                    created_at=datetime.now(UTC),
                )
            ]
        return []

    run_service.list_runs_for_reconciliation.side_effect = list_runs_for_reconciliation
    audit_controls = AsyncMock()

    async def reconcile_run(run_id: str) -> SimpleNamespace:
        assert run_id == "audit-run-1"
        reconciled.set()
        return SimpleNamespace(succeeded=True, failed_resource_types=())

    audit_controls.reconcile_run.side_effect = reconcile_run
    terminal_supervisor = AsyncMock()
    placeholder = object()
    runtime = ControlPlane(
        settings=APISettings(),
        database=AsyncMock(),
        run_service=run_service,
        audit_service=placeholder,  # type: ignore[arg-type]
        action_service=placeholder,  # type: ignore[arg-type]
        event_service=placeholder,  # type: ignore[arg-type]
        execution_service=placeholder,  # type: ignore[arg-type]
        finding_service=placeholder,  # type: ignore[arg-type]
        node_service=placeholder,  # type: ignore[arg-type]
        runner_control_service=AsyncMock(),
        report_service=placeholder,  # type: ignore[arg-type]
        tool_service=placeholder,  # type: ignore[arg-type]
        model_profile_service=placeholder,  # type: ignore[arg-type]
        approval_service=placeholder,  # type: ignore[arg-type]
        artifact_service=placeholder,  # type: ignore[arg-type]
        context_service=placeholder,  # type: ignore[arg-type]
        memory_service=placeholder,  # type: ignore[arg-type]
        runtime_observability_service=placeholder,  # type: ignore[arg-type]
        terminal_service=placeholder,  # type: ignore[arg-type]
        terminal_supervisor=terminal_supervisor,
        graph_repository=placeholder,  # type: ignore[arg-type]
        traffic_repository=placeholder,  # type: ignore[arg-type]
        audit_control_service=audit_controls,
    )

    runtime.start_cleanup_reconciler()
    await asyncio.wait_for(reconciled.wait(), timeout=1)
    await runtime.close()

    run_service.stop_resources_for_cleanup.assert_not_awaited()
    audit_controls.reconcile_run.assert_awaited_once_with("audit-run-1")
