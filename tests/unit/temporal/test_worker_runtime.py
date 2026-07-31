from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

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
from riftx.domain import Engagement, Objective, Run
from riftx.persistence import (
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
        models=ModelsRuntimeConfig(path=models_path),
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

    monkeypatch.setattr(worker_runtime, "create_worker", fake_create_worker)
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
    assert (tmp_path / "workspaces").is_dir()
    assert (tmp_path / "runner").is_dir()
    node = await SQLAlchemyNodeRepository(runtime.database.session_factory).get("worker-local")
    assert node is not None
    assert node.labels["mode"] == "worker-local"
    assert node.labels["tool_count"] == "0"
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
    calls: list[tuple[str, str]] = []
    client = object()

    async def fake_connect(target: str, *, namespace: str) -> object:
        calls.append((target, namespace))
        return client

    monkeypatch.setattr(worker_runtime.Client, "connect", fake_connect)
    monkeypatch.setattr(worker_runtime, "create_worker", lambda *_, **__: FakeWorker())

    runtime = await worker_runtime.build_temporal_worker(runtime_config(tmp_path))
    try:
        assert calls == [("temporal.test:7233", "test-namespace")]
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
    runtime = TemporalWorkerRuntime(
        worker=worker,  # type: ignore[arg-type]
        database=database,
        process_supervisor=process_supervisor,
        terminal_supervisor=terminal_supervisor,
        model_provider=model_provider,
        node_service=nodes,  # type: ignore[arg-type]
        node_id="worker-local",
        heartbeat_interval_seconds=0.01,
    )

    run_task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    await asyncio.wait_for(nodes.first_heartbeat.wait(), timeout=1)
    await asyncio.sleep(0.03)
    assert nodes.heartbeat_calls >= 2

    worker.release.set()
    await asyncio.wait_for(run_task, timeout=1)
    heartbeat_calls_after_close = nodes.heartbeat_calls
    await asyncio.sleep(0.03)

    assert nodes.heartbeat_calls == heartbeat_calls_after_close
    assert nodes.disconnected_node_id == "worker-local"
    terminal_supervisor.close_all.assert_awaited_once_with()
    process_supervisor.close.assert_awaited_once_with(cancel_running=True)
    model_provider.aclose.assert_awaited_once_with()
    database.dispose.assert_awaited_once_with()


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
                id="run-1",
                engagement_id="engagement-1",
                node_id="worker-local",
                objective=Objective(description="Resume input"),
                workspace_path=str(tmp_path / "workspaces" / "run-1"),
            )
        )
        sessions = SQLAlchemyAgentSessionRepository(runtime.database.session_factory)
        await sessions.create(
            AgentSession(id="session-1", run_id="run-1", model_profile="test")
        )
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
