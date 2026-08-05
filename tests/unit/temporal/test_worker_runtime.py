from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel
from pydantic import SecretStr
from temporalio.client import TLSConfig

from riftx.application.services import ResourceStopDisposition, SafetyStopResult
from riftx.config import (
    AgentConfig,
    DatabaseConfig,
    ModelsRuntimeConfig,
    RiftXConfig,
    RunnerConfig,
    TemporalConfig,
    ToolsConfig,
    WorkspaceConfig,
)
from riftx.domain import Engagement, Objective, Run, RunKind, RunStatus
from riftx.models import ModelAPI, ModelProfile, ModelProfileRegistry
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyNodeRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTranscriptRepository,
)
from riftx.runtime.types import AgentSession
from riftx.temporal import worker_runtime
from riftx.temporal.runtime import TemporalRuntimeConfig
from riftx.temporal.worker_runtime import TemporalWorkerRuntime, _RunEventUserInputResolver


@dataclass
class FakeWorker:
    run_calls: int = 0

    async def run(self) -> None:
        self.run_calls += 1


class BlockingWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        await self.release.wait()


class RecordingNodeService:
    def __init__(self) -> None:
        self.heartbeat_calls = 0
        self.first_heartbeat = asyncio.Event()
        self.disconnected_node_id: str | None = None

    async def heartbeat(self, node_id: str, heartbeat: object) -> None:
        self.heartbeat_calls += 1
        self.first_heartbeat.set()

    async def disconnect(self, node_id: str) -> None:
        self.disconnected_node_id = node_id


def write_yaml(path: Path, payload: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def runtime_config(tmp_path: Path) -> RiftXConfig:
    tools_path = tmp_path / "tools.yaml"
    models_path = tmp_path / "models.yaml"
    write_yaml(tools_path, {"version": 1, "tools": {}})
    write_yaml(
        models_path,
        {
            "default_profile": "test",
            "models": {
                "test": {
                    "model": "test-model",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "requires_api_key": False,
                    "api_key_env": None,
                }
            },
        },
    )
    return RiftXConfig(
        database=DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}"),
        temporal=TemporalConfig(
            target="temporal.test:7233",
            namespace="test-namespace",
            task_queue="test-queue",
            workflow_id_prefix="test-run",
            max_concurrent_activities=7,
            max_cached_workflows=11,
        ),
        runner=RunnerConfig(node_id="worker-local", state_path=tmp_path / "runner"),
        workspace=WorkspaceConfig(root=tmp_path / "workspaces"),
        tools=ToolsConfig(path=tools_path),
        models=ModelsRuntimeConfig(
            path=models_path,
            secrets_path=tmp_path / "secrets" / "models.json",
        ),
        agent=AgentConfig(max_history_items=12, max_turns=3),
    )


@pytest.mark.asyncio
async def test_build_temporal_worker_assembles_runtime_and_closes_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    fake_worker = FakeWorker()

    def fake_create_worker(
        client: object,
        activities: object,
        config: object,
        *,
        runtime_cycle_activities: object | None = None,
    ) -> FakeWorker:
        captured.update(
            client=client,
            activities=activities,
            config=config,
            runtime_cycle_activities=runtime_cycle_activities,
        )
        return fake_worker

    real_web_artifact_store = worker_runtime.ApplicationWebArtifactStore

    def capture_web_artifact_store(
        service: object,
        *,
        runs: object,
        audits: object,
    ) -> object:
        captured["web_artifact_runs"] = runs
        captured["web_artifact_audits"] = audits
        return real_web_artifact_store(  # type: ignore[arg-type]
            service,
            runs=runs,
            audits=audits,
        )

    monkeypatch.setattr(worker_runtime, "create_worker", fake_create_worker)
    monkeypatch.setattr(
        worker_runtime,
        "ApplicationWebArtifactStore",
        capture_web_artifact_store,
    )
    temporal_client = object()
    config = runtime_config(tmp_path)

    runtime = await worker_runtime.build_temporal_worker(
        config,
        temporal_client=temporal_client,  # type: ignore[arg-type]
    )

    assert runtime.worker is fake_worker
    assert captured["client"] is temporal_client
    assert captured["config"] == TemporalRuntimeConfig(
        task_queue="test-queue",
        workflow_id_prefix="test-run",
        max_concurrent_activities=7,
        max_cached_workflows=11,
    )
    assert len(captured["activities"].registered()) > 0
    assert captured["runtime_cycle_activities"] is not None
    assert len(captured["runtime_cycle_activities"].registered()) == 1
    assert runtime.run_repository is not None
    assert captured["web_artifact_runs"] is runtime.run_repository
    assert captured["web_artifact_audits"] is not None
    assert runtime.mcp_registry is not None
    assert runtime.mcp_registry.snapshot.servers == []
    assert runtime.mcp_registry.snapshot.tools == []
    assert runtime.safety_stopper is not None
    assert runtime.runner_control_service is not None
    process_executor = runtime.process_supervisor._process_executor
    assert process_executor._require_containment is True
    assert runtime.terminal_supervisor._require_containment is True
    assert runtime.terminal_supervisor._containment_manager is process_executor.containment_manager
    assert runtime.safety_stopper._execution_runner._local_terminal is runtime.terminal_supervisor
    assert set(runtime.safety_stopper._resource_stoppers) == {
        "browser_sessions",
        "target_http_requests",
    }
    assert (tmp_path / "workspaces").is_dir()
    assert (tmp_path / "runner").is_dir()
    node = await SQLAlchemyNodeRepository(runtime.database.session_factory).get("worker-local")
    assert node is not None
    assert node.labels["mode"] == "worker-local"
    assert node.labels["tool_count"] == "0"
    assert node.labels["mcp_server_count"] == "0"
    assert node.labels["mcp_tool_count"] == "0"
    assert node.labels["working_directory"]
    assert node.labels["shell"]

    await runtime.run()
    await runtime.close()

    assert fake_worker.run_calls == 1
    assert runtime._closed is True


@pytest.mark.asyncio
async def test_build_temporal_worker_connects_with_configured_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_ca = tmp_path / "temporal-root-ca.pem"
    client_cert = tmp_path / "temporal-client-cert.pem"
    client_key = tmp_path / "temporal-client-key.pem"
    root_ca.write_bytes(b"worker-root-ca")
    client_cert.write_bytes(b"worker-client-cert")
    client_key.write_bytes(b"worker-client-key")
    calls: list[tuple[str, str, object, str | None]] = []
    client = object()

    async def fake_connect(
        target: str,
        *,
        namespace: str,
        tls: object,
        api_key: str | None,
    ) -> object:
        calls.append((target, namespace, tls, api_key))
        return client

    monkeypatch.setattr(worker_runtime.Client, "connect", fake_connect)
    monkeypatch.setattr(worker_runtime, "create_worker", lambda *_, **__: FakeWorker())
    config = runtime_config(tmp_path)
    config.temporal = TemporalConfig(
        target="temporal.test:7233",
        namespace="test-namespace",
        task_queue="test-queue",
        workflow_id_prefix="test-run",
        max_concurrent_activities=7,
        max_cached_workflows=11,
        tls_enabled=True,
        tls_server_root_ca_path=root_ca,
        tls_server_name="temporal.worker.test",
        tls_client_cert_path=client_cert,
        tls_client_private_key_path=client_key,
        api_key=SecretStr("worker-temporal-secret"),
    )

    runtime = await worker_runtime.build_temporal_worker(config)
    try:
        assert len(calls) == 1
        target, namespace, tls, api_key = calls[0]
        assert target == "temporal.test:7233"
        assert namespace == "test-namespace"
        assert api_key == "worker-temporal-secret"
        assert isinstance(tls, TLSConfig)
        assert tls.server_root_ca_cert == b"worker-root-ca"
        assert tls.domain == "temporal.worker.test"
        assert tls.client_cert == b"worker-client-cert"
        assert tls.client_private_key == b"worker-client-key"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_worker_model_provider_hot_reloads_profile_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_runtime, "create_worker", lambda *_, **__: FakeWorker())
    config = runtime_config(tmp_path)
    runtime = await worker_runtime.build_temporal_worker(
        config,
        temporal_client=object(),  # type: ignore[arg-type]
    )
    try:
        first = runtime.model_provider.get_model("test")
        api_registry = ModelProfileRegistry(
            config.models.path,
            config.models.secrets_path,
        )
        api_registry.refresh()
        api_registry.upsert(
            "test",
            ModelProfile(
                model="reloaded-model",
                api=ModelAPI.RESPONSES,
                base_url="http://127.0.0.1:8001/v1",
                requires_api_key=False,
                api_key_env=None,
            ),
        )

        reloaded = runtime.model_provider.get_model("test")

        assert isinstance(first, OpenAIChatCompletionsModel)
        assert isinstance(reloaded, OpenAIResponsesModel)
        assert reloaded is not first
        assert runtime.model_provider.config.models["test"].model == "reloaded-model"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_worker_session_initializer_reloads_effective_default_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_create_worker(
        *_: object,
        runtime_cycle_activities: object | None = None,
        **__: object,
    ) -> FakeWorker:
        captured["runtime_cycle_activities"] = runtime_cycle_activities
        return FakeWorker()

    monkeypatch.setattr(worker_runtime, "create_worker", fake_create_worker)
    config = runtime_config(tmp_path)
    runtime = await worker_runtime.build_temporal_worker(
        config,
        temporal_client=object(),  # type: ignore[arg-type]
    )
    try:
        await SQLAlchemyEngagementRepository(runtime.database.session_factory).create(
            Engagement(id="engagement-default", name="Default profile reload")
        )
        runs = SQLAlchemyRunRepository(runtime.database.session_factory)
        await runs.create(
            Run(
                kind="general",
                id="run-default",
                engagement_id="engagement-default",
                node_id="worker-local",
                objective=Objective(description="Use the latest default"),
                workspace_path=str(tmp_path / "workspaces" / "run-default"),
            )
        )
        api_registry = ModelProfileRegistry(
            config.models.path,
            config.models.secrets_path,
        )
        api_registry.refresh()
        api_registry.upsert(
            "fast",
            ModelProfile(
                model="fast-model",
                base_url="http://127.0.0.1:8002/v1",
                requires_api_key=False,
                api_key_env=None,
            ),
        )
        api_registry.set_default("fast")

        runtime_activities = captured["runtime_cycle_activities"]
        initializer = runtime_activities._session_initializer
        await initializer.ensure_primary_session(
            "run-default",
            "session-default",
        )

        session = await SQLAlchemyAgentSessionRepository(runtime.database.session_factory).get(
            "session-default"
        )
        assert session is not None
        assert session.model_profile == "fast"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_temporal_worker_keeps_local_node_online_until_shutdown() -> None:
    worker = BlockingWorker()
    nodes = RecordingNodeService()
    database = AsyncMock()
    process_supervisor = AsyncMock()
    terminal_supervisor = AsyncMock()
    model_provider = AsyncMock()
    run_repository = AsyncMock()
    run_repository.list_for_reconciliation.return_value = []
    safety_stopper = AsyncMock()
    runtime = TemporalWorkerRuntime(
        worker=worker,  # type: ignore[arg-type]
        database=database,
        process_supervisor=process_supervisor,
        terminal_supervisor=terminal_supervisor,
        model_provider=model_provider,
        node_service=nodes,  # type: ignore[arg-type]
        node_id="worker-local",
        heartbeat_interval_seconds=0.01,
        run_repository=run_repository,
        safety_stopper=safety_stopper,
    )

    run_task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    await asyncio.wait_for(nodes.first_heartbeat.wait(), timeout=1)
    safety_task = runtime._safety_reconciler_task
    assert safety_task is not None and not safety_task.done()
    await asyncio.sleep(0.03)
    assert nodes.heartbeat_calls >= 2

    worker.release.set()
    await asyncio.wait_for(run_task, timeout=1)
    heartbeat_calls_after_close = nodes.heartbeat_calls
    await asyncio.sleep(0.03)

    assert nodes.heartbeat_calls == heartbeat_calls_after_close
    assert nodes.disconnected_node_id == "worker-local"
    assert safety_task.done()
    terminal_supervisor.close_all.assert_awaited_once_with()
    process_supervisor.close.assert_awaited_once_with(cancel_running=True)
    model_provider.aclose.assert_awaited_once_with()
    database.dispose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_worker_safety_reconciler_recovers_after_list_failure() -> None:
    list_attempts = 0
    returned_run = False
    stop_completed = asyncio.Event()

    async def list_runs(
        *,
        status: object,
        created_through: object,
        after_created_at: object,
        after_id: object,
        limit: int,
    ) -> list[SimpleNamespace]:
        del created_through, after_created_at, after_id, limit
        nonlocal list_attempts, returned_run
        list_attempts += 1
        if list_attempts == 1:
            raise RuntimeError("transient safety scan failure")
        if status is RunStatus.PAUSING and not returned_run:
            returned_run = True
            return [SimpleNamespace(id="run-retry", kind=RunKind.GENERAL)]
        return []

    async def stop_run(run_id: str, *, drain: bool) -> SimpleNamespace:
        assert run_id == "run-retry"
        assert drain is True
        stop_completed.set()
        return SimpleNamespace(succeeded=True, failed_resource_types=())

    run_repository = AsyncMock()
    run_repository.list_for_reconciliation.side_effect = list_runs
    safety_stopper = AsyncMock()
    safety_stopper.stop_run.side_effect = stop_run
    runtime = TemporalWorkerRuntime(
        worker=AsyncMock(),
        database=AsyncMock(),
        process_supervisor=AsyncMock(),
        terminal_supervisor=AsyncMock(),
        model_provider=AsyncMock(),
        node_service=AsyncMock(),
        node_id="worker-local",
        heartbeat_interval_seconds=0.01,
        run_repository=run_repository,
        safety_stopper=safety_stopper,
    )

    task = asyncio.create_task(runtime._safety_reconciler_loop())
    try:
        await asyncio.wait_for(stop_completed.wait(), timeout=1)

        assert not task.done()
        assert list_attempts >= 2
        safety_stopper.stop_run.assert_awaited_once_with("run-retry", drain=True)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_worker_runner_reconciler_retries_and_runs_both_paths() -> None:
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
    runtime = TemporalWorkerRuntime(
        worker=AsyncMock(),
        database=AsyncMock(),
        process_supervisor=AsyncMock(),
        terminal_supervisor=AsyncMock(),
        model_provider=AsyncMock(),
        node_service=AsyncMock(),
        node_id="worker-local",
        heartbeat_interval_seconds=0.01,
        runner_control_service=runner_control,
    )

    task = asyncio.create_task(runtime._runner_reconciliation_loop())
    try:
        await asyncio.wait_for(reconciled.wait(), timeout=1)
        assert not task.done()
        assert stop_attempts >= 2
        assert runner_control.reconcile_quarantined_commands.await_count >= 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_worker_safety_reconciler_keyset_scan_does_not_skip_mutated_pages() -> None:
    created = datetime(2025, 1, 1, tzinfo=UTC)
    remaining = {
        f"run-{index:03d}": SimpleNamespace(
            id=f"run-{index:03d}",
            kind=RunKind.GENERAL,
            created_at=created + timedelta(microseconds=index),
        )
        for index in range(205)
    }
    stopped_run_ids: list[str] = []
    all_stops_completed = asyncio.Event()

    async def list_runs(
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
                for run in remaining.values()
                if run.created_at <= created_through
                and (cursor is None or (run.created_at, run.id) > cursor)
            ),
            key=lambda run: (run.created_at, run.id),
        )
        return candidates[:limit]

    async def stop_run(run_id: str, *, drain: bool) -> SimpleNamespace:
        assert drain is True
        stopped_run_ids.append(run_id)
        remaining.pop(run_id)
        if not remaining:
            all_stops_completed.set()
        return SimpleNamespace(succeeded=True, failed_resource_types=())

    run_repository = AsyncMock()
    run_repository.list_for_reconciliation.side_effect = list_runs
    safety_stopper = AsyncMock()
    safety_stopper.stop_run.side_effect = stop_run
    runtime = TemporalWorkerRuntime(
        worker=AsyncMock(),
        database=AsyncMock(),
        process_supervisor=AsyncMock(),
        terminal_supervisor=AsyncMock(),
        model_provider=AsyncMock(),
        node_service=AsyncMock(),
        node_id="worker-local",
        heartbeat_interval_seconds=0.01,
        run_repository=run_repository,
        safety_stopper=safety_stopper,
    )

    task = asyncio.create_task(runtime._safety_reconciler_loop())
    try:
        await asyncio.wait_for(all_stops_completed.wait(), timeout=1)
        assert len(stopped_run_ids) == 205
        assert len(set(stopped_run_ids)) == 205
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_worker_routes_code_audit_cleanup_to_dedicated_reconciler() -> None:
    delivered = False
    reconciled = asyncio.Event()

    async def list_runs(**filters: object) -> list[SimpleNamespace]:
        nonlocal delivered
        if filters["status"] is RunStatus.CANCELLING and not delivered:
            delivered = True
            return [
                SimpleNamespace(
                    id="audit-run-1",
                    kind=RunKind.CODE_AUDIT,
                    created_at=datetime.now(UTC),
                )
            ]
        return []

    run_repository = AsyncMock()
    run_repository.list_for_reconciliation.side_effect = list_runs
    safety_stopper = AsyncMock()
    audit_reconciler = AsyncMock()

    async def reconcile_run(run_id: str) -> SimpleNamespace:
        assert run_id == "audit-run-1"
        reconciled.set()
        return SimpleNamespace(succeeded=True, failed_resource_types=())

    audit_reconciler.reconcile_run.side_effect = reconcile_run
    runtime = TemporalWorkerRuntime(
        worker=AsyncMock(),
        database=AsyncMock(),
        process_supervisor=AsyncMock(),
        terminal_supervisor=AsyncMock(),
        model_provider=AsyncMock(),
        node_service=AsyncMock(),
        node_id="worker-local",
        heartbeat_interval_seconds=0.01,
        run_repository=run_repository,
        safety_stopper=safety_stopper,
        audit_cleanup_reconciler=audit_reconciler,
    )

    task = asyncio.create_task(runtime._safety_reconciler_loop())
    try:
        await asyncio.wait_for(reconciled.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    safety_stopper.stop_run.assert_not_awaited()
    audit_reconciler.reconcile_run.assert_awaited_once_with("audit-run-1")


@pytest.mark.parametrize(
    ("target", "defer_cleanup_event"),
    [
        (RunStatus.FAILED, False),
        (RunStatus.COMPLETED, True),
    ],
)
@pytest.mark.asyncio
async def test_worker_reconciler_terminalizes_durable_finalization_intent(
    tmp_path: Path,
    target: RunStatus,
    defer_cleanup_event: bool,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'worker-finalization.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Worker finalization")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await runs.create(
        Run(
            kind="general",
            id="run-finalization",
            engagement_id="engagement-1",
            node_id="worker-local",
            objective=Objective(description="Reconcile the finalization intent"),
            workspace_path=str(tmp_path / "workspaces" / "run-finalization"),
        )
    )
    await runs.update_status("run-finalization", RunStatus.PREPARING)
    await runs.update_status("run-finalization", RunStatus.RUNNING)
    await runs.fence_finalization(
        "run-finalization",
        target,
        defer_cleanup_event=defer_cleanup_event,
    )
    empty = ResourceStopDisposition((), {}, {}, {}, {})
    stop_result = SafetyStopResult(
        resources={
            "executions": empty,
            "browser_sessions": empty,
            "target_http_requests": empty,
        }
    )
    safety_stopper = AsyncMock()
    safety_stopper.stop_run.return_value = stop_result
    runtime = TemporalWorkerRuntime(
        worker=AsyncMock(),
        database=database,
        process_supervisor=AsyncMock(),
        terminal_supervisor=AsyncMock(),
        model_provider=AsyncMock(),
        node_service=AsyncMock(),
        node_id="worker-local",
        heartbeat_interval_seconds=0.01,
        run_repository=runs,
        event_repository=events,
        safety_stopper=safety_stopper,
    )

    task = asyncio.create_task(runtime._safety_reconciler_loop())
    try:
        for _ in range(100):
            finalized = await runs.get("run-finalization")
            if finalized is not None and finalized.status is target:
                break
            await asyncio.sleep(0.01)
        assert finalized is not None and finalized.status is target
        assert not task.done()
        timeline = []
        for _ in range(100):
            timeline = list(await events.list_after("run-finalization"))
            if any(
                event.event_type == "run.cleanup_reconciled" for event in timeline
            ):
                break
            await asyncio.sleep(0.01)
        cleaned = [event for event in timeline if event.event_type == "run.cleaned_up"]
        assert len(cleaned) == (0 if defer_cleanup_event else 1)
        reconciled = [event for event in timeline if event.event_type == "run.cleanup_reconciled"]
        assert reconciled[-1].payload["finalization_target"] == target.value
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await database.dispose()


@pytest.mark.asyncio
async def test_user_input_resolver_moves_event_content_to_transcript_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_runtime, "create_worker", lambda *_, **__: FakeWorker())
    runtime = await worker_runtime.build_temporal_worker(
        runtime_config(tmp_path),
        temporal_client=object(),  # type: ignore[arg-type]
    )
    try:
        await SQLAlchemyEngagementRepository(runtime.database.session_factory).create(
            Engagement(id="engagement-1", name="Runtime input")
        )
        runs = SQLAlchemyRunRepository(runtime.database.session_factory)
        await runs.create(
            Run(
                kind="general",
                id="run-1",
                engagement_id="engagement-1",
                node_id="worker-local",
                objective=Objective(description="Resume input"),
                workspace_path=str(tmp_path / "workspaces" / "run-1"),
            )
        )
        sessions = SQLAlchemyAgentSessionRepository(runtime.database.session_factory)
        await sessions.create(AgentSession(id="session-1", run_id="run-1", model_profile="test"))
        events = SQLAlchemyRunEventRepository(runtime.database.session_factory)
        event = await events.append(
            "run-1",
            "user.message_queued",
            {"message": "Continue safely"},
        )
        transcript = SQLAlchemyTranscriptRepository(runtime.database.session_factory)
        resolver = _RunEventUserInputResolver(
            events=events,
            sessions=sessions,
            transcript=transcript,
        )

        first = await resolver.resolve_user_input("run-1", "session-1", event.id)
        retried = await resolver.resolve_user_input("run-1", "session-1", event.id)

        assert retried == first
        messages = await transcript.list_by_session("session-1")
        assert len(messages) == 1
        assert messages[0].content == "Continue safely"
        assert messages[0].structured_content == {
            "role": "user",
            "content": "Continue safely",
            "source_event_id": event.id,
        }
    finally:
        await runtime.close()
