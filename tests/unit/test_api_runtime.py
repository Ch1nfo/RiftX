from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from riftx.api.runtime import APISettings, ControlPlane
from riftx.domain import RunStatus


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
        return [SimpleNamespace(id=f"run-{status.value}")]

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
        action_service=placeholder,  # type: ignore[arg-type]
        event_service=placeholder,  # type: ignore[arg-type]
        execution_service=placeholder,  # type: ignore[arg-type]
        finding_service=placeholder,  # type: ignore[arg-type]
        node_service=placeholder,  # type: ignore[arg-type]
        runner_control_service=placeholder,  # type: ignore[arg-type]
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


async def test_control_plane_owner_reconciler_recovers_after_list_failure() -> None:
    runs = RecordingCleanupRunService(fail_first_list=True)
    database = AsyncMock()
    terminal_supervisor = AsyncMock()
    placeholder = object()
    runtime = ControlPlane(
        settings=APISettings(),
        database=database,
        run_service=runs,  # type: ignore[arg-type]
        action_service=placeholder,  # type: ignore[arg-type]
        event_service=placeholder,  # type: ignore[arg-type]
        execution_service=placeholder,  # type: ignore[arg-type]
        finding_service=placeholder,  # type: ignore[arg-type]
        node_service=placeholder,  # type: ignore[arg-type]
        runner_control_service=placeholder,  # type: ignore[arg-type]
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
        action_service=placeholder,  # type: ignore[arg-type]
        event_service=placeholder,  # type: ignore[arg-type]
        execution_service=placeholder,  # type: ignore[arg-type]
        finding_service=placeholder,  # type: ignore[arg-type]
        node_service=placeholder,  # type: ignore[arg-type]
        runner_control_service=placeholder,  # type: ignore[arg-type]
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
    )

    runtime.start_cleanup_reconciler()
    await asyncio.wait_for(runs.all_stops_completed.wait(), timeout=1)
    await runtime.close()

    assert len(runs.stopped_run_ids) == 205
    assert len(set(runs.stopped_run_ids)) == 205
