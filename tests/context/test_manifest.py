from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from riftx.api import APISettings, create_app
from riftx.cli.render import render_context
from riftx.context import (
    ContextApplicationService,
    ContextCategory,
    ContextCompilation,
    ContextManifest,
    ManifestingContextCompiler,
)
from riftx.domain import Engagement, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
)
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.runtime.lifecycle import (
    CompiledContext,
    ContextCompileRequest,
    ContextPurpose,
)
from riftx.runtime.types import AgentSession


class StaticCompiler:
    def __init__(self, compiled: CompiledContext) -> None:
        self.compiled = compiled

    async def compile(self, request: ContextCompileRequest) -> CompiledContext:
        return self.compiled.model_copy(deep=True)


@dataclass(slots=True)
class ContextHarness:
    database: Database
    service: ContextApplicationService
    repository: SQLAlchemyContextCompilationRepository

    async def close(self) -> None:
        await self.database.dispose()


@pytest.fixture
async def context_harness(tmp_path: Path) -> AsyncIterator[ContextHarness]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'context-manifest.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Inspect the authorized target"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    await SQLAlchemyAgentSessionRepository(database.session_factory).create(
        AgentSession(
            id="session-1",
            run_id="run-1",
            agent_type="primary",
            model_profile="gpt-test",
        )
    )
    repository = SQLAlchemyContextCompilationRepository(database.session_factory)
    harness = ContextHarness(
        database=database,
        service=ContextApplicationService(repository),
        repository=repository,
    )
    try:
        yield harness
    finally:
        await harness.close()


def _request() -> ContextCompileRequest:
    return ContextCompileRequest(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        model_profile="gpt-test",
        purpose=ContextPurpose.PRIMARY_REASONING,
    )


def test_empty_manifest_keeps_every_category() -> None:
    manifest = ContextManifest.empty(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        model_profile="gpt-test",
        purpose="primary_reasoning",
    )

    assert list(manifest.categories) == list(ContextCategory)
    assert manifest.estimated_tokens == 0
    assert all(usage.item_count == 0 for usage in manifest.categories.values())


async def test_multicategory_manifest_and_tool_schema_accounting(
    context_harness: ContextHarness,
) -> None:
    delegate = StaticCompiler(
        CompiledContext(
            system_instructions=(
                "You are the RiftX primary agent. Follow the authorized run contract.\n"
                "Project rule: preserve evidence."
            ),
            input_items=[
                {"role": "user", "content": "Inspect the service"},
                {
                    "type": "working_memory",
                    "content": {"next_action": "inspect 443"},
                    "source_refs": ["working-memory://1"],
                },
                {
                    "type": "tool_result",
                    "content": "Port 443 is open",
                    "source_refs": ["artifact://runs/run-1/executions/e-1/stdout"],
                },
                {
                    "type": "retrieved_memory",
                    "content": "Target owner approved discovery",
                    "source_refs": ["memory://1"],
                },
                {
                    "type": "subagent_result",
                    "content": "TLS review complete",
                    "source_refs": ["subagent://1"],
                },
            ],
            available_tools=[
                {
                    "type": "function",
                    "name": "inspect_tls",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            loaded_memory_ids=["memory-1"],
        )
    )
    compiler = ManifestingContextCompiler(delegate, context_harness.service)

    compiled = await compiler.compile(_request())

    assert compiled.compilation_id is not None
    persisted = await context_harness.service.get(compiled.compilation_id)
    assert persisted.manifest.categories[ContextCategory.CONVERSATION].item_count == 1
    assert persisted.manifest.categories[ContextCategory.WORKING_MEMORY].item_count == 1
    assert persisted.manifest.categories[ContextCategory.TOOL_RESULTS].item_count == 1
    assert persisted.manifest.categories[ContextCategory.RETRIEVED_MEMORY].item_count == 1
    assert persisted.manifest.categories[ContextCategory.SUBAGENT_RESULTS].item_count == 1
    tool_usage = persisted.manifest.categories[ContextCategory.TOOL_SCHEMAS]
    assert tool_usage.item_count == 1
    assert tool_usage.estimated_tokens > 0
    assert tool_usage.source_refs == ["inspect_tls"]
    assert persisted.estimated_tokens == sum(
        usage.estimated_tokens for usage in persisted.manifest.categories.values()
    )
    assert compiled.token_estimate == persisted.estimated_tokens


async def test_usage_backfill_and_manifest_persistence_survive_reload(
    context_harness: ContextHarness,
) -> None:
    compiler = ManifestingContextCompiler(
        StaticCompiler(
            CompiledContext(
                system_instructions="runtime",
                input_items=[{"role": "user", "content": "hello"}],
            )
        ),
        context_harness.service,
    )
    compiled = await compiler.compile(_request())
    assert compiled.compilation_id is not None

    updated = await compiler.record_usage(
        compiled.compilation_id,
        {"input_tokens": 321, "output_tokens": 45},
    )
    reloaded = await context_harness.repository.get(compiled.compilation_id)

    assert updated.actual_input_tokens == 321
    assert updated.actual_output_tokens == 45
    assert reloaded == updated
    assert await context_harness.repository.latest_for_session("session-1") == updated
    assert await context_harness.repository.latest_for_run("run-1") == updated


async def test_context_inspector_api_and_cli_output(
    context_harness: ContextHarness,
    tmp_path: Path,
) -> None:
    compilation = await context_harness.service.create(
        ContextCompilation(
            id="compilation-1",
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
            model_profile="gpt-test",
            purpose="primary_reasoning",
            manifest=ContextManifest.empty(
                run_id="run-1",
                session_id="session-1",
                agent_id="primary",
                model_profile="gpt-test",
                purpose="primary_reasoning",
            ),
            actual_input_tokens=12,
            actual_output_tokens=4,
        )
    )
    control_plane = SimpleNamespace(
        settings=APISettings(web_dist_path=tmp_path / "missing-web"),
        context_service=context_harness.service,
    )
    with TestClient(create_app(control_plane=control_plane)) as client:
        session_response = client.get("/api/v1/sessions/session-1/context")
        detail_response = client.get("/api/v1/context-compilations/compilation-1")
        run_response = client.get("/api/v1/runs/run-1/context")

    assert session_response.status_code == 200
    assert detail_response.status_code == 200
    assert run_response.status_code == 200
    assert run_response.json()["id"] == compilation.id
    assert session_response.json()["id"] == compilation.id
    assert detail_response.json()["manifest"]["categories"]["tool_schemas"]["estimated_tokens"] == 0

    console = Console(record=True, width=140)
    render_context(console, detail_response.json())
    output = console.export_text()
    assert "Context Inspector" in output
    assert "Runtime Contract" in output
    assert "Tool Schemas" in output
    assert "Actual input/output" in output
    assert "12/4" in output
