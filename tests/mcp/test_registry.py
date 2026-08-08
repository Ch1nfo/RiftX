from __future__ import annotations

import asyncio
import json

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent, Tool, ToolAnnotations
from pydantic import ValidationError

from riftx.config import MCPConfig, MCPServerConfig
from riftx.mcp import (
    MCPServerAvailability,
    MCPServerConfigurationError,
    MCPServerRegistry,
    MCPToolInvocationError,
    OpenAIMCPAdapter,
    OpenAIMCPServerFactory,
)


class FakeAdapter:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []
        self.arguments: list[dict[str, object]] = []
        self.closed = False

    async def call(
        self,
        server_id: str,
        method: str,
        _arguments: dict[str, object],
    ) -> object:
        self.calls.append((server_id, method))
        self.arguments.append(_arguments)
        response = self.responses.get(f"{server_id}:{method}", self.responses[server_id])
        if isinstance(response, Exception):
            raise response
        if isinstance(response, asyncio.Event):
            await response.wait()
        return response

    async def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(
        self,
        tools: list[Tool],
        *,
        connect_gate: asyncio.Event | None = None,
    ) -> None:
        self.tools = tools
        self.connect_gate = connect_gate
        self.closed = False

    async def connect(self) -> None:
        if self.connect_gate is not None:
            await self.connect_gate.wait()

    async def cleanup(self) -> None:
        self.closed = True

    async def list_tools(self) -> list[Tool]:
        return self.tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object] | None,
        meta: dict[str, object] | None = None,
    ) -> CallToolResult:
        del tool_name, arguments, meta
        return CallToolResult(content=[])


def server(url: str, **updates: object) -> MCPServerConfig:
    return MCPServerConfig.model_validate({"url": url, **updates})


def test_mcp_server_config_accepts_only_secret_references_and_safe_remote_urls() -> None:
    configured = server(
        "https://mcp.example.test/rpc",
        header_env={"Authorization": "MCP_TOKEN"},
        allowed_tools=["read_doc"],
    )

    assert configured.header_env == {"Authorization": "MCP_TOKEN"}
    with pytest.raises(ValidationError, match="without credentials"):
        server("https://user:secret@mcp.example.test/rpc")
    with pytest.raises(ValidationError, match="environment reference"):
        server(
            "https://mcp.example.test/rpc",
            header_env={"Authorization": "literal secret"},
        )
    with pytest.raises(ValidationError, match="both allowed and blocked"):
        server(
            "https://mcp.example.test/rpc",
            allowed_tools=["read_doc"],
            blocked_tools=["read_doc"],
        )


def test_openai_factory_fails_closed_when_header_secret_is_missing() -> None:
    factory = OpenAIMCPServerFactory(environment={})

    with pytest.raises(MCPServerConfigurationError) as captured:
        factory(
            "docs",
            server(
                "https://mcp.example.test/rpc",
                header_env={"Authorization": "MCP_TOKEN"},
            ),
        )

    assert captured.value.code == "mcp_secret_unavailable"


async def test_registry_isolates_servers_and_projects_safe_bounded_tool_index() -> None:
    deeply_nested_schema: dict[str, object] = {"type": "string"}
    for _ in range(66):
        deeply_nested_schema = {
            "type": "object",
            "properties": {"nested": deeply_nested_schema},
        }
    adapter = FakeAdapter(
        {
            "docs": [
                Tool(
                    name="read_doc",
                    title="Read documentation",
                    description="Read public docs from /Users/operator/private-source.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Never reveal C:\\Secrets\\token.txt",
                            }
                        },
                        "required": ["query"],
                    },
                    annotations=ToolAnnotations(
                        readOnlyHint=True,
                        destructiveHint=False,
                        idempotentHint=True,
                        openWorldHint=True,
                    ),
                ),
                Tool(name=" read_doc ", inputSchema={"type": "object"}),
                Tool(name="invalid", inputSchema={"type": "array"}),
                Tool(name="/Users/operator/private-tool", inputSchema={"type": "object"}),
                Tool(name="too_deep", inputSchema=deeply_nested_schema),
                Tool(
                    name="echo_secret",
                    description=(
                        "Never expose actual-mcp-secret or "
                        "https://mcp.example.test/rpc; see https://public.example/docs"
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "token": {
                                "type": "string",
                                "description": "actual-mcp-secret",
                            }
                        },
                    },
                ),
            ],
            "broken": RuntimeError("upstream leaked diagnostic /srv/secret"),
        }
    )
    registry = MCPServerRegistry(
        MCPConfig(
            servers={
                "docs": server(
                    "https://mcp.example.test/rpc",
                    header_env={"Authorization": "MCP_TOKEN"},
                ),
                "broken": server("https://broken.example.test/rpc"),
                "disabled": server("https://disabled.example.test/rpc", enabled=False),
            }
        ),
        adapter=adapter,
        environment={"MCP_TOKEN": "actual-mcp-secret"},
    )

    snapshot = await registry.refresh()

    assert [(item.server_id, item.availability) for item in snapshot.servers] == [
        ("broken", MCPServerAvailability.UNAVAILABLE),
        ("disabled", MCPServerAvailability.DISABLED),
        ("docs", MCPServerAvailability.READY),
    ]
    assert snapshot.servers[0].error_code == "mcp_server_unavailable"
    assert snapshot.servers[2].tool_count == 2
    assert snapshot.servers[2].rejected_tool_count == 4
    assert adapter.calls == [("docs", "tools/list"), ("broken", "tools/list")]
    assert all(tool.invocation_enabled is False for tool in snapshot.tools)

    entry = next(tool for tool in snapshot.tools if tool.name == "read_doc")
    assert entry.server_id == "docs"
    assert entry.name == "read_doc"
    assert len(entry.id) <= 64
    assert entry.read_only_hint is True
    assert entry.destructive_hint is False
    assert "REDACTED_PATH" in entry.description
    assert registry.index.search("documentation") == [entry]
    schema = registry.index.schema(entry.id)
    assert schema.generation == snapshot.generation
    assert schema.full_schema["name"] == entry.id
    assert schema.full_schema["x-riftx"] == {
        "tool_id": entry.id,
        "execution_type": "mcp",
        "approval_policy": "explicit",
        "content_trust": "UNTRUSTED_EXTERNAL_CONTENT",
        "mcp_server_id": "docs",
        "mcp_tool_name": "read_doc",
    }
    serialized = json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False)
    serialized_schema = json.dumps(
        [registry.index.schema(tool.id).model_dump(mode="json") for tool in snapshot.tools],
        ensure_ascii=False,
    )
    for forbidden in (
        "MCP_TOKEN",
        "actual-mcp-secret",
        "https://mcp.example.test",
        "/Users/operator/private-source",
        "C:\\Secrets\\token.txt",
        "/srv/secret",
    ):
        assert forbidden not in serialized
        assert forbidden not in serialized_schema
    assert "https://public.example/docs" in serialized

    second = await registry.refresh()
    assert second.generation == snapshot.generation + 1
    assert [tool.id for tool in second.tools] == [tool.id for tool in snapshot.tools]
    await registry.close()
    assert adapter.closed is True


async def test_openai_adapter_connects_servers_independently() -> None:
    slow_gate = asyncio.Event()
    clients = {
        "slow": FakeClient([], connect_gate=slow_gate),
        "ready": FakeClient([Tool(name="ping", inputSchema={"type": "object"})]),
    }
    servers = {
        "slow": server("https://slow.example.test/rpc"),
        "ready": server("https://ready.example.test/rpc"),
    }
    adapter = OpenAIMCPAdapter(
        servers,
        factory=lambda server_id, _definition: clients[server_id],
    )
    registry = MCPServerRegistry(
        MCPConfig(
            discovery_timeout_seconds=0.01,
            servers=servers,
        ),
        adapter=adapter,
    )

    snapshot = await registry.refresh()

    by_id = {item.server_id: item for item in snapshot.servers}
    assert by_id["slow"].error_code == "mcp_discovery_timeout"
    assert by_id["ready"].availability is MCPServerAvailability.READY
    assert [tool.name for tool in snapshot.tools] == ["ping"]
    assert clients["slow"].closed is True
    await registry.close()
    assert clients["ready"].closed is True


async def test_registry_invokes_only_allowlisted_tools_with_sanitized_results() -> None:
    adapter = FakeAdapter(
        {
            "docs": [
                Tool(
                    name="read_doc",
                    description="Read one document",
                    inputSchema={"type": "object"},
                )
            ],
            "docs:tools/call": CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            "actual-mcp-secret /Users/operator/private "
                            "https://mcp.example.test/rpc"
                        ),
                    ),
                    ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png"),
                ],
                structuredContent={"path": "C:\\Secrets\\token.txt"},
            ),
        }
    )
    registry = MCPServerRegistry(
        MCPConfig(
            servers={
                "docs": server(
                    "https://mcp.example.test/rpc",
                    header_env={"Authorization": "MCP_TOKEN"},
                    allowed_tools=["read_doc"],
                )
            }
        ),
        adapter=adapter,
        environment={"MCP_TOKEN": "actual-mcp-secret"},
    )
    snapshot = await registry.refresh()
    tool = snapshot.tools[0]

    entry, result = await registry.invoke(
        tool.id,
        {"path": "/remote/work"},
        execution_key="execution:v1:one",
    )

    assert entry.invocation_enabled is True
    assert adapter.calls == [("docs", "tools/list"), ("docs", "tools/call")]
    assert adapter.arguments[-1] == {
        "name": "read_doc",
        "arguments": {"path": "/remote/work"},
        "execution_key": "execution:v1:one",
    }
    serialized = json.dumps(result, ensure_ascii=False)
    for forbidden in (
        "actual-mcp-secret",
        "/Users/operator/private",
        "C:\\Secrets\\token.txt",
        "https://mcp.example.test/rpc",
    ):
        assert forbidden not in serialized

    discovery_only = MCPServerRegistry(
        MCPConfig(
            servers={"docs": server("https://mcp.example.test/rpc")},
        ),
        adapter=FakeAdapter({"docs": [Tool(name="read_doc", inputSchema={})]}),
    )
    discovery_snapshot = await discovery_only.refresh()
    with pytest.raises(MCPToolInvocationError) as captured:
        await discovery_only.invoke(
            discovery_snapshot.tools[0].id,
            {},
            execution_key="execution:v1:two",
        )
    assert captured.value.code == "mcp_tool_invocation_disabled"


async def test_registry_bounds_discovery_timeout_without_blocking_other_servers() -> None:
    never = asyncio.Event()
    adapter = FakeAdapter(
        {
            "slow": never,
            "ready": [Tool(name="ping", inputSchema={"type": "object"})],
        }
    )
    registry = MCPServerRegistry(
        MCPConfig(
            discovery_timeout_seconds=0.01,
            servers={
                "slow": server("https://slow.example.test/rpc"),
                "ready": server("https://ready.example.test/rpc"),
            },
        ),
        adapter=adapter,
    )

    snapshot = await registry.refresh()

    by_id = {item.server_id: item for item in snapshot.servers}
    assert by_id["slow"].error_code == "mcp_discovery_timeout"
    assert by_id["ready"].availability is MCPServerAvailability.READY
    assert [tool.name for tool in snapshot.tools] == ["ping"]
