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
