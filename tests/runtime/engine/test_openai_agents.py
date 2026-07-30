from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

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
    ) -> None:
        self.sdk_events = events
        self.final_output = final_output
        self.stream_error = stream_error
        self.raw_responses: list[object] = []
        self.cancel_modes: list[str] = []
        self.state = state or FakeState()
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


def run_item(name: str, *, tool_name: str | None = None) -> object:
    item = SimpleNamespace(raw_item=SimpleNamespace(name=tool_name))
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
            run_item("tool_called", tool_name="scan"),
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
