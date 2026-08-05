from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from agents.models.chatcmpl_converter import Converter

from riftx.runtime.engine import (
    AgentEngine,
    AgentEngineEvent,
    AgentEngineEventType,
    AgentEngineRequest,
    AgentEngineResumeRequest,
    AgentEngineState,
    InvalidProviderStateError,
    OpenAIAgentsEngine,
)
from riftx.runtime.lifecycle import CompiledContext


class FakeState:
    def __init__(self, payload: dict[str, object] | None = None, error: Exception | None = None):
        self.payload = payload or {"current_turn": 2}
        self.error = error

    def to_json(self, *, strict_context: bool = False) -> dict[str, object]:
        assert strict_context is False
        if self.error:
            raise self.error
        return self.payload


class FakeStreamingResult:
    def __init__(
        self,
        events: list[object],
        *,
        final_output: object = None,
        stream_error: Exception | None = None,
        state: FakeState | None = None,
        interruptions: list[object] | None = None,
    ) -> None:
        self.sdk_events = events
        self.final_output = final_output
        self.stream_error = stream_error
        self.raw_responses: list[object] = []
        self.cancel_modes: list[str] = []
        self.state = state or FakeState()
        self.interruptions = interruptions or []
        self._previous_response_id = "response-2"

    async def stream_events(self) -> AsyncIterator[object]:
        for event in self.sdk_events:
            yield event
        if self.stream_error:
            raise self.stream_error

    def cancel(self, mode: str = "immediate") -> None:
        self.cancel_modes.append(mode)

    def to_state(self) -> FakeState:
        return self.state


def raw_event(event_type: str, **values: object) -> object:
    return SimpleNamespace(
        type="raw_response_event",
        data=SimpleNamespace(type=event_type, **values),
    )


def run_item(
    name: str,
    *,
    tool_name: str | None = None,
    call_id: str | None = None,
    arguments: str | None = None,
) -> object:
    item = SimpleNamespace(
        raw_item=SimpleNamespace(
            name=tool_name,
            call_id=call_id,
            arguments=arguments,
        )
    )
    return SimpleNamespace(type="run_item_stream_event", name=name, item=item)


async def collect(run: object) -> list[AgentEngineEvent]:
    return [event async for event in run.events()]


async def test_adapter_translates_text_tool_and_streaming_events_in_order() -> None:
    result = FakeStreamingResult(
        [
            raw_event("response.output_text.delta", delta="hello "),
            raw_event(
                "response.output_item.added",
                item=SimpleNamespace(type="function_call", call_id="call-1", name="scan"),
            ),
            raw_event(
                "response.function_call_arguments.delta",
                item_id="call-1",
                delta='{"target":"192.0.2.1"}',
            ),
            run_item(
                "tool_called",
                tool_name="scan",
                call_id="call-1",
                arguments='{"target":"192.0.2.1"}',
            ),
            run_item("message_output_created"),
        ],
        final_output="done",
    )
    engine = OpenAIAgentsEngine(lambda request: object(), stream_runner=lambda *a, **k: result)

    events = await collect(
        await engine.start(
            AgentEngineRequest(session_id="session-1", model="gpt-5.6", input_text="start")
        )
    )

    assert [event.event_type for event in events] == [
        AgentEngineEventType.RUN_STARTED,
        AgentEngineEventType.ASSISTANT_DELTA,
        AgentEngineEventType.TOOL_CALL_STARTED,
        AgentEngineEventType.TOOL_CALL_ARGUMENT_DELTA,
        AgentEngineEventType.TOOL_CALL_READY,
        AgentEngineEventType.ASSISTANT_MESSAGE,
        AgentEngineEventType.FINAL_OUTPUT,
        AgentEngineEventType.RUN_COMPLETED,
    ]
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[1].data == {"delta": "hello "}
    assert events[3].data == {"call_id": "call-1", "delta": '{"target":"192.0.2.1"}'}
    assert events[4].data == {
        "call_id": "call-1",
        "tool_id": "scan",
        "arguments": '{"target":"192.0.2.1"}',
    }


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("update_plan", AgentEngineEventType.PLAN_UPDATE),
        ("delegate", AgentEngineEventType.SUBAGENT_REQUESTED),
    ],
)
async def test_adapter_translates_runtime_control_tools(
    tool_name: str, expected: AgentEngineEventType
) -> None:
    result = FakeStreamingResult([run_item("tool_called", tool_name=tool_name)])
    engine = OpenAIAgentsEngine(lambda request: object(), stream_runner=lambda *a, **k: result)
    events = await collect(
        await engine.start(AgentEngineRequest(session_id="session-1", model="gpt-5.6"))
    )
    assert events[1].event_type is expected


async def test_adapter_emits_explicit_control_tool_interruption() -> None:
    interruption = SimpleNamespace(
        tool_name="apply_patch",
        call_id="patch-call",
        arguments='{"path":"src/app.py"}',
    )
    result = FakeStreamingResult([], interruptions=[interruption])
    context = CompiledContext(
        system_instructions="Stay inside scope.",
        available_tools=[
            {
                "type": "function",
                "name": "apply_patch",
                "parameters": {"type": "object"},
                "x-riftx": {
                    "resident": True,
                    "approval_level": "always",
                    "approval_policy": "explicit",
                },
            }
        ],
    )
    engine = OpenAIAgentsEngine(lambda request: object(), stream_runner=lambda *a, **k: result)

    events = await collect(
        await engine.start(
            AgentEngineRequest(
                session_id="session-1",
                model="gpt-5.6",
                context=context,
            )
        )
    )

    assert events[1].event_type is AgentEngineEventType.TOOL_CALL_READY
    assert events[1].data == {
        "call_id": "patch-call",
        "tool_id": "apply_patch",
        "arguments": '{"path":"src/app.py"}',
        "approval_level": "always",
        "approval_required": True,
        "approval_policy": "explicit",
    }


async def test_streaming_model_failure_becomes_stable_error_event() -> None:
    result = FakeStreamingResult([], stream_error=TimeoutError("model timed out"))
    engine = OpenAIAgentsEngine(lambda request: object(), stream_runner=lambda *a, **k: result)
    events = await collect(
        await engine.start(AgentEngineRequest(session_id="session-1", model="gpt-5.6"))
    )
    assert [event.event_type for event in events] == [
        AgentEngineEventType.RUN_STARTED,
        AgentEngineEventType.ERROR,
        AgentEngineEventType.RUN_COMPLETED,
    ]
    assert events[1].data["error_type"] == "TimeoutError"
    assert events[-1].data == {"status": "failed"}


async def test_suspend_serializes_state_and_cancel_uses_sdk_controls() -> None:
    result = FakeStreamingResult([])
    engine = OpenAIAgentsEngine(lambda request: object(), stream_runner=lambda *a, **k: result)
    run = await engine.start(AgentEngineRequest(session_id="session-1", model="gpt-5.6"))

    state = await run.suspend()
    assert result.cancel_modes == ["after_turn"]
    assert state.engine_type == "openai-agents"
    assert state.sdk_run_state == {"current_turn": 2}
    assert state.previous_response_id == "response-2"

    await run.cancel()
    assert result.cancel_modes == ["after_turn", "immediate"]


async def test_resume_deserializes_provider_state_before_starting_stream() -> None:
    result = FakeStreamingResult([])
    captured: dict[str, object] = {}

    def stream_runner(agent: object, engine_input: object, **kwargs: object) -> FakeStreamingResult:
        captured["input"] = engine_input
        return result

    engine = OpenAIAgentsEngine(lambda request: object(), stream_runner=stream_runner)
    state = AgentEngineState(
        engine_type="openai-agents",
        engine_version="0.19",
        provider="openai",
        model="gpt-5.6",
        sdk_run_state={"current_turn": 1},
    )
    restored = object()
    with patch(
        "riftx.runtime.engine.openai_agents.RunState.from_json",
        new=AsyncMock(return_value=restored),
    ):
        await engine.resume(
            AgentEngineResumeRequest(session_id="session-1", model="gpt-5.6", state=state)
        )
    assert captured["input"] is restored


@pytest.mark.parametrize(
    ("decision", "feedback", "expected"),
    [
        ("approve_tool_for_run", None, "approve"),
        ("reject_with_feedback", "Use a narrower patch.", "reject"),
    ],
)
async def test_resume_applies_exact_sdk_approval_decision_before_running(
    decision: str,
    feedback: str | None,
    expected: str,
) -> None:
    result = FakeStreamingResult([])
    interruption = SimpleNamespace(call_id="patch-call")

    class ApprovalState:
        def __init__(self) -> None:
            self.approved: list[tuple[object, bool]] = []
            self.rejected: list[tuple[object, bool, str | None]] = []

        def get_interruptions(self) -> list[object]:
            return [interruption]

        def approve(self, item: object, always_approve: bool = False) -> None:
            self.approved.append((item, always_approve))

        def reject(
            self,
            item: object,
            always_reject: bool = False,
            rejection_message: str | None = None,
        ) -> None:
            self.rejected.append((item, always_reject, rejection_message))

    restored = ApprovalState()
    engine = OpenAIAgentsEngine(lambda request: object(), stream_runner=lambda *a, **k: result)
    state = AgentEngineState(
        engine_type="openai-agents",
        engine_version="0.19",
        provider="openai",
        model="gpt-5.6",
        sdk_run_state={"current_turn": 1},
    )
    with patch(
        "riftx.runtime.engine.openai_agents.RunState.from_json",
        new=AsyncMock(return_value=restored),
    ):
        await engine.resume(
            AgentEngineResumeRequest(
                session_id="session-1",
                model="gpt-5.6",
                state=state,
                input_items=[
                    {
                        "type": "approval_decision",
                        "engine_call_id": "patch-call",
                        "decision": decision,
                        "feedback": feedback,
                    }
                ],
            )
        )

    if expected == "approve":
        assert restored.approved == [(interruption, False)]
        assert restored.rejected == []
    else:
        assert restored.approved == []
        assert restored.rejected == [(interruption, False, feedback)]


async def test_invalid_provider_state_returns_explicit_error() -> None:
    engine = OpenAIAgentsEngine(lambda request: object(), stream_runner=lambda *a, **k: None)
    state = AgentEngineState(
        engine_type="other-engine",
        engine_version="1",
        provider="other",
        model="model",
        sdk_run_state={"bad": True},
    )
    with pytest.raises(InvalidProviderStateError, match="cannot resume"):
        await engine.resume(
            AgentEngineResumeRequest(session_id="session-1", model="gpt-5.6", state=state)
        )


async def test_mock_engine_satisfies_protocol_without_sdk_types() -> None:
    class MockRun:
        async def events(self) -> AsyncIterator[AgentEngineEvent]:
            yield AgentEngineEvent(
                sequence=1,
                event_type=AgentEngineEventType.RUN_COMPLETED,
            )

        async def suspend(self) -> AgentEngineState:
            return AgentEngineState(
                engine_type="mock",
                engine_version="1",
                provider="mock",
                model="mock",
            )

        async def cancel(self) -> None:
            return None

    class MockEngine:
        async def start(self, request: AgentEngineRequest) -> MockRun:
            return MockRun()

        async def resume(self, request: AgentEngineResumeRequest) -> MockRun:
            return MockRun()

    engine: AgentEngine = MockEngine()
    assert isinstance(engine, AgentEngine)
    event = [
        item
        async for item in (
            await engine.start(AgentEngineRequest(session_id="session-1", model="mock"))
        ).events()
    ][0]
    assert event.__class__.__module__.startswith("riftx.runtime.engine")


async def test_corrupt_sdk_state_does_not_escape_as_sdk_exception() -> None:
    engine = OpenAIAgentsEngine(lambda request: object(), stream_runner=lambda *a, **k: None)
    state = AgentEngineState(
        engine_type="openai-agents",
        engine_version="0.19",
        provider="openai",
        model="gpt-5.6",
        sdk_run_state={"corrupt": True},
    )
    with (
        patch(
            "riftx.runtime.engine.openai_agents.RunState.from_json",
            new=AsyncMock(side_effect=KeyError("missing current_agent")),
        ),
        pytest.raises(InvalidProviderStateError, match="could not deserialize"),
    ):
        await engine.resume(
            AgentEngineResumeRequest(session_id="session-1", model="gpt-5.6", state=state)
        )


def test_engine_state_round_trips_through_durable_provider_state() -> None:
    state = AgentEngineState(
        engine_type="openai-agents",
        engine_version="0.19",
        provider="openai",
        model="gpt-5.6",
        serialized_state={"portable": True},
        sdk_run_state={"current_turn": 2},
        previous_response_id="response-1",
        last_model_call_id="model-call-1",
    )
    durable = state.to_provider_state("session-1")
    restored = AgentEngineState.from_provider_state(durable)
    assert restored == state


async def test_compiled_system_instructions_replace_factory_prompt() -> None:
    result = FakeStreamingResult([])
    agent = SimpleNamespace(instructions="legacy prompt")
    captured: dict[str, object] = {}

    def stream_runner(selected_agent: object, *_: object, **__: object) -> FakeStreamingResult:
        captured["instructions"] = selected_agent.instructions  # type: ignore[attr-defined]
        return result

    engine = OpenAIAgentsEngine(lambda request: agent, stream_runner=stream_runner)
    await engine.start(
        AgentEngineRequest(
            session_id="session-1",
            model="gpt-5.6",
            context=SimpleNamespace(system_instructions="compiled authoritative prompt"),
        )
    )

    assert captured["instructions"] == "compiled authoritative prompt"


async def test_start_removes_riftx_metadata_before_agents_sdk_conversion() -> None:
    result = FakeStreamingResult([])
    captured: dict[str, object] = {}

    def stream_runner(agent: object, engine_input: object, **kwargs: object) -> FakeStreamingResult:
        captured["input"] = engine_input
        return result

    engine = OpenAIAgentsEngine(lambda request: object(), stream_runner=stream_runner)
    await engine.start(
        AgentEngineRequest(
            session_id="session-1",
            model="chat-profile",
            input_items=[
                {
                    "role": "user",
                    "content": "Begin the bounded task.",
                    "source_event_id": "event-1",
                    "source_refs": ["message://message-1"],
                },
                {
                    "id": "tool-result:execution-1",
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "content": {"exit_code": 0, "summary": "safe result"},
                    "source_refs": ["artifact://execution-1/stdout"],
                    "priority": 100,
                    "required": True,
                },
            ],
        )
    )

    sdk_input = captured["input"]
    assert sdk_input == [
        {"role": "user", "content": "Begin the bounded task."},
        {
            "role": "user",
            "content": (
                '[RiftX context]\n{"content": {"exit_code": 0, '
                '"summary": "safe result"}, "tool_call_id": "call-1", '
                '"type": "tool_result"}'
            ),
        },
    ]
    assert Converter.items_to_messages(sdk_input, model="wire-chat-model") == sdk_input


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ({"type": "unknown_context", "content": "unsafe"}, "unsupported model input"),
        (
            {"type": "function_call_output", "output": "missing identity"},
            "requires a non-empty call_id",
        ),
        (
            {"type": "function_call", "call_id": "call-1", "name": "scan"},
            "requires call_id, name, and string arguments",
        ),
    ],
)
async def test_start_rejects_unknown_or_malformed_provider_items(
    item: dict[str, object],
    message: str,
) -> None:
    engine = OpenAIAgentsEngine(
        lambda request: object(),
        stream_runner=lambda *args, **kwargs: FakeStreamingResult([]),
    )

    with pytest.raises(ValueError, match=message):
        await engine.start(
            AgentEngineRequest(
                session_id="session-1",
                model="chat-profile",
                input_items=[item],
            )
        )
