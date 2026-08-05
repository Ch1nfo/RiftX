from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from riftx.runtime.engine import AgentEngineRequest, DeferredRuntimeAgentFactory
from riftx.runtime.lifecycle import ContextCompileRequest, DynamicToolContextCompiler
from riftx.tools import ToolContextManager, ToolNotFoundError, ToolRegistry


@pytest.mark.parametrize(
    ("execution_policy", "shell_visible"),
    [("registered_only", False), ("open", True)],
)
async def test_registry_policy_controls_shell_visibility_end_to_end(
    tmp_path: Path,
    execution_policy: str,
    shell_visible: bool,
) -> None:
    async def control_handler(
        _scope: object,
        _name: str,
        _arguments: dict[str, object],
        _call_id: str,
    ) -> object:
        return {"status": "ok"}

    config_path = tmp_path / "tools.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": execution_policy,
                "tools": {},
            },
            sort_keys=False,
        )
    )
    registry = ToolRegistry(config_path, node_id="local")
    await registry.refresh()
    tool_context = ToolContextManager(registry)
    compiled = await DynamicToolContextCompiler(tool_context).compile(
        ContextCompileRequest(
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
            model_profile="test-model",
        )
    )
    agent = DeferredRuntimeAgentFactory(control_handler=control_handler)(
        AgentEngineRequest(
            session_id="session-1",
            model="test-model",
            context=compiled,
        )
    )

    compiled_names = {str(schema.get("name")) for schema in compiled.available_tools}
    model_tool_names = {tool.name for tool in agent.tools}
    assert registry.available_tools() == []
    assert compiled.context_manifest["execution_policy"] == execution_policy
    assert ("run_shell" in compiled_names) is shell_visible
    assert ("run_shell" in model_tool_names) is shell_visible
    assert model_tool_names == compiled_names
    reference_schema = next(
        schema for schema in compiled.available_tools if schema.get("name") == "find_references"
    )
    assert reference_schema["parameters"]["required"] == ["symbol"]
    assert set(reference_schema["parameters"]["properties"]) == {
        "symbol",
        "path",
        "file_glob",
        "include_declarations",
        "max_results",
    }
    call_schema = next(
        schema for schema in compiled.available_tools if schema.get("name") == "call_hierarchy"
    )
    assert call_schema["parameters"]["required"] == ["symbol"]
    assert call_schema["parameters"]["properties"]["direction"]["enum"] == [
        "incoming",
        "outgoing",
        "both",
    ]
    diagnostic_schema = next(
        schema for schema in compiled.available_tools if schema.get("name") == "diagnostics"
    )
    assert diagnostic_schema["parameters"]["required"] == []
    assert set(diagnostic_schema["parameters"]["properties"]) == {
        "path",
        "file_glob",
        "max_results",
    }
    patch_schema = next(
        schema for schema in compiled.available_tools if schema.get("name") == "apply_patch"
    )
    assert patch_schema["parameters"]["required"] == ["patch"]
    assert patch_schema["x-riftx"]["approval_policy"] == "explicit"
    revert_schema = next(
        schema for schema in compiled.available_tools if schema.get("name") == "revert_patch"
    )
    assert revert_schema["parameters"]["required"] == ["receipt_artifact_id"]
    assert {
        tool.name for tool in agent.tools if tool.name in {"apply_patch", "revert_patch"}
    } == {"apply_patch", "revert_patch"}
    assert all(
        tool.needs_approval is True
        for tool in agent.tools
        if tool.name in {"apply_patch", "revert_patch"}
    )
    open_browser_schema = next(
        schema for schema in compiled.available_tools if schema.get("name") == "open_browser"
    )
    assert open_browser_schema["parameters"]["required"] == ["url"]
    assert open_browser_schema["x-riftx"]["approval_policy"] == "explicit"
    act_browser_schema = next(
        schema for schema in compiled.available_tools if schema.get("name") == "act_browser"
    )
    assert act_browser_schema["parameters"]["required"] == [
        "browser_session_id",
        "page_id",
        "observation_version",
        "action",
    ]
    assert "evaluate" not in act_browser_schema["parameters"]["properties"]["action"][
        "enum"
    ]
    assert "upload" not in act_browser_schema["parameters"]["properties"]["action"]["enum"]
    assert {
        tool.name
        for tool in agent.tools
        if tool.name
        in {"open_browser", "observe_browser", "act_browser", "close_browser"}
    } == {"open_browser", "observe_browser", "act_browser", "close_browser"}
    assert {
        tool.name: tool.needs_approval
        for tool in agent.tools
        if tool.name
        in {"open_browser", "observe_browser", "act_browser", "close_browser"}
    } == {
        "open_browser": True,
        "observe_browser": False,
        "act_browser": True,
        "close_browser": False,
    }
    web_fetch_schema = next(
        schema for schema in compiled.available_tools if schema.get("name") == "web_fetch"
    )
    assert web_fetch_schema["parameters"]["required"] == ["url"]
    assert web_fetch_schema["x-riftx"]["approval_policy"] == "explicit"
    web_fetch_tool = next(tool for tool in agent.tools if tool.name == "web_fetch")
    assert web_fetch_tool.needs_approval is True
    web_search_schema = next(
        schema for schema in compiled.available_tools if schema.get("name") == "web_search"
    )
    assert web_search_schema["parameters"]["required"] == ["query"]
    assert web_search_schema["x-riftx"]["approval_policy"] == "explicit"
    web_research_schema = next(
        schema for schema in compiled.available_tools if schema.get("name") == "web_research"
    )
    assert web_research_schema["parameters"]["required"] == ["question"]
    assert web_research_schema["x-riftx"]["approval_policy"] == "explicit"
    assert {
        tool.name: tool.needs_approval
        for tool in agent.tools
        if tool.name in {"web_search", "web_research"}
    } == {"web_search": True, "web_research": True}
    query_traffic_schema = next(
        schema
        for schema in compiled.available_tools
        if schema.get("name") == "query_http_traffic"
    )
    assert query_traffic_schema["parameters"]["required"] == []
    read_exchange_schema = next(
        schema
        for schema in compiled.available_tools
        if schema.get("name") == "read_http_exchange"
    )
    assert read_exchange_schema["parameters"]["required"] == ["exchange_id"]
    target_http_schema = next(
        schema
        for schema in compiled.available_tools
        if schema.get("name") == "target_http_request"
    )
    assert target_http_schema["parameters"]["required"] == ["method", "url"]
    assert target_http_schema["x-riftx"]["approval_policy"] == "explicit"
    assert {
        tool.name: tool.needs_approval
        for tool in agent.tools
        if tool.name
        in {"query_http_traffic", "read_http_exchange", "target_http_request"}
    } == {
        "query_http_traffic": False,
        "read_http_exchange": False,
        "target_http_request": True,
    }
    call_mcp_schema = next(
        schema for schema in compiled.available_tools if schema.get("name") == "call_mcp_tool"
    )
    assert call_mcp_schema["parameters"]["required"] == ["tool_id", "arguments"]
    assert call_mcp_schema["x-riftx"]["approval_policy"] == "explicit"
    assert {
        tool.name: tool.needs_approval
        for tool in agent.tools
        if tool.name in {"search_mcp_tools", "get_mcp_tool", "call_mcp_tool"}
    } == {
        "search_mcp_tools": False,
        "get_mcp_tool": False,
        "call_mcp_tool": True,
    }
    assert {
        "search_tools",
        "list_tools",
        "get_tool",
        "list_files",
        "read_file",
        "read_many_files",
        "grep",
        "glob",
        "symbol_search",
        "find_references",
        "call_hierarchy",
        "diagnostics",
        "apply_patch",
        "revert_patch",
        "git_status",
        "git_diff",
        "git_log",
        "open_browser",
        "observe_browser",
        "act_browser",
        "close_browser",
        "web_fetch",
        "web_search",
        "web_research",
        "query_http_traffic",
        "read_http_exchange",
        "target_http_request",
        "search_mcp_tools",
        "get_mcp_tool",
        "call_mcp_tool",
        "get_execution",
        "wait_execution",
        "cancel_execution",
        "read_artifact",
    }.isdisjoint(agent.tool_use_behavior["stop_at_tool_names"])
    assert "complete_run" in agent.tool_use_behavior["stop_at_tool_names"]

    if shell_visible:
        shell_schema = next(
            schema for schema in compiled.available_tools if schema.get("name") == "run_shell"
        )
        assert shell_schema["x-riftx"] == {
            "resident": True,
            "execution_policy": "open",
        }
    else:
        with pytest.raises(ToolNotFoundError):
            tool_context.load_tool(
                "run_shell",
                run_id="run-1",
                session_id="session-1",
                agent_id="primary",
            )
