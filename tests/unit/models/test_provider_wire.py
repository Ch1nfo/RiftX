from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from agents import (
    Model,
    ModelSettings,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    function_tool,
)
from agents.models.interface import ModelTracing
from openai import AsyncOpenAI

import riftx.models.provider as provider_module
from riftx.models import ModelAPI, ModelProfile, ModelsConfig, RiftXModelProvider
from riftx.runtime.engine import (
    AgentEngineEventType,
    AgentEngineRequest,
    DeferredRuntimeAgentFactory,
    OpenAIAgentsEngine,
)
from riftx.runtime.lifecycle import CompiledContext


@function_tool
def inspect_target(host: str) -> str:
    """Inspect a target host."""

    return host


def _provider_with_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: ModelProfile,
    response_body: str | list[str],
) -> tuple[RiftXModelProvider, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    response_bodies = [response_body] if isinstance(response_body, str) else response_body

    async def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response_index = min(len(requests) - 1, len(response_bodies) - 1)
        return httpx.Response(
            status_code=200,
            headers={
                "content-type": "text/event-stream",
                "x-request-id": "wire-request-id",
            },
            content=response_bodies[response_index],
        )

    transport = httpx.MockTransport(handle_request)
    real_async_openai = provider_module.AsyncOpenAI

    def build_client(**kwargs: Any) -> AsyncOpenAI:
        kwargs["http_client"] = httpx.AsyncClient(transport=transport)
        return real_async_openai(**kwargs)

    monkeypatch.setattr(provider_module, "AsyncOpenAI", build_client)
    config = ModelsConfig(default_profile="primary", models={"primary": profile})
    return RiftXModelProvider(config, environment={}), requests


async def _stream(model: Model) -> list[Any]:
    stream: AsyncIterator[Any] = model.stream_response(
        system_instructions="Stay within the supplied target boundary.",
        input="Inspect example.com",
        model_settings=ModelSettings(),
        tools=[inspect_target],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )
    return [event async for event in stream]


def _chat_chunk(
    delta: dict[str, Any],
    *,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-wire",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "wire-chat-model",
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def _chat_completions_sse(
    *,
    tool_name: str = "inspect_target",
    arguments: str = '{"host":"example.com"}',
) -> str:
    split_at = max(1, len(arguments) // 2)
    chunks = [
        _chat_chunk({"role": "assistant", "content": "Ready "}),
        _chat_chunk({"content": "now."}),
        _chat_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-wire",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": arguments[:split_at],
                        },
                    }
                ]
            }
        ),
        _chat_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "function": {"arguments": arguments[split_at:]},
                    }
                ]
            }
        ),
        _chat_chunk({}, finish_reason="tool_calls"),
        {
            "id": "chatcmpl-wire",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "wire-chat-model",
            "choices": [],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 4,
                "total_tokens": 12,
            },
        },
    ]
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def _chat_text_sse() -> str:
    chunks = [
        _chat_chunk({"role": "assistant", "content": "RIFTX_ACCEPTANCE_OK"}),
        _chat_chunk({}, finish_reason="stop"),
        {
            "id": "chatcmpl-wire",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "wire-chat-model",
            "choices": [],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 4,
                "total_tokens": 12,
            },
        },
    ]
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def _response_snapshot(
    output: list[dict[str, Any]],
    *,
    status: str,
) -> dict[str, Any]:
    return {
        "id": "resp-wire",
        "created_at": 1,
        "model": "wire-responses-model",
        "object": "response",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "status": status,
    }


def _responses_sse(
    *,
    tool_name: str = "inspect_target",
    arguments: str = '{"host":"example.com"}',
) -> str:
    split_at = max(1, len(arguments) // 2)
    message_in_progress = {
        "id": "msg-wire",
        "type": "message",
        "role": "assistant",
        "status": "in_progress",
        "content": [],
    }
    message_completed = {
        **message_in_progress,
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": "Ready now.",
                "annotations": [],
                "logprobs": [],
            }
        ],
    }
    call_in_progress = {
        "id": "fc-wire",
        "type": "function_call",
        "call_id": "call-wire",
        "name": tool_name,
        "arguments": "",
        "status": "in_progress",
    }
    call_completed = {
        **call_in_progress,
        "arguments": arguments,
        "status": "completed",
    }
    events = [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": _response_snapshot([], status="in_progress"),
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "output_index": 0,
            "item": message_in_progress,
        },
        {
            "type": "response.content_part.added",
            "sequence_number": 2,
            "output_index": 0,
            "item_id": "msg-wire",
            "content_index": 0,
            "part": {
                "type": "output_text",
                "text": "",
                "annotations": [],
                "logprobs": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 3,
            "output_index": 0,
            "item_id": "msg-wire",
            "content_index": 0,
            "delta": "Ready ",
            "logprobs": [],
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 4,
            "output_index": 0,
            "item_id": "msg-wire",
            "content_index": 0,
            "delta": "now.",
            "logprobs": [],
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 5,
            "output_index": 0,
            "item": message_completed,
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 6,
            "output_index": 1,
            "item": call_in_progress,
        },
        {
            "type": "response.function_call_arguments.delta",
            "sequence_number": 7,
            "output_index": 1,
            "item_id": "fc-wire",
            "delta": arguments[:split_at],
        },
        {
            "type": "response.function_call_arguments.delta",
            "sequence_number": 8,
            "output_index": 1,
            "item_id": "fc-wire",
            "delta": arguments[split_at:],
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 9,
            "output_index": 1,
            "item": call_completed,
        },
        {
            "type": "response.completed",
            "sequence_number": 10,
            "response": _response_snapshot(
                [message_completed, call_completed],
                status="completed",
            ),
        },
    ]
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def _responses_text_sse(text: str = "RIFTX_ACCEPTANCE_OK") -> str:
    message_in_progress = {
        "id": "msg-wire-final",
        "type": "message",
        "role": "assistant",
        "status": "in_progress",
        "content": [],
    }
    message_completed = {
        **message_in_progress,
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
                "logprobs": [],
            }
        ],
    }
    events = [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": _response_snapshot([], status="in_progress"),
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "output_index": 0,
            "item": message_in_progress,
        },
        {
            "type": "response.content_part.added",
            "sequence_number": 2,
            "output_index": 0,
            "item_id": "msg-wire-final",
            "content_index": 0,
            "part": {
                "type": "output_text",
                "text": "",
                "annotations": [],
                "logprobs": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 3,
            "output_index": 0,
            "item_id": "msg-wire-final",
            "content_index": 0,
            "delta": text,
            "logprobs": [],
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 4,
            "output_index": 0,
            "item": message_completed,
        },
        {
            "type": "response.completed",
            "sequence_number": 5,
            "response": _response_snapshot([message_completed], status="completed"),
        },
    ]
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def _assert_streamed_text_and_tool_call(events: list[Any]) -> None:
    text = "".join(event.delta for event in events if event.type == "response.output_text.delta")
    tool_call = next(
        event.item
        for event in events
        if event.type == "response.output_item.done"
        and getattr(event.item, "type", None) == "function_call"
    )

    assert text == "Ready now."
    assert tool_call.call_id == "call-wire"
    assert tool_call.name == "inspect_target"
    assert json.loads(tool_call.arguments) == {"host": "example.com"}


async def test_default_chat_completions_profile_uses_wire_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ModelProfile(
        model="wire-chat-model",
        base_url="http://wire.invalid/v1",
        requires_api_key=False,
        api_key_env=None,
    )
    provider, requests = _provider_with_mock_transport(
        monkeypatch,
        profile=profile,
        response_body=_chat_completions_sse(),
    )

    try:
        model = provider.get_model(None)
        events = await _stream(model)
    finally:
        await provider.aclose()

    assert profile.api is ModelAPI.CHAT_COMPLETIONS
    assert isinstance(model, OpenAIChatCompletionsModel)
    assert len(requests) == 1
    request = requests[0]
    payload = json.loads(request.content)
    assert request.method == "POST"
    assert request.url.path == "/v1/chat/completions"
    assert payload["model"] == "wire-chat-model"
    assert payload["stream"] is True
    assert payload["messages"] == [
        {"content": "Stay within the supplied target boundary.", "role": "system"},
        {"role": "user", "content": "Inspect example.com"},
    ]
    tool = payload["tools"][0]["function"]
    assert payload["tools"][0]["type"] == "function"
    assert tool["name"] == "inspect_target"
    assert tool["parameters"]["required"] == ["host"]
    assert tool["strict"] is True
    _assert_streamed_text_and_tool_call(events)


async def test_runtime_engine_strips_context_provenance_before_chat_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ModelProfile(
        model="wire-chat-model",
        base_url="http://wire.invalid/v1",
        requires_api_key=False,
        api_key_env=None,
    )
    provider, requests = _provider_with_mock_transport(
        monkeypatch,
        profile=profile,
        response_body=_chat_text_sse(),
    )
    compiled = CompiledContext(
        system_instructions="Stay within the supplied target boundary.",
        input_items=[
            {
                "role": "user",
                "content": "Reply exactly RIFTX_ACCEPTANCE_OK.",
                "source_event_id": "event-1",
                "source_refs": ["message://message-1"],
            }
        ],
    )
    engine = OpenAIAgentsEngine(
        DeferredRuntimeAgentFactory(),
        model_provider=provider,
    )

    try:
        run = await engine.start(
            AgentEngineRequest(
                session_id="session-1",
                model="primary",
                input_items=compiled.input_items,
                context=compiled,
            )
        )
        events = [event async for event in run.events()]
    finally:
        await provider.aclose()

    assert AgentEngineEventType.ERROR not in [event.event_type for event in events]
    assert (
        "".join(
            str(event.data.get("delta") or "")
            for event in events
            if event.event_type is AgentEngineEventType.ASSISTANT_DELTA
        )
        == "RIFTX_ACCEPTANCE_OK"
    )
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["messages"] == [
        {"content": "Stay within the supplied target boundary.", "role": "system"},
        {"role": "user", "content": "Reply exactly RIFTX_ACCEPTANCE_OK."},
    ]


async def test_runtime_engine_restarts_from_canonical_tool_result_on_chat_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ModelProfile(
        model="wire-chat-model",
        base_url="http://wire.invalid/v1",
        requires_api_key=False,
        api_key_env=None,
    )
    provider, requests = _provider_with_mock_transport(
        monkeypatch,
        profile=profile,
        response_body=[_chat_completions_sse(), _chat_text_sse()],
    )
    tool_schema = {
        "name": "inspect_target",
        "description": "Inspect one bounded target.",
        "parameters": {
            "type": "object",
            "properties": {"host": {"type": "string"}},
            "required": ["host"],
            "additionalProperties": False,
        },
        "x-riftx": {
            "tool_id": "inspect_target",
            "execution_type": "process",
            "approval_level": "never",
        },
    }
    engine = OpenAIAgentsEngine(
        DeferredRuntimeAgentFactory(),
        model_provider=provider,
    )

    try:
        first_context = CompiledContext(
            system_instructions="Stay within the supplied target boundary.",
            input_items=[{"role": "user", "content": "Inspect example.com."}],
            available_tools=[tool_schema],
            context_manifest={"dynamically_loaded_tools": ["inspect_target"]},
        )
        first_run = await engine.start(
            AgentEngineRequest(
                session_id="session-1",
                model="primary",
                input_items=first_context.input_items,
                context=first_context,
            )
        )
        first_events = [event async for event in first_run.events()]
        second_context = CompiledContext(
            system_instructions="Stay within the supplied target boundary.",
            input_items=[
                {"role": "user", "content": "Inspect example.com."},
                {
                    "type": "relevant_tool_results",
                    "content": {
                        "tool_id": "inspect_target",
                        "status": "exited",
                        "context_summary": "RIFTX_TOOL_OK",
                    },
                    "source_refs": ["artifact://execution-1/stdout"],
                    "context_item_id": "tool-result:execution-1",
                },
            ],
            available_tools=[tool_schema],
            context_manifest={"dynamically_loaded_tools": ["inspect_target"]},
        )
        second_run = await engine.start(
            AgentEngineRequest(
                session_id="session-1",
                model="primary",
                input_items=second_context.input_items,
                context=second_context,
            )
        )
        second_events = [event async for event in second_run.events()]
    finally:
        await provider.aclose()

    assert AgentEngineEventType.TOOL_CALL_READY in [event.event_type for event in first_events]
    assert AgentEngineEventType.ERROR not in [event.event_type for event in second_events]
    assert len(requests) == 2
    second_payload = json.loads(requests[1].content)
    serialized_messages = json.dumps(second_payload["messages"], ensure_ascii=False)
    assert "RIFTX_TOOL_OK" in serialized_messages
    assert "source_refs" not in serialized_messages
    assert "artifact://execution-1/stdout" not in serialized_messages


async def test_responses_profile_uses_wire_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ModelProfile(
        model="wire-responses-model",
        api=ModelAPI.RESPONSES,
        base_url="http://wire.invalid/v1",
        requires_api_key=False,
        api_key_env=None,
    )
    provider, requests = _provider_with_mock_transport(
        monkeypatch,
        profile=profile,
        response_body=_responses_sse(),
    )

    try:
        model = provider.get_model(None)
        events = await _stream(model)
    finally:
        await provider.aclose()

    assert isinstance(model, OpenAIResponsesModel)
    assert len(requests) == 1
    request = requests[0]
    payload = json.loads(request.content)
    assert request.method == "POST"
    assert request.url.path == "/v1/responses"
    assert payload["model"] == "wire-responses-model"
    assert payload["stream"] is True
    assert payload["instructions"] == "Stay within the supplied target boundary."
    assert payload["input"] == [{"content": "Inspect example.com", "role": "user"}]
    tool = payload["tools"][0]
    assert tool["type"] == "function"
    assert tool["name"] == "inspect_target"
    assert tool["parameters"]["required"] == ["host"]
    assert tool["strict"] is True
    _assert_streamed_text_and_tool_call(events)


@pytest.mark.parametrize("api", [ModelAPI.CHAT_COMPLETIONS, ModelAPI.RESPONSES])
async def test_inline_runtime_control_result_reaches_sdk_second_model_turn(
    monkeypatch: pytest.MonkeyPatch,
    api: ModelAPI,
) -> None:
    model_name = "wire-chat-model" if api is ModelAPI.CHAT_COMPLETIONS else "wire-responses-model"
    first_response = (
        _chat_completions_sse(
            tool_name="search_tools",
            arguments='{"query":"ports"}',
        )
        if api is ModelAPI.CHAT_COMPLETIONS
        else _responses_sse(
            tool_name="search_tools",
            arguments='{"query":"ports"}',
        )
    )
    second_response = (
        _chat_text_sse() if api is ModelAPI.CHAT_COMPLETIONS else _responses_text_sse()
    )
    provider, requests = _provider_with_mock_transport(
        monkeypatch,
        profile=ModelProfile(
            model=model_name,
            api=api,
            base_url="http://wire.invalid/v1",
            requires_api_key=False,
            api_key_env=None,
        ),
        response_body=[first_response, second_response],
    )
    control_calls: list[tuple[object, str, dict[str, object], str]] = []

    async def control_handler(
        scope: object,
        tool_name: str,
        arguments: dict[str, object],
        call_id: str,
    ) -> object:
        control_calls.append((scope, tool_name, arguments, call_id))
        return {"marker": "RIFTX_INLINE_CONTROL_OK", "matches": ["scanner"]}

    compiled = CompiledContext(
        system_instructions="Stay within the supplied target boundary.",
        input_items=[{"role": "user", "content": "Find a port-scanning tool."}],
        available_tools=[
            {
                "type": "function",
                "name": "search_tools",
                "description": "Search the authorized Tool Index.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "x-riftx": {
                    "resident": True,
                    "execution_policy": "registered_only",
                },
            }
        ],
        context_manifest={
            "run_id": "run-1",
            "session_id": "session-1",
            "agent_id": "primary",
            "execution_policy": "registered_only",
        },
    )
    engine = OpenAIAgentsEngine(
        DeferredRuntimeAgentFactory(control_handler=control_handler),
        model_provider=provider,
    )

    try:
        run = await engine.start(
            AgentEngineRequest(
                session_id="session-1",
                model="primary",
                input_items=compiled.input_items,
                context=compiled,
            )
        )
        events = [event async for event in run.events()]
    finally:
        await provider.aclose()

    assert AgentEngineEventType.ERROR not in [event.event_type for event in events]
    assert len(control_calls) == 1
    scope, tool_name, arguments, call_id = control_calls[0]
    assert (scope.run_id, scope.session_id, scope.agent_id) == (
        "run-1",
        "session-1",
        "primary",
    )
    assert (tool_name, arguments, call_id) == (
        "search_tools",
        {"query": "ports"},
        "call-wire",
    )
    assert len(requests) == 2
    assert "RIFTX_INLINE_CONTROL_OK" in requests[1].content.decode()
    assert any(
        event.event_type is AgentEngineEventType.ASSISTANT_DELTA
        and "RIFTX_ACCEPTANCE_OK" in str(event.data.get("delta"))
        for event in events
    )


@pytest.mark.parametrize("api", [ModelAPI.CHAT_COMPLETIONS, ModelAPI.RESPONSES])
async def test_complete_run_stops_sdk_without_another_model_request(
    monkeypatch: pytest.MonkeyPatch,
    api: ModelAPI,
) -> None:
    model_name = "wire-chat-model" if api is ModelAPI.CHAT_COMPLETIONS else "wire-responses-model"
    arguments = '{"run_summary":"authorized objective complete"}'
    first_response = (
        _chat_completions_sse(tool_name="complete_run", arguments=arguments)
        if api is ModelAPI.CHAT_COMPLETIONS
        else _responses_sse(tool_name="complete_run", arguments=arguments)
    )
    provider, requests = _provider_with_mock_transport(
        monkeypatch,
        profile=ModelProfile(
            model=model_name,
            api=api,
            base_url="http://wire.invalid/v1",
            requires_api_key=False,
            api_key_env=None,
        ),
        response_body=first_response,
    )
    control_calls: list[tuple[str, dict[str, object], str]] = []

    async def control_handler(
        _scope: object,
        tool_name: str,
        arguments: dict[str, object],
        call_id: str,
    ) -> object:
        control_calls.append((tool_name, arguments, call_id))
        return {"completion_requested": True}

    compiled = CompiledContext(
        system_instructions="Complete only after the authorized objective is satisfied.",
        input_items=[{"role": "user", "content": "Finish the authorized Run."}],
        available_tools=[
            {
                "type": "function",
                "name": "complete_run",
                "description": "Request completion of the current Run.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_summary": {"type": "string", "minLength": 1},
                    },
                    "required": ["run_summary"],
                    "additionalProperties": False,
                },
                "x-riftx": {
                    "resident": True,
                    "execution_policy": "registered_only",
                },
            }
        ],
        context_manifest={
            "run_id": "run-1",
            "session_id": "session-1",
            "agent_id": "primary",
            "execution_policy": "registered_only",
        },
    )
    engine = OpenAIAgentsEngine(
        DeferredRuntimeAgentFactory(control_handler=control_handler),
        model_provider=provider,
    )

    try:
        run = await engine.start(
            AgentEngineRequest(
                session_id="session-1",
                model="primary",
                input_items=compiled.input_items,
                context=compiled,
            )
        )
        events = [event async for event in run.events()]
    finally:
        await provider.aclose()

    assert AgentEngineEventType.ERROR not in [event.event_type for event in events]
    assert events[-1].event_type is AgentEngineEventType.RUN_COMPLETED
    assert control_calls == [
        (
            "complete_run",
            {"run_summary": "authorized objective complete"},
            "call-wire",
        )
    ]
    assert len(requests) == 1
