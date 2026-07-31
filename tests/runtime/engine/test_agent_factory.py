from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from riftx.runtime.engine import AgentEngineRequest, DeferredRuntimeAgentFactory
from riftx.runtime.lifecycle import CompiledContext


async def test_factory_exposes_control_and_deferred_tools_with_distinct_yield_behavior() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    control_calls: list[tuple[object, str, dict[str, object], str]] = []

    async def handler(name: str, arguments: dict[str, object]) -> object:
        calls.append((name, arguments))
        return {"deferred": True}

    async def control_handler(
        scope: object,
        name: str,
        arguments: dict[str, object],
        call_id: str,
    ) -> object:
        control_calls.append((scope, name, arguments, call_id))
        return {"control": True}

    context = CompiledContext(
        system_instructions="Stay inside scope.",
        context_manifest={
            "run_id": "run-1",
            "session_id": "session-1",
            "agent_id": "primary",
            "execution_policy": "open",
            "dynamically_loaded_tools": ["scanner", "interactive"],
        },
        available_tools=[
            {
                "type": "function",
                "name": "search_tools",
                "description": "Search",
                "parameters": {"type": "object"},
                "x-riftx": {"resident": True},
            },
            {
                "type": "function",
                "name": "delegate",
                "description": "Delegate",
                "parameters": {"type": "object"},
                "x-riftx": {"resident": True},
            },
            {
                "type": "function",
                "name": "complete_run",
                "description": "Complete",
                "parameters": {"type": "object"},
                "x-riftx": {"resident": True},
            },
            {
                "type": "function",
                "name": "run_shell",
                "description": "Shell",
                "parameters": {"type": "object"},
                "x-riftx": {"resident": True, "execution_policy": "open"},
            },
            {
                "type": "function",
                "name": "scanner",
                "description": "Scan",
                "parameters": {"type": "object"},
                "x-riftx": {
                    "tool_id": "scanner",
                    "execution_type": "process",
                    "approval_level": "sensitive",
                },
            },
            {
                "type": "function",
                "name": "interactive",
                "description": "PTY",
                "parameters": {"type": "object"},
                "x-riftx": {
                    "tool_id": "interactive",
                    "execution_type": "pty",
                    "approval_level": "always",
                },
            },
        ],
    )

    agent = DeferredRuntimeAgentFactory(handler, control_handler=control_handler)(
        AgentEngineRequest(
            session_id="session-1",
            model="test-model",
            context=context,
        )
    )

    assert [tool.name for tool in agent.tools] == [
        "search_tools",
        "delegate",
        "complete_run",
        "run_shell",
        "scanner",
        "interactive",
    ]
    assert agent.tool_use_behavior == {
        "stop_at_tool_names": [
            "delegate",
            "complete_run",
            "run_shell",
            "scanner",
            "interactive",
        ]
    }
    assert agent.model_settings.parallel_tool_calls is False
    control_result = await agent.tools[0].on_invoke_tool(
        SimpleNamespace(tool_call_id="control-call-1"),
        json.dumps({"query": "scanner"}),
    )
    completion_result = await agent.tools[2].on_invoke_tool(
        SimpleNamespace(tool_call_id="completion-call-1"),
        json.dumps({"run_summary": "done"}),
    )
    result = await agent.tools[4].on_invoke_tool(None, json.dumps({"args": ["-sV"]}))  # type: ignore[arg-type]
    assert control_result == {"control": True}
    assert control_calls[0][1:] == (
        "search_tools",
        {"query": "scanner"},
        "control-call-1",
    )
    assert completion_result == {"control": True}
    assert control_calls[1][1:] == (
        "complete_run",
        {"run_summary": "done"},
        "completion-call-1",
    )
    assert result == {"deferred": True}
    assert calls == [("scanner", {"args": ["-sV"]})]


async def test_factory_rejects_stale_shell_for_registered_only_context() -> None:
    context = CompiledContext(
        system_instructions="Stay inside scope.",
        context_manifest={
            "execution_policy": "registered_only",
            "dynamically_loaded_tools": ["scanner"],
        },
        available_tools=[
            {
                "type": "function",
                "name": "run_shell",
                "description": "Stale shell schema",
                "parameters": {"type": "object"},
                "x-riftx": {"resident": True, "execution_policy": "open"},
            },
            {
                "type": "function",
                "name": "scanner",
                "description": "Registered scanner",
                "parameters": {"type": "object"},
                "x-riftx": {
                    "tool_id": "scanner",
                    "execution_type": "process",
                    "approval_level": "never",
                },
            },
        ],
    )

    with pytest.raises(RuntimeError, match="invalid_shell=\\['run_shell'\\]"):
        DeferredRuntimeAgentFactory()(
            AgentEngineRequest(
                session_id="session-1",
                model="test-model",
                context=context,
            )
        )


async def test_factory_requires_explicit_open_policy_for_shell_schema() -> None:
    context = CompiledContext(
        system_instructions="Stay inside scope.",
        available_tools=[
            {
                "type": "function",
                "name": "run_shell",
                "description": "Untrusted shell schema",
                "parameters": {"type": "object"},
                "x-riftx": {"resident": True, "execution_policy": "open"},
            }
        ],
    )

    with pytest.raises(RuntimeError, match="invalid_shell=\\['run_shell'\\]"):
        DeferredRuntimeAgentFactory()(
            AgentEngineRequest(
                session_id="session-1",
                model="test-model",
                context=context,
            )
        )


async def test_factory_rejects_unbound_control_tools() -> None:
    context = CompiledContext(
        system_instructions="Stay inside scope.",
        context_manifest={
            "run_id": "run-1",
            "session_id": "session-1",
            "agent_id": "primary",
            "execution_policy": "registered_only",
        },
        available_tools=[
            {
                "type": "function",
                "name": "search_tools",
                "parameters": {"type": "object"},
                "x-riftx": {
                    "resident": True,
                    "execution_policy": "registered_only",
                },
            }
        ],
    )

    with pytest.raises(RuntimeError, match="control tool handler is required"):
        DeferredRuntimeAgentFactory()(
            AgentEngineRequest(
                session_id="session-1",
                model="test-model",
                context=context,
            )
        )
