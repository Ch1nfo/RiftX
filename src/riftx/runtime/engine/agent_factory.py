"""Build an Agents SDK agent from one compiled Runtime Context."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from agents import Agent, FunctionTool

from riftx.runtime.lifecycle import CompiledContext

from .types import AgentEngineRequest

_CONTROL_TOOL_NAMES = {
    "search_tools",
    "list_tools",
    "get_tool",
    "get_execution",
    "wait_execution",
    "cancel_execution",
    "read_artifact",
    "complete_run",
}

DeferredToolHandler = Callable[[str, dict[str, object]], Awaitable[object]]


class DeferredRuntimeAgentFactory:
    """Expose execution tools without running commands inside the model process."""

    def __init__(self, handler: DeferredToolHandler | None = None) -> None:
        self._handler = handler or _deferred_marker

    def __call__(self, request: AgentEngineRequest) -> Agent[Any]:
        compiled = request.context
        schemas = compiled.available_tools if isinstance(compiled, CompiledContext) else []
        tools = [
            self._tool(schema)
            for schema in schemas
            if _is_deferred_execution_schema(schema)
        ]
        return Agent(
            name="RiftX Primary Runtime Agent",
            handoff_description="Plans the next authorized durable Runtime action.",
            instructions=(
                compiled.system_instructions
                if isinstance(compiled, CompiledContext)
                else "Follow the authorized RiftX Run contract."
            ),
            model=request.model,
            tools=tools,
            tool_use_behavior={"stop_at_tool_names": [tool.name for tool in tools]},
        )

    def _tool(self, schema: dict[str, object]) -> FunctionTool:
        name = str(schema["name"])
        description = str(schema.get("description") or f"Execute {name} through RiftX.")
        parameters = schema.get("parameters")
        params_json_schema = parameters if isinstance(parameters, dict) else {"type": "object"}

        async def invoke(_context: object, arguments_json: str) -> object:
            arguments = json.loads(arguments_json or "{}")
            if not isinstance(arguments, dict):
                raise ValueError(f"Tool {name!r} arguments must be a JSON object")
            return await self._handler(name, arguments)

        return FunctionTool(
            name=name,
            description=description,
            params_json_schema=params_json_schema,
            on_invoke_tool=invoke,
            strict_json_schema=False,
        )


async def _deferred_marker(name: str, arguments: dict[str, object]) -> object:
    return {
        "status": "deferred_to_riftx_execution_service",
        "tool_id": name,
        "arguments": arguments,
    }


def _is_deferred_execution_schema(schema: dict[str, object]) -> bool:
    name = schema.get("name")
    if name in {"run_registered_tool", "run_shell"}:
        return True
    if not isinstance(name, str) or not name or name in _CONTROL_TOOL_NAMES:
        return False
    metadata = schema.get("x-riftx")
    return isinstance(metadata, dict) and metadata.get("execution_type") in {
        "process",
        "shell",
    }
