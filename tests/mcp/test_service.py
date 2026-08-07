from __future__ import annotations

import json

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

from riftx.application.errors import ApplicationConflictError
from riftx.config import MCPConfig, MCPServerConfig
from riftx.domain import Objective, Run, RunKind, RunStatus
from riftx.execution import build_execution_key
from riftx.mcp import MCPApplicationService, MCPServerRegistry
from riftx.runtime.types import ToolCallIntent, ToolCallStatus


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def call(
        self,
        server_id: str,
        method: str,
        arguments: dict[str, object],
    ) -> object:
        self.calls.append((server_id, method, arguments))
        if method == "tools/list":
            return [Tool(name="read_doc", inputSchema={"type": "object"})]
        return CallToolResult(
            content=[
                TextContent(type="text", text="result text"),
                ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png"),
            ],
            structuredContent={"answer": 42},
        )

    async def close(self) -> None:
        return None


class FakeRuns:
    def __init__(self, run: Run) -> None:
        self.run = run

    async def get(self, run_id: str) -> Run | None:
        return self.run if run_id == self.run.id else None


class FakeArtifacts:
    def __init__(self) -> None:
        self.saved: list[tuple[str, dict[str, object]]] = []

    async def save(self, run_id: str, **kwargs: object) -> str:
        self.saved.append((run_id, kwargs))
        return "mcp-artifact-1"


class FakeToolCalls:
    def __init__(self, intent: ToolCallIntent, *, claim_current: bool = True) -> None:
        self.intent = intent
        self.claim_current = claim_current
        self.fail_after_checks: int | None = None
        self.claim_checks: list[tuple[str, str, str]] = []

    async def get(self, intent_id: str) -> ToolCallIntent | None:
        return self.intent if intent_id == self.intent.id else None

    async def execution_claim_is_current(
        self,
        intent_id: str,
        *,
        execution_key: str,
        attempt_group: str,
    ) -> bool:
        self.claim_checks.append((intent_id, execution_key, attempt_group))
        return self.claim_current and (
            self.fail_after_checks is None
            or len(self.claim_checks) <= self.fail_after_checks
        )


def run(*, kind: RunKind = RunKind.GENERAL, status: RunStatus = RunStatus.RUNNING) -> Run:
    return Run(
        id="run-1",
        engagement_id="engagement-1",
        objective=Objective(description="Invoke one MCP Tool"),
        workspace_path="/workspace/run-1",
        node_id="worker-local",
        kind=kind,
        status=status,
    )


async def service_for(
    current_run: Run,
) -> tuple[MCPApplicationService, FakeAdapter, FakeArtifacts, FakeToolCalls]:
    adapter = FakeAdapter()
    registry = MCPServerRegistry(
        MCPConfig(
            servers={
                "docs": MCPServerConfig(
                    url="https://mcp.example.test/rpc",
                    allowed_tools=("read_doc",),
                )
            }
        ),
        adapter=adapter,
    )
    await registry.refresh()
    tool_id = registry.snapshot.tools[0].id
    artifacts = FakeArtifacts()
    tool_calls = FakeToolCalls(
        ToolCallIntent(
            id="intent-1",
            run_id=current_run.id,
            session_id="session-1",
            cycle_id="cycle-1",
            step_id="step-1",
            tool_id="call_mcp_tool",
            arguments={"tool_id": tool_id, "arguments": {"query": "hello"}},
            status=ToolCallStatus.EXECUTING,
            engine_call_id="provider-call-1",
        )
    )
    return (
        MCPApplicationService(
            registry=registry,
            runs=FakeRuns(current_run),
            tool_calls=tool_calls,
            artifacts=artifacts,
        ),
        adapter,
        artifacts,
        tool_calls,
    )


async def test_mcp_service_persists_identity_bound_result_and_returns_bounded_preview(
) -> None:
    service, adapter, artifacts, tool_calls = await service_for(run())
    found = service.search_tools("read")
    detail = service.get_tool(str(found[0]["id"]))

    result = await service.invoke(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="intent-1",
        tool_id=str(found[0]["id"]),
        arguments={"query": "hello"},
    )

    assert detail["entry"]["invocation_enabled"] is True
    assert result.tool_call_id == "intent-1"
    assert result.execution_key.startswith("execution:v1:")
    assert result.artifact_id == "mcp-artifact-1"
    assert result.content == [
        {"type": "text", "text": "result text", "truncated": False},
        {
            "type": "image",
            "mime_type": "image/png",
            "encoded_characters": 8,
            "data_omitted": True,
        },
    ]
    assert result.structured_content == {"answer": 42}
    assert len(adapter.calls) == 2
    assert artifacts.saved[0][0] == "run-1"
    envelope = json.loads(bytes(artifacts.saved[0][1]["content"]))
    assert envelope["tool_call_id"] == "intent-1"
    assert envelope["execution_key"] == result.execution_key
    assert envelope["tool_id"] == result.tool_id
    assert envelope["result_sha256"] == result.result_sha256
    expected_key = build_execution_key(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="intent-1",
        attempt_group="mcp",
    )
    assert tool_calls.claim_checks == [
        ("intent-1", expected_key, "mcp"),
        ("intent-1", expected_key, "mcp"),
    ]


async def test_mcp_service_blocks_terminal_run_before_external_call() -> None:
    service, adapter, artifacts, _ = await service_for(run(status=RunStatus.CANCELLING))
    tool_id = str(service.search_tools("")[0]["id"])

    with pytest.raises(ApplicationConflictError) as captured:
        await service.invoke(
            run_id="run-1",
            session_id="session-1",
            tool_call_id="intent-1",
            tool_id=tool_id,
            arguments={},
        )

    assert captured.value.code == "run_mcp_invocation_blocked"
    assert [method for _, method, _ in adapter.calls] == ["tools/list"]
    assert artifacts.saved == []


async def test_mcp_service_rejects_missing_durable_execution_claim_before_external_call() -> None:
    service, adapter, artifacts, tool_calls = await service_for(run())
    tool_calls.claim_current = False
    tool_id = str(service.search_tools("")[0]["id"])

    with pytest.raises(ApplicationConflictError) as captured:
        await service.invoke(
            run_id="run-1",
            session_id="session-1",
            tool_call_id="intent-1",
            tool_id=tool_id,
            arguments={"query": "hello"},
        )

    assert captured.value.code == "mcp_tool_call_not_authorized"
    assert [method for _, method, _ in adapter.calls] == ["tools/list"]
    assert artifacts.saved == []


async def test_mcp_service_does_not_persist_result_after_execution_claim_is_lost() -> None:
    service, adapter, artifacts, tool_calls = await service_for(run())
    tool_calls.fail_after_checks = 1
    tool_id = str(service.search_tools("")[0]["id"])

    with pytest.raises(ApplicationConflictError) as captured:
        await service.invoke(
            run_id="run-1",
            session_id="session-1",
            tool_call_id="intent-1",
            tool_id=tool_id,
            arguments={"query": "hello"},
        )

    assert captured.value.code == "mcp_tool_call_not_authorized"
    assert [method for _, method, _ in adapter.calls] == ["tools/list", "tools/call"]
    assert artifacts.saved == []
