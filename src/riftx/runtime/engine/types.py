"""Provider-neutral Agent Engine contracts and event models."""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, Field

from riftx.domain.base import DomainModel, utc_now
from riftx.runtime.types import ProviderState


class AgentEngineEventType(StrEnum):
    RUN_STARTED = "run_started"
    ASSISTANT_DELTA = "assistant_delta"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_ARGUMENT_DELTA = "tool_call_argument_delta"
    TOOL_CALL_READY = "tool_call_ready"
    PLAN_UPDATE = "plan_update"
    SUBAGENT_REQUESTED = "subagent_requested"
    FINAL_OUTPUT = "final_output"
    USAGE = "usage"
    ERROR = "error"
    RUN_COMPLETED = "run_completed"


class AgentEngineEvent(DomainModel):
    sequence: int = Field(ge=1)
    event_type: AgentEngineEventType
    data: dict[str, object] = Field(default_factory=dict)
    created_at: AwareDatetime = Field(default_factory=utc_now)


class AgentEngineState(DomainModel):
    engine_type: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    serialized_state: dict[str, object] | str | None = None
    sdk_run_state: dict[str, object] | None = None
    previous_response_id: str | None = None
    last_model_call_id: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)

    def to_provider_state(self, session_id: str) -> ProviderState:
        return ProviderState(
            session_id=session_id,
            provider=self.provider,
            model=self.model,
            engine_type=self.engine_type,
            engine_version=self.engine_version,
            state={
                "serialized_state": self.serialized_state,
                "sdk_run_state": self.sdk_run_state,
                "last_model_call_id": self.last_model_call_id,
            },
            previous_response_id=self.previous_response_id,
            created_at=self.created_at,
        )

    @classmethod
    def from_provider_state(cls, state: ProviderState) -> AgentEngineState:
        serialized_state = state.state.get("serialized_state")
        sdk_run_state = state.state.get("sdk_run_state")
        last_model_call_id = state.state.get("last_model_call_id")
        return cls(
            engine_type=state.engine_type,
            engine_version=state.engine_version,
            provider=state.provider,
            model=state.model,
            serialized_state=serialized_state if isinstance(serialized_state, dict | str) else None,
            sdk_run_state=sdk_run_state if isinstance(sdk_run_state, dict) else None,
            previous_response_id=state.previous_response_id,
            last_model_call_id=last_model_call_id if isinstance(last_model_call_id, str) else None,
            created_at=state.created_at,
        )


class AgentEngineRequest(DomainModel):
    session_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    input_text: str | None = None
    input_items: list[dict[str, object]] = Field(default_factory=list)
    context: object | None = None
    max_turns: int = Field(default=10, ge=1)

    def engine_input(self) -> str | list[dict[str, object]]:
        if self.input_items:
            return self.input_items
        return self.input_text or ""


class AgentEngineResumeRequest(AgentEngineRequest):
    state: AgentEngineState


@runtime_checkable
class AgentEngineRun(Protocol):
    async def events(self) -> AsyncIterator[AgentEngineEvent]: ...

    async def suspend(self) -> AgentEngineState: ...

    async def cancel(self) -> None: ...


@runtime_checkable
class AgentEngine(Protocol):
    async def start(self, request: AgentEngineRequest) -> AgentEngineRun: ...

    async def resume(self, request: AgentEngineResumeRequest) -> AgentEngineRun: ...
