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
    SUBAGENT_RESIDENT_TOOL_IDS,
    ExecutionPolicy,
    ToolContextManager,
    ToolNotFoundError,
    ToolRegistry,
    ToolSearchRequest,
    ToolUnavailableError,
)


def _write_tools(
    path: Path,
    count: int,
    *,
    include_unavailable: bool = False,
    execution_policy: str = "registered_only",
) -> None:
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
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": execution_policy,
                "tools": tools,
            },
            sort_keys=False,
        )
    )


async def _registry(
    tmp_path: Path,
    count: int,
    *,
    include_unavailable: bool = False,
    execution_policy: str = "registered_only",
) -> ToolRegistry:
    path = tmp_path / f"tools-{count}.yaml"
    _write_tools(
        path,
        count,
        include_unavailable=include_unavailable,
        execution_policy=execution_policy,
    )
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

    assert [schema["name"] for schema in compiled.available_tools] == [
        tool_id for tool_id in RESIDENT_TOOL_IDS if tool_id != "run_shell"
    ]
    assert compiled.context_manifest["execution_policy"] == "registered_only"
    assert compiled.context_manifest["dynamically_loaded_tools"] == []
    assert len(compiled.context_manifest["hidden_available_tools"]) == 100


async def test_run_shell_resident_schema_requires_script(tmp_path: Path) -> None:
    manager = ToolContextManager(await _registry(tmp_path, 10, execution_policy="open"))

    schema = next(
        item
        for item in (await manager.visibility(
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
        )).available_tools
        if item["name"] == "run_shell"
    )

    parameters = schema["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["required"] == ["script"]
    assert set(parameters["properties"]) == {
        "script",
        "cwd",
        "environment",
        "timeout_seconds",
    }
    assert parameters["additionalProperties"] is False
    assert schema["x-riftx"] == {
        "resident": True,
        "execution_policy": "open",
    }


async def test_target_http_schema_exposes_references_instead_of_cookie_values(
    tmp_path: Path,
) -> None:
    manager = ToolContextManager(await _registry(tmp_path, 10))
    schema = next(
        item
        for item in (
            await manager.visibility(
                run_id="run-1",
                session_id="session-1",
                agent_id="primary",
            )
        ).available_tools
        if item["name"] == "target_http_request"
    )

    parameters = schema["parameters"]
    assert isinstance(parameters, dict)
    properties = parameters["properties"]
    assert isinstance(properties, dict)
    assert "cookies" not in properties
    assert {
        "header_secret_refs",
        "body_secret_ref",
        "cookie_secret_refs",
    } <= set(properties)


async def test_task_planner_resident_schemas_are_strict_and_role_scoped(
    tmp_path: Path,
) -> None:
    manager = ToolContextManager(await _registry(tmp_path, 10))
    schemas = {
        item["name"]: item
        for item in (
            await manager.visibility(
                run_id="run-1",
                session_id="session-1",
                agent_id="primary",
            )
        ).available_tools
    }

    add_parameters = schemas["add_task"]["parameters"]
    update_parameters = schemas["update_task"]["parameters"]
    complete_parameters = schemas["complete_task"]["parameters"]
    assert add_parameters["required"] == ["expected_graph_version", "title"]
    assert update_parameters["required"] == ["expected_graph_version", "task_id"]
    assert update_parameters["properties"]["title"]["type"] == "string"
    assert complete_parameters["required"] == [
        "expected_graph_version",
        "task_id",
        "attempt_id",
        "completion_summary",
    ]
    assert all(
        parameters["additionalProperties"] is False
        for parameters in (add_parameters, update_parameters, complete_parameters)
    )
    assert {
        "list_ready_tasks",
        "claim_ready_task",
        "complete_task",
        "fail_task_attempt",
    } <= set(SUBAGENT_RESIDENT_TOOL_IDS)
    assert {
        "add_task",
        "update_task",
        "link_tasks",
        "block_task",
        "reopen_task",
        "cancel_task",
    }.isdisjoint(SUBAGENT_RESIDENT_TOOL_IDS)
    cognitive_tools = {
        "propose_plan_update",
        "register_artifact_evidence",
        "record_observation",
        "propose_fact",
        "propose_hypothesis",
        "record_attempt",
        "propose_finding",
        "create_finding",
        "record_negative_result",
        "query_reasoning_graph",
    }
    assert cognitive_tools <= set(RESIDENT_TOOL_IDS)
    assert cognitive_tools.isdisjoint(SUBAGENT_RESIDENT_TOOL_IDS)
    assert "create_worktree" in RESIDENT_TOOL_IDS
    assert "create_worktree" not in SUBAGENT_RESIDENT_TOOL_IDS
    assert schemas["record_observation"]["parameters"]["properties"]["evidence_ids"][
        "minItems"
    ] == 1
    assert schemas["register_artifact_evidence"]["parameters"]["required"] == [
        "artifact_id",
        "start_offset",
        "end_offset",
    ]
    assert "status" not in schemas["propose_finding"]["parameters"]["properties"]
    create_finding = schemas["create_finding"]["parameters"]
    assert create_finding["required"] == [
        "reasoning_node_id",
        "title",
        "severity",
        "evidence",
    ]
    assert "status" not in create_finding["properties"]
    assert create_finding["properties"]["evidence"]["minItems"] == 1


async def test_registered_only_hides_and_rejects_run_shell(tmp_path: Path) -> None:
    manager = ToolContextManager(await _registry(tmp_path, 10))

    visibility = await manager.visibility(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
    )

    assert visibility.execution_policy is ExecutionPolicy.REGISTERED_ONLY
    assert "run_shell" not in visibility.always_visible_tools
    assert "run_shell" not in {str(schema.get("name")) for schema in visibility.available_tools}
    with pytest.raises(ToolNotFoundError):
        await manager.load_tool(
            "run_shell",
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
        )
    with pytest.raises(ToolNotFoundError):
        await manager.assert_allowed(
            "run_shell",
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
        )
    with pytest.raises(ToolNotFoundError):
        await manager.restrict_tools(
            ["search_tools", "run_shell"],
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
        )


async def test_capability_and_synonym_search_discover_smb_tool(tmp_path: Path) -> None:
    manager = ToolContextManager(await _registry(tmp_path, 10))

    by_capability = await manager.search_tools(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        request=ToolSearchRequest(capability="smb enumeration"),
    )
    by_synonym = await manager.search_tools(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        request=ToolSearchRequest(query="Windows share discovery"),
    )

    assert [result.tool.id for result in by_capability] == ["netexec-smb"]
    assert by_synonym[0].tool.id == "netexec-smb"
    assert {"smb", "enumeration"} <= set(by_synonym[0].matched_terms)


async def test_unavailable_tool_is_discoverable_but_cannot_be_loaded(tmp_path: Path) -> None:
    manager = ToolContextManager(await _registry(tmp_path, 10, include_unavailable=True))

    results = await manager.search_tools(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        request=ToolSearchRequest(query="utility 0"),
    )

    assert results[0].tool.id == "tool-000"
    assert results[0].tool.availability is ToolAvailability.DISABLED
    with pytest.raises(ToolUnavailableError):
        await manager.load_tool(
            "tool-000",
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
        )


async def test_subagents_keep_independent_dynamic_tool_sets(tmp_path: Path) -> None:
    manager = ToolContextManager(await _registry(tmp_path, 10))
    await manager.load_tool(
        "netexec-smb",
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
    )

    primary = await manager.visibility(
        run_id="run-1", session_id="session-1", agent_id="primary"
    )
    subagent = await manager.visibility(
        run_id="run-1", session_id="session-2", agent_id="subagent-1"
    )

    assert primary.dynamically_loaded_tools == ["netexec-smb"]
    assert subagent.dynamically_loaded_tools == []
    assert "netexec-smb" in subagent.hidden_available_tools
    await manager.restrict_tools(
        list(SUBAGENT_RESIDENT_TOOL_IDS),
        run_id="run-1",
        session_id="session-1",
        agent_id="subagent:recon",
    )
    production_subagent = await manager.visibility(
        run_id="run-1",
        session_id="session-1",
        agent_id="subagent:recon",
    )
    assert {
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
    }.isdisjoint(production_subagent.always_visible_tools)


async def test_subagent_tool_allowlist_hides_and_rejects_unassigned_tools(
    tmp_path: Path,
) -> None:
    manager = ToolContextManager(await _registry(tmp_path, 10))
    await manager.restrict_tools(
        ["search_tools", "get_tool", "run_registered_tool", "netexec-smb"],
        run_id="run-1",
        session_id="subagent-session",
        agent_id="subagent:recon",
    )
    await manager.load_tool(
        "netexec-smb",
        run_id="run-1",
        session_id="subagent-session",
        agent_id="subagent:recon",
    )

    visibility = await manager.visibility(
        run_id="run-1",
        session_id="subagent-session",
        agent_id="subagent:recon",
    )
    search = await manager.search_tools(
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
        await manager.load_tool(
            "tool-001",
            run_id="run-1",
            session_id="subagent-session",
            agent_id="subagent:recon",
        )


async def test_registry_hot_reload_marks_selection_stale_until_explicit_reload(
    tmp_path: Path,
) -> None:
    registry = await _registry(tmp_path, 10)
    manager = ToolContextManager(registry)
    first = await manager.load_tool(
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
    visibility = await manager.visibility(
        run_id="run-1", session_id="session-1", agent_id="primary"
    )

    assert second.generation == first.generation + 1
    assert second.full_schema["description"] == "Reloaded full schema."
    assert "new-tool" in visibility.hidden_available_tools
    assert visibility.dynamically_loaded_tools == []
    assert visibility.stale_loaded_tools == ["netexec-smb"]
    selected = await manager.get_tool(
        "netexec-smb",
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
    )
    assert selected.full_schema["description"] != "Reloaded full schema."
    assert selected.stale is True
    with pytest.raises(ToolUnavailableError, match="explicit reload"):
        await manager.assert_selected(
            "netexec-smb",
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
        )

    reloaded = await manager.reload_tool(
        "netexec-smb",
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
    )
    visibility = await manager.visibility(
        run_id="run-1", session_id="session-1", agent_id="primary"
    )
    assert reloaded.full_schema["description"] == "Reloaded full schema."
    assert visibility.dynamically_loaded_tools == ["netexec-smb"]
    assert visibility.stale_loaded_tools == []
