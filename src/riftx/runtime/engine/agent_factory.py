"""Build an Agents SDK agent from one compiled Runtime Context."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agents import Agent, FunctionTool, ModelSettings

from riftx.runtime.lifecycle import CompiledContext
from riftx.tools.policy import validate_runtime_tool_inventory

from .types import AgentEngineRequest

_CONTROL_TOOL_NAMES = {
    "search_tools",
    "list_tools",
    "get_tool",
    "reload_tool",
    "unload_tool",
    "search_mcp_tools",
    "get_mcp_tool",
    "call_mcp_tool",
    "search_skills",
    "list_skills",
    "load_skill",
    "load_skill_references",
    "reload_skill",
    "unload_skill",
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
    "get_execution",
    "wait_execution",
    "cancel_execution",
    "read_artifact",
    "complete_run",
}

_TERMINAL_CONTROL_TOOL_NAMES = {"complete_run"}

DeferredToolHandler = Callable[[str, dict[str, object]], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class RuntimeToolScope:
    run_id: str
    session_id: str
    agent_id: str
    model_profile: str


ControlToolHandler = Callable[[RuntimeToolScope, str, dict[str, object], str], Awaitable[object]]


class DeferredRuntimeAgentFactory:
    """Expose execution tools without running commands inside the model process."""

    def __init__(
        self,
        handler: DeferredToolHandler | None = None,
        *,
        control_handler: ControlToolHandler | None = None,
    ) -> None:
        self._handler = handler or _deferred_marker
        self._control_handler = control_handler

    def __call__(self, request: AgentEngineRequest) -> Agent[Any]:
        compiled = request.context
        schemas = compiled.available_tools if isinstance(compiled, CompiledContext) else []
        execution_policy = _compiled_execution_policy(compiled)
        manifest = compiled.context_manifest if isinstance(compiled, CompiledContext) else {}
        validate_runtime_tool_inventory(schemas, context_manifest=manifest)
        selected_schemas = [
            schema
            for schema in schemas
            if _is_model_visible_schema(schema, execution_policy=execution_policy)
        ]
        control_names = {
            str(schema.get("name"))
            for schema in selected_schemas
            if schema.get("name") in _CONTROL_TOOL_NAMES
        }
        if control_names and self._control_handler is None:
            raise RuntimeError(
                "Runtime control tool handler is required for model-visible tools: "
                f"{sorted(control_names)}"
            )
        scope = _runtime_tool_scope(request) if control_names else None
        tools = [self._tool(schema, scope=scope) for schema in selected_schemas]
        stop_names = [
            tool.name
            for tool in tools
            if tool.name not in _CONTROL_TOOL_NAMES or tool.name in _TERMINAL_CONTROL_TOOL_NAMES
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
            model_settings=ModelSettings(parallel_tool_calls=False),
            tools=tools,
            tool_use_behavior={"stop_at_tool_names": stop_names},
        )

    def _tool(
        self,
        schema: dict[str, object],
        *,
        scope: RuntimeToolScope | None,
    ) -> FunctionTool:
        name = str(schema["name"])
        description = str(schema.get("description") or f"Execute {name} through RiftX.")
        parameters = schema.get("parameters")
        params_json_schema = parameters if isinstance(parameters, dict) else {"type": "object"}
        is_control = name in _CONTROL_TOOL_NAMES
        explicit_approval = _requires_explicit_approval(schema)

        async def invoke(context: object, arguments_json: str) -> object:
            arguments = json.loads(arguments_json or "{}")
            if not isinstance(arguments, dict):
                raise ValueError(f"Tool {name!r} arguments must be a JSON object")
            if is_control:
                if self._control_handler is None or scope is None:
                    raise RuntimeError(f"Runtime control tool {name!r} is not bound")
                call_id = getattr(context, "tool_call_id", None)
                if not isinstance(call_id, str) or not call_id:
                    raise RuntimeError(f"Runtime control tool {name!r} has no call ID")
                return await self._control_handler(scope, name, arguments, call_id)
            return await self._handler(name, arguments)

        return FunctionTool(
            name=name,
            description=description,
            params_json_schema=params_json_schema,
            on_invoke_tool=invoke,
            strict_json_schema=False,
            needs_approval=explicit_approval,
            _use_default_failure_error_function=not is_control,
        )


async def _deferred_marker(name: str, arguments: dict[str, object]) -> object:
    return {
        "status": "deferred_to_riftx_execution_service",
        "tool_id": name,
        "arguments": arguments,
    }


def _compiled_execution_policy(compiled: object) -> str:
    if not isinstance(compiled, CompiledContext):
        return "registered_only"
    policy = compiled.context_manifest.get("execution_policy")
    return str(policy) if policy is not None else "registered_only"


def _is_model_visible_schema(
    schema: dict[str, object],
    *,
    execution_policy: str,
) -> bool:
    name = schema.get("name")
    if name in _CONTROL_TOOL_NAMES:
        return True
    if name == "run_shell":
        metadata = schema.get("x-riftx")
        return (
            execution_policy == "open"
            and isinstance(metadata, dict)
            and metadata.get("execution_policy") == "open"
        )
    if name in {"run_registered_tool", "delegate"}:
        return True
    if not isinstance(name, str) or not name:
        return False
    metadata = schema.get("x-riftx")
    return isinstance(metadata, dict) and metadata.get("execution_type") in {
        "process",
        "shell",
        "pty",
    }


def _requires_explicit_approval(schema: dict[str, object]) -> bool:
    metadata = schema.get("x-riftx")
    return isinstance(metadata, dict) and metadata.get("approval_policy") == "explicit"


def _runtime_tool_scope(request: AgentEngineRequest) -> RuntimeToolScope:
    compiled = request.context
    if not isinstance(compiled, CompiledContext):
        raise RuntimeError("Runtime control tools require one compiled Context")
    manifest = compiled.context_manifest
    run_id = manifest.get("run_id")
    session_id = manifest.get("session_id")
    agent_id = manifest.get("agent_id")
    if (
        not isinstance(run_id, str)
        or not run_id
        or session_id != request.session_id
        or not isinstance(agent_id, str)
        or not agent_id
    ):
        raise RuntimeError("Runtime control tools require an immutable Run/Session/Agent scope")
    return RuntimeToolScope(
        run_id=run_id,
        session_id=request.session_id,
        agent_id=agent_id,
        model_profile=request.model,
    )
