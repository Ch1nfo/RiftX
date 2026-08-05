"""OpenAI Agents SDK adapter behind the provider-neutral Engine contract."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, is_dataclass
from typing import Any, cast

from agents import RunConfig, Runner, RunState

from .errors import InvalidProviderStateError, ProviderStateSerializationError
from .types import (
    AgentEngineEvent,
    AgentEngineEventType,
    AgentEngineRequest,
    AgentEngineResumeRequest,
    AgentEngineRun,
    AgentEngineState,
)

ENGINE_TYPE = "openai-agents"
ENGINE_VERSION = "0.19"

AgentFactory = Callable[[AgentEngineRequest], Any]
StreamRunner = Callable[..., Awaitable[Any] | Any]


class OpenAIAgentsEngine:
    """Translate Agents SDK runs and stream events into RiftX-owned types."""

    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        provider: str = "openai",
        model_provider: Any | None = None,
        stream_runner: StreamRunner | None = None,
    ) -> None:
        self._agent_factory = agent_factory
        self._provider = provider
        self._model_provider = model_provider
        self._stream_runner = stream_runner or Runner.run_streamed

    async def start(self, request: AgentEngineRequest) -> AgentEngineRun:
        agent = self._agent_factory(request)
        _apply_compiled_instructions(agent, request.context)
        result = self._stream_runner(
            agent,
            _agents_input(request.engine_input()),
            context=request.context,
            max_turns=request.max_turns,
            run_config=self._run_config(),
        )
        if hasattr(result, "__await__"):
            result = await result
        return OpenAIAgentsEngineRun(
            result,
            provider=self._provider,
            model=request.model,
            tool_schemas=_tool_schemas(request.context),
        )

    async def resume(self, request: AgentEngineResumeRequest) -> AgentEngineRun:
        if request.state.engine_type != ENGINE_TYPE:
            raise InvalidProviderStateError(
                f"cannot resume {request.state.engine_type!r} state with {ENGINE_TYPE!r}"
            )
        if request.state.engine_version != ENGINE_VERSION:
            raise InvalidProviderStateError(
                f"unsupported {ENGINE_TYPE!r} state version {request.state.engine_version!r}"
            )
        if request.state.sdk_run_state is None:
            raise InvalidProviderStateError("provider state does not contain sdk_run_state")
        agent = self._agent_factory(request)
        _apply_compiled_instructions(agent, request.context)
        try:
            state = await RunState.from_json(
                agent,
                request.state.sdk_run_state,
                context_override=request.context,
                strict_context=request.context is not None,
            )
            _apply_sdk_approval_decisions(state, request.input_items)
        except Exception as exc:
            raise InvalidProviderStateError(
                f"could not deserialize OpenAI Agents state: {type(exc).__name__}: {exc}"
            ) from exc
        result = self._stream_runner(
            agent,
            state,
            max_turns=request.max_turns,
            run_config=self._run_config(),
        )
        if hasattr(result, "__await__"):
            result = await result
        return OpenAIAgentsEngineRun(
            result,
            provider=self._provider,
            model=request.model,
            previous_response_id=request.state.previous_response_id,
            tool_schemas=_tool_schemas(request.context),
        )

    def _run_config(self) -> RunConfig:
        if self._model_provider is None:
            return RunConfig(tracing_disabled=True, workflow_name="RiftX Agent Engine")
        return RunConfig(
            model_provider=self._model_provider,
            tracing_disabled=True,
            workflow_name="RiftX Agent Engine",
        )


def _apply_compiled_instructions(agent: object, context: object | None) -> None:
    instructions = getattr(context, "system_instructions", None)
    if isinstance(instructions, str) and instructions and hasattr(agent, "instructions"):
        cast(Any, agent).instructions = instructions


_MESSAGE_ROLES = frozenset({"user", "assistant", "system", "developer"})
_CONTEXT_METADATA_KEYS = frozenset(
    {
        "compressible",
        "context_item_id",
        "id",
        "priority",
        "relevance",
        "removable",
        "required",
        "source_event_id",
        "source_refs",
    }
)
_RIFTX_CONTEXT_TYPES = frozenset(
    {
        "approval_decision",
        "context_checkpoint",
        "current_input",
        "execution_completion",
        "hook_context",
        "latest_checkpoint",
        "memory",
        "recent_conversation",
        "relevant_tool_results",
        "retrieved_memory",
        "subagent_result",
        "subagent_results",
        "tool_result",
        "working_memory",
        "working_memory_snapshot",
    }
)


def _agents_input(
    value: str | list[dict[str, object]],
) -> str | list[dict[str, object]]:
    """Remove RiftX-only metadata before crossing the Agents SDK boundary.

    Compiled Context Items retain provenance and budgeting metadata for durable
    audit. The Agents SDK accepts only provider input-item shapes, however, and
    the Chat Completions converter deliberately rejects message dictionaries
    with extra keys. Non-message Context Items are therefore rendered as an
    explicit user-visible context block instead of being passed as an invented
    provider item type.
    """

    if isinstance(value, str):
        return value
    return [_agents_input_item(item) for item in value]


def _agents_input_item(item: dict[str, object]) -> dict[str, object]:
    role = item.get("role")
    if isinstance(role, str) and role in _MESSAGE_ROLES and "content" in item:
        return {
            "role": role,
            "content": _agents_text(item.get("content")),
        }

    item_type = item.get("type")
    if item_type == "function_call_output":
        call_id = item.get("call_id") or item.get("tool_call_id")
        if isinstance(call_id, str) and call_id:
            return {
                "type": "function_call_output",
                "call_id": call_id,
                "output": _agents_text(item.get("output", item.get("content"))),
            }
        raise ValueError("function_call_output input requires a non-empty call_id")

    if item_type == "function_call":
        call_id = item.get("call_id")
        name = item.get("name")
        arguments = item.get("arguments")
        if (
            isinstance(call_id, str)
            and call_id
            and isinstance(name, str)
            and name
            and isinstance(arguments, str)
        ):
            return {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
        raise ValueError("function_call input requires call_id, name, and string arguments")

    if item_type not in _RIFTX_CONTEXT_TYPES and role != "tool":
        raise ValueError(f"unsupported model input item type: {item_type!r}")

    context_payload = {
        key: value for key, value in item.items() if key not in _CONTEXT_METADATA_KEYS
    }
    return {
        "role": "user",
        "content": "[RiftX context]\n" + _agents_text(context_payload),
    }


def _agents_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class OpenAIAgentsEngineRun:
    def __init__(
        self,
        result: Any,
        *,
        provider: str,
        model: str,
        previous_response_id: str | None = None,
        tool_schemas: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self._result = result
        self._provider = provider
        self._model = model
        self._previous_response_id = previous_response_id
        self._tool_schemas = tool_schemas or {}
        self._events_started = False
        self._cancelled = False

    async def events(self) -> AsyncIterator[AgentEngineEvent]:
        if self._events_started:
            raise RuntimeError("engine events can only be consumed once")
        self._events_started = True
        sequence = 1
        yield AgentEngineEvent(
            sequence=sequence,
            event_type=AgentEngineEventType.RUN_STARTED,
            data={"provider": self._provider, "model": self._model},
        )
        sequence += 1
        failed = False
        ready_call_ids: set[str] = set()
        try:
            async for sdk_event in self._result.stream_events():
                for event_type, data in _translate_sdk_event(
                    sdk_event,
                    tool_schemas=self._tool_schemas,
                ):
                    if event_type is AgentEngineEventType.TOOL_CALL_READY:
                        call_id = data.get("call_id")
                        if isinstance(call_id, str) and call_id:
                            ready_call_ids.add(call_id)
                    yield AgentEngineEvent(
                        sequence=sequence,
                        event_type=event_type,
                        data=data,
                    )
                    sequence += 1
        except Exception as exc:
            failed = True
            yield AgentEngineEvent(
                sequence=sequence,
                event_type=AgentEngineEventType.ERROR,
                data={"error_type": type(exc).__name__, "message": str(exc)},
            )
            sequence += 1

        if not failed:
            for interruption in getattr(self._result, "interruptions", []) or []:
                payload = _approval_item_payload(
                    interruption,
                    tool_schemas=self._tool_schemas,
                )
                call_id = payload.get("call_id")
                if isinstance(call_id, str) and call_id in ready_call_ids:
                    continue
                yield AgentEngineEvent(
                    sequence=sequence,
                    event_type=AgentEngineEventType.TOOL_CALL_READY,
                    data=payload,
                )
                sequence += 1

        if not failed and getattr(self._result, "final_output", None) is not None:
            yield AgentEngineEvent(
                sequence=sequence,
                event_type=AgentEngineEventType.FINAL_OUTPUT,
                data={"output": _json_value(self._result.final_output)},
            )
            sequence += 1
        usage = _collect_usage(getattr(self._result, "raw_responses", []))
        if usage:
            yield AgentEngineEvent(
                sequence=sequence,
                event_type=AgentEngineEventType.USAGE,
                data=usage,
            )
            sequence += 1
        yield AgentEngineEvent(
            sequence=sequence,
            event_type=AgentEngineEventType.RUN_COMPLETED,
            data={
                "status": "failed" if failed else "cancelled" if self._cancelled else "completed"
            },
        )

    async def suspend(self) -> AgentEngineState:
        self._result.cancel(mode="after_turn")
        return self._serialize_state()

    async def cancel(self) -> None:
        self._cancelled = True
        self._result.cancel(mode="immediate")

    def _serialize_state(self) -> AgentEngineState:
        try:
            sdk_state = self._result.to_state().to_json(strict_context=False)
        except Exception as exc:
            raise ProviderStateSerializationError(
                f"could not serialize OpenAI Agents state: {type(exc).__name__}: {exc}"
            ) from exc
        previous_response_id = getattr(self._result, "_previous_response_id", None)
        return AgentEngineState(
            engine_type=ENGINE_TYPE,
            engine_version=ENGINE_VERSION,
            provider=self._provider,
            model=self._model,
            serialized_state=sdk_state,
            sdk_run_state=sdk_state,
            previous_response_id=previous_response_id or self._previous_response_id,
        )


def _translate_sdk_event(
    event: Any,
    *,
    tool_schemas: dict[str, dict[str, object]] | None = None,
) -> list[tuple[AgentEngineEventType, dict[str, object]]]:
    event_kind = getattr(event, "type", None)
    if event_kind == "raw_response_event":
        data = getattr(event, "data", None)
        raw_type = getattr(data, "type", None)
        if raw_type == "response.output_text.delta":
            return [(AgentEngineEventType.ASSISTANT_DELTA, {"delta": getattr(data, "delta", "")})]
        if raw_type == "response.function_call_arguments.delta":
            return [
                (
                    AgentEngineEventType.TOOL_CALL_ARGUMENT_DELTA,
                    {
                        "call_id": getattr(data, "call_id", None) or getattr(data, "item_id", None),
                        "delta": getattr(data, "delta", ""),
                    },
                )
            ]
        if raw_type == "response.output_item.added":
            item = getattr(data, "item", None)
            if getattr(item, "type", None) == "function_call":
                return [
                    (
                        AgentEngineEventType.TOOL_CALL_STARTED,
                        {
                            "call_id": getattr(item, "call_id", None),
                            "name": getattr(item, "name", None),
                        },
                    )
                ]
        if raw_type == "response.completed":
            return []
        return []
    if event_kind == "run_item_stream_event":
        name = getattr(event, "name", None)
        item = getattr(event, "item", None)
        payload = _json_dict(item)
        if name == "message_output_created":
            return [(AgentEngineEventType.ASSISTANT_MESSAGE, payload)]
        if name == "tool_called":
            tool_name = _tool_name(item)
            schema = (tool_schemas or {}).get(tool_name or "", {})
            payload = _tool_call_payload(
                item,
                payload,
                tool_name=tool_name,
                schema=schema,
            )
            event_type = (
                AgentEngineEventType.PLAN_UPDATE
                if tool_name == "update_plan"
                else AgentEngineEventType.SUBAGENT_REQUESTED
                if tool_name in {"delegate", "spawn_subagent"}
                else AgentEngineEventType.TOOL_CALL_STARTED
                if not _is_deferred_execution_schema(tool_name, schema)
                else AgentEngineEventType.TOOL_CALL_READY
            )
            return [(event_type, payload)]
        if name == "handoff_requested":
            return [(AgentEngineEventType.SUBAGENT_REQUESTED, payload)]
    return []


def _tool_name(item: Any) -> str | None:
    raw_item = getattr(item, "raw_item", None)
    return getattr(raw_item, "name", None) or getattr(item, "name", None)


def _tool_call_payload(
    item: Any,
    payload: dict[str, object],
    *,
    tool_name: str | None,
    schema: dict[str, object],
) -> dict[str, object]:
    if set(payload) == {"value"}:
        payload = {}
    raw_item = getattr(item, "raw_item", None)
    call_id = (
        getattr(raw_item, "call_id", None)
        or getattr(raw_item, "id", None)
        or getattr(item, "call_id", None)
    )
    arguments = getattr(raw_item, "arguments", None) or getattr(item, "arguments", None)
    if isinstance(call_id, str) and call_id:
        payload.setdefault("call_id", call_id)
    if isinstance(tool_name, str) and tool_name:
        payload.setdefault("tool_id", tool_name)
    if isinstance(arguments, dict | str):
        payload.setdefault("arguments", arguments)
    metadata = schema.get("x-riftx")
    if isinstance(metadata, dict):
        approval_level = metadata.get("approval_level")
        payload.setdefault("approval_level", approval_level or "sensitive")
        payload.setdefault("approval_required", approval_level != "never")
        approval_policy = metadata.get("approval_policy")
        if isinstance(approval_policy, str) and approval_policy:
            payload.setdefault("approval_policy", approval_policy)
    return payload


def _approval_item_payload(
    item: object,
    *,
    tool_schemas: dict[str, dict[str, object]],
) -> dict[str, object]:
    tool_name = getattr(item, "tool_name", None) or getattr(item, "name", None)
    call_id = getattr(item, "call_id", None)
    arguments = getattr(item, "arguments", None)
    payload: dict[str, object] = {}
    if isinstance(call_id, str) and call_id:
        payload["call_id"] = call_id
    if isinstance(tool_name, str) and tool_name:
        payload["tool_id"] = tool_name
    if isinstance(arguments, dict | str):
        payload["arguments"] = arguments
    schema = tool_schemas.get(tool_name, {}) if isinstance(tool_name, str) else {}
    metadata = schema.get("x-riftx")
    if isinstance(metadata, dict):
        approval_level = metadata.get("approval_level")
        payload["approval_level"] = approval_level or "sensitive"
        payload["approval_required"] = True
        approval_policy = metadata.get("approval_policy")
        if isinstance(approval_policy, str) and approval_policy:
            payload["approval_policy"] = approval_policy
    else:
        payload["approval_level"] = "sensitive"
        payload["approval_required"] = True
    return payload


def _apply_sdk_approval_decisions(
    state: RunState[Any],
    input_items: list[dict[str, object]],
) -> None:
    decisions: dict[str, tuple[str, str | None]] = {}
    for item in input_items:
        if item.get("type") != "approval_decision":
            continue
        call_id = item.get("engine_call_id")
        decision = item.get("decision")
        if not isinstance(call_id, str) or not call_id:
            continue
        if not isinstance(decision, str) or not decision:
            raise ValueError("SDK approval decision is missing its decision")
        if call_id in decisions:
            raise ValueError(f"duplicate SDK approval decision for call {call_id!r}")
        feedback = item.get("feedback")
        decisions[call_id] = (
            decision,
            feedback if isinstance(feedback, str) and feedback else None,
        )
    if not decisions:
        return

    interruptions = list(state.get_interruptions())
    by_call_id: dict[str, object] = {}
    for interruption in interruptions:
        call_id = getattr(interruption, "call_id", None)
        if not isinstance(call_id, str) or not call_id:
            continue
        if call_id in by_call_id:
            raise ValueError(f"duplicate SDK interruption for call {call_id!r}")
        by_call_id[call_id] = interruption

    for call_id, (decision, feedback) in decisions.items():
        interruption = by_call_id.get(call_id)
        if interruption is None:
            raise ValueError(f"SDK approval call {call_id!r} is not pending")
        if decision in {"approve_once", "approve_tool_for_run"}:
            state.approve(interruption, always_approve=False)  # type: ignore[arg-type]
        elif decision in {"reject", "reject_with_feedback"}:
            state.reject(  # type: ignore[arg-type]
                interruption,
                always_reject=False,
                rejection_message=feedback,
            )
        else:
            raise ValueError(f"unsupported SDK approval decision {decision!r}")


def _tool_schemas(context: object | None) -> dict[str, dict[str, object]]:
    schemas = getattr(context, "available_tools", None)
    if not isinstance(schemas, list):
        return {}
    return {
        str(schema["name"]): schema
        for schema in schemas
        if isinstance(schema, dict) and isinstance(schema.get("name"), str)
    }


def _is_deferred_execution_schema(
    tool_name: str | None,
    schema: dict[str, object],
) -> bool:
    if tool_name in {"run_registered_tool", "run_shell"}:
        return True
    if not schema:
        return tool_name not in {
            "search_tools",
            "list_tools",
            "get_tool",
            "search_skills",
            "list_skills",
            "load_skill",
            "load_skill_references",
            "unload_skill",
            "get_execution",
            "wait_execution",
            "cancel_execution",
            "read_artifact",
            "complete_run",
        }
    metadata = schema.get("x-riftx")
    return isinstance(metadata, dict) and metadata.get("execution_type") in {
        "process",
        "shell",
        "pty",
    }


def _collect_usage(responses: list[Any]) -> dict[str, object]:
    totals: dict[str, int] = {}
    for response in responses:
        usage = getattr(response, "usage", None)
        if usage is None:
            continue
        payload = _json_dict(usage)
        for key, value in payload.items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    return totals


def _json_dict(value: Any) -> dict[str, object]:
    converted = _json_value(value)
    return converted if isinstance(converted, dict) else {"value": converted}


def _json_value(value: Any) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return _json_value(asdict(value))
    return str(value)
