from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from riftx.persistence import SQLAlchemyNodeRepository
from riftx.temporal import worker_runtime
from riftx.temporal.runtime import TemporalRuntimeConfig


@dataclass
class FakeWorker:
    run_calls: int = 0

    async def run(self) -> None:
        self.run_calls += 1


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

    def fake_create_worker(client: object, activities: object, config: object) -> FakeWorker:
        captured.update(client=client, activities=activities, config=config)
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
    assert (tmp_path / "workspaces").is_dir()
    assert (tmp_path / "runner").is_dir()
    node = await SQLAlchemyNodeRepository(runtime.database.session_factory).get("worker-local")
    assert node is not None
    assert node.labels == {"mode": "worker-local"}

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
    monkeypatch.setattr(worker_runtime, "create_worker", lambda *_: FakeWorker())

    runtime = await worker_runtime.build_temporal_worker(runtime_config(tmp_path))
    try:
        assert calls == [("temporal.test:7233", "test-namespace")]
    finally:
        await runtime.close()
