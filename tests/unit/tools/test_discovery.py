from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from riftx.domain import ToolAvailability
from riftx.runtime.lifecycle import (
    ContextCompileRequest,
    DynamicToolContextCompiler,
)
from riftx.tools import (
    RESIDENT_TOOL_IDS,
    ToolContextManager,
    ToolNotFoundError,
    ToolRegistry,
    ToolSearchRequest,
    ToolUnavailableError,
)


def _write_tools(path: Path, count: int, *, include_unavailable: bool = False) -> None:
    tools: dict[str, object] = {}
    for index in range(count):
        tool_id = "netexec-smb" if index == count - 1 else f"tool-{index:03d}"
        tools[tool_id] = {
            "enabled": not (include_unavailable and index == 0),
            "command": [sys.executable],
            "short_description": (
                "Enumerate Windows SMB shares and users"
                if tool_id == "netexec-smb"
                else f"Deterministic utility {index}"
            ),
            "description": f"Full schema description for {tool_id}.",
            "capabilities": (
                ["smb_enumeration", "network_share_discovery"]
                if tool_id == "netexec-smb"
                else [f"capability_{index}"]
            ),
            "synonyms": ["CIFS recon"] if tool_id == "netexec-smb" else [],
        }
    path.write_text(yaml.safe_dump({"version": 1, "tools": tools}, sort_keys=False))


async def _registry(tmp_path: Path, count: int, **kwargs: object) -> ToolRegistry:
    path = tmp_path / f"tools-{count}.yaml"
    _write_tools(path, count, **kwargs)
    registry = ToolRegistry(path, node_id="node-1")
    await registry.refresh()
    return registry


async def test_tool_index_exposes_required_fields_for_ten_tools(tmp_path: Path) -> None:
    manager = ToolContextManager(await _registry(tmp_path, 10))

    entries = manager.list_tools()

    assert len(entries) == 10
    assert entries[-1].id == "tool-008"
    smb = next(item for item in entries if item.id == "netexec-smb")
    assert smb.short_description == "Enumerate Windows SMB shares and users"
    assert smb.capabilities == ["smb_enumeration", "network_share_discovery"]
    assert smb.availability is ToolAvailability.AVAILABLE
    assert smb.execution_type.value == "process"


async def test_hundred_tools_do_not_pollute_initial_context(tmp_path: Path) -> None:
    registry = await _registry(tmp_path, 100)
    manager = ToolContextManager(registry)
    compiler = DynamicToolContextCompiler(manager)

    compiled = await compiler.compile(
        ContextCompileRequest(
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
            model_profile="test-model",
        )
    )

    assert [schema["name"] for schema in compiled.available_tools] == list(RESIDENT_TOOL_IDS)
    assert compiled.context_manifest["dynamically_loaded_tools"] == []
    assert len(compiled.context_manifest["hidden_available_tools"]) == 100


async def test_capability_and_synonym_search_discover_smb_tool(tmp_path: Path) -> None:
    manager = ToolContextManager(await _registry(tmp_path, 10))

    by_capability = manager.search_tools(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        request=ToolSearchRequest(capability="smb enumeration"),
    )
    by_synonym = manager.search_tools(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        request=ToolSearchRequest(query="Windows share discovery"),
    )

    assert [result.tool.id for result in by_capability] == ["netexec-smb"]
    assert by_synonym[0].tool.id == "netexec-smb"
    assert {"smb", "enumeration"} <= set(by_synonym[0].matched_terms)


async def test_unavailable_tool_is_discoverable_but_cannot_be_loaded(tmp_path: Path) -> None:
    manager = ToolContextManager(
        await _registry(tmp_path, 10, include_unavailable=True)
    )

    results = manager.search_tools(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        request=ToolSearchRequest(query="utility 0"),
    )

    assert results[0].tool.id == "tool-000"
    assert results[0].tool.availability is ToolAvailability.DISABLED
    with pytest.raises(ToolUnavailableError):
        manager.load_tool(
            "tool-000",
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
        )


async def test_subagents_keep_independent_dynamic_tool_sets(tmp_path: Path) -> None:
    manager = ToolContextManager(await _registry(tmp_path, 10))
    manager.load_tool(
        "netexec-smb",
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
    )

    primary = manager.visibility(
        run_id="run-1", session_id="session-1", agent_id="primary"
    )
    subagent = manager.visibility(
        run_id="run-1", session_id="session-1", agent_id="subagent-1"
    )

    assert primary.dynamically_loaded_tools == ["netexec-smb"]
    assert subagent.dynamically_loaded_tools == []
    assert "netexec-smb" in subagent.hidden_available_tools


async def test_subagent_tool_allowlist_hides_and_rejects_unassigned_tools(
    tmp_path: Path,
) -> None:
    manager = ToolContextManager(await _registry(tmp_path, 10))
    manager.restrict_tools(
        ["search_tools", "get_tool", "run_registered_tool", "netexec-smb"],
        run_id="run-1",
        session_id="subagent-session",
        agent_id="subagent:recon",
    )
    manager.load_tool(
        "netexec-smb",
        run_id="run-1",
        session_id="subagent-session",
        agent_id="subagent:recon",
    )

    visibility = manager.visibility(
        run_id="run-1",
        session_id="subagent-session",
        agent_id="subagent:recon",
    )
    search = manager.search_tools(
        run_id="run-1",
        session_id="subagent-session",
        agent_id="subagent:recon",
        request=ToolSearchRequest(query="utility"),
    )

    assert visibility.dynamically_loaded_tools == ["netexec-smb"]
    assert visibility.always_visible_tools == [
        "search_tools",
        "get_tool",
        "run_registered_tool",
    ]
    assert visibility.hidden_available_tools == []
    assert search == []
    with pytest.raises(ToolNotFoundError):
        manager.load_tool(
            "tool-001",
            run_id="run-1",
            session_id="subagent-session",
            agent_id="subagent:recon",
        )


async def test_registry_hot_reload_rebuilds_index_and_selected_schema(tmp_path: Path) -> None:
    registry = await _registry(tmp_path, 10)
    manager = ToolContextManager(registry)
    first = manager.load_tool(
        "netexec-smb",
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
    )
    raw = yaml.safe_load(registry.config_path.read_text())
    raw["tools"]["netexec-smb"]["description"] = "Reloaded full schema."
    raw["tools"]["new-tool"] = {
        "command": [sys.executable],
        "capabilities": ["new_capability"],
    }
    registry.config_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    await registry.reload_if_changed()
    second = manager.index.schema("netexec-smb")
    visibility = manager.visibility(
        run_id="run-1", session_id="session-1", agent_id="primary"
    )

    assert second.generation == first.generation + 1
    assert second.full_schema["description"] == "Reloaded full schema."
    assert "new-tool" in visibility.hidden_available_tools
    assert visibility.dynamically_loaded_tools == ["netexec-smb"]
