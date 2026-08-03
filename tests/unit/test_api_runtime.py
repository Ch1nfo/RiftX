from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from riftx.api.runtime import APISettings, ControlPlane, _create_audit_service
from riftx.application.services import AuditApplicationService
from riftx.config import AuditConfig
from riftx.domain import RunKind, RunStatus
from riftx.persistence import (
    Database,
    SQLAlchemyAuditAggregateReadRepository,
    SQLAlchemyAuditCreationUnitOfWork,
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
