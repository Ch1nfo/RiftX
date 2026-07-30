from __future__ import annotations

import json

from riftx.runtime.engine import AgentEngineRequest, DeferredRuntimeAgentFactory
from riftx.runtime.lifecycle import CompiledContext


async def test_factory_exposes_only_deferred_execution_tools() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def handler(name: str, arguments: dict[str, object]) -> object:
        calls.append((name, arguments))
        return {"deferred": True}

    context = CompiledContext(
        system_instructions="Stay inside scope.",
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
                "name": "run_shell",
                "description": "Shell",
                "parameters": {"type": "object"},
                "x-riftx": {"resident": True},
            },
            {
                "type": "function",
                "name": "scanner",
                "description": "Scan",
                "parameters": {"type": "object"},
                "x-riftx": {"execution_type": "process"},
            },
            {
                "type": "function",
                "name": "interactive",
                "description": "PTY",
                "parameters": {"type": "object"},
                "x-riftx": {"execution_type": "pty"},
            },
        ],
    )

    agent = DeferredRuntimeAgentFactory(handler)(
        AgentEngineRequest(
            session_id="session-1",
            model="test-model",
            context=context,
        )
    )

    assert [tool.name for tool in agent.tools] == [
        "delegate",
        "run_shell",
        "scanner",
        "interactive",
    ]
    assert agent.tool_use_behavior == {
        "stop_at_tool_names": ["delegate", "run_shell", "scanner", "interactive"]
    }
    result = await agent.tools[2].on_invoke_tool(None, json.dumps({"args": ["-sV"]}))  # type: ignore[arg-type]
    assert result == {"deferred": True}
    assert calls == [("scanner", {"args": ["-sV"]})]
