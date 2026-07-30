"""Runtime bridge from durable Tool Call intent to deferred Runner Execution."""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from riftx.application.errors import ApplicationConflictError, RepositoryConflictError
from riftx.domain import ApprovalLevel, Execution, ExecutorType
from riftx.executors import EnvironmentMode, ShellKind
from riftx.persistence.runtime_repositories import SQLAlchemyToolCallIntentRepository
from riftx.runtime.engine import AgentEngineEvent
from riftx.runtime.types import AgentCycle, AgentSession, AgentStep, ToolCallIntent, ToolCallStatus

from .models import SubmitExecutionRequest
from .service import ExecutionService


class DeferredExecutionSpec(BaseModel):
    """Runner launch data supplied by the Tool Proxy after policy resolution."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    executor_type: ExecutorType
    cwd: Path
    argv: list[str] = Field(default_factory=list)
    command_text: str | None = None
    tool_version: str | None = None
    shell: ShellKind | None = None
    shell_path: Path | None = None
    environment_mode: EnvironmentMode = EnvironmentMode.INHERIT
    env: dict[str, str | None] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    attempt_group: str = Field(default="initial", min_length=1, max_length=64)


class DeferredExecutionDispatcher:
    """Persist a stable Tool Call and submit exactly one deferred Execution."""

    def __init__(
        self,
        *,
        tool_call_repository: SQLAlchemyToolCallIntentRepository,
        execution_service: ExecutionService,
    ) -> None:
        self._tool_calls = tool_call_repository
        self._executions = execution_service

    async def dispatch(
        self,
        *,
        session: AgentSession,
        cycle: AgentCycle,
        step: AgentStep,
        event: AgentEngineEvent,
    ) -> Execution:
        call_id = _required_string(event.data, "call_id")
        tool_id = _tool_id(event.data)
        raw_spec = event.data.get("execution")
        if not isinstance(raw_spec, dict):
            raise ApplicationConflictError(
                "deferred_execution_missing",
                f"Tool Call {call_id!r} is missing resolved execution data",
            )
        spec = DeferredExecutionSpec.model_validate(raw_spec)
        intent = await self._persist_intent(
            session=session,
            cycle=cycle,
            step=step,
            event=event,
            call_id=call_id,
            tool_id=tool_id,
            spec=spec,
        )
        return await self._executions.submit(
            SubmitExecutionRequest(
                run_id=session.run_id,
                session_id=session.id,
                tool_call_id=intent.id,
                attempt_group=spec.attempt_group,
                node_id=spec.node_id,
                executor_type=spec.executor_type,
                cwd=spec.cwd,
                argv=spec.argv,
                command_text=spec.command_text,
                tool_id=tool_id,
                tool_version=spec.tool_version,
                shell=spec.shell,
                shell_path=spec.shell_path,
                environment_mode=spec.environment_mode,
                env=spec.env,
                timeout_seconds=spec.timeout_seconds,
            )
        )

    async def _persist_intent(
        self,
        *,
        session: AgentSession,
        cycle: AgentCycle,
        step: AgentStep,
        event: AgentEngineEvent,
        call_id: str,
        tool_id: str,
        spec: DeferredExecutionSpec,
    ) -> ToolCallIntent:
        intent_id = build_tool_call_intent_id(
            run_id=session.run_id,
            session_id=session.id,
            engine_call_id=call_id,
        )
        existing = await self._tool_calls.get(intent_id)
        if existing is not None:
            _validate_existing_intent(existing, session=session, call_id=call_id, tool_id=tool_id)
            return existing

        intent = ToolCallIntent(
            id=intent_id,
            run_id=session.run_id,
            session_id=session.id,
            cycle_id=cycle.id,
            step_id=step.id,
            tool_id=tool_id,
            arguments=_arguments(event.data),
            command_preview=spec.command_text or shlex.join(spec.argv),
            reason=str(event.data.get("reason") or ""),
            target_summary=_optional_string(event.data.get("target_summary")),
            approval_level=ApprovalLevel(
                str(event.data.get("approval_level") or ApprovalLevel.SENSITIVE.value)
            ),
            status=ToolCallStatus.READY,
            engine_call_id=call_id,
        )
        try:
            return await self._tool_calls.create(intent)
        except RepositoryConflictError:
            raced = await self._tool_calls.get(intent_id)
            if raced is None:
                raise
            _validate_existing_intent(raced, session=session, call_id=call_id, tool_id=tool_id)
            return raced


def build_tool_call_intent_id(*, run_id: str, session_id: str, engine_call_id: str) -> str:
    identity = "\x1f".join((run_id, session_id, engine_call_id))
    return f"tool-call:v1:{hashlib.sha256(identity.encode()).hexdigest()}"


def _required_string(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ApplicationConflictError(
            "invalid_tool_call_event", f"Deferred Tool Call requires {key!r}"
        )
    return value


def _tool_id(data: dict[str, object]) -> str:
    value = data.get("tool_id") or data.get("name")
    if not isinstance(value, str) or not value:
        raise ApplicationConflictError(
            "invalid_tool_call_event", "Deferred Tool Call requires a tool ID"
        )
    return value


def _arguments(data: dict[str, object]) -> dict[str, object]:
    value = data.get("arguments")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ApplicationConflictError(
                "invalid_tool_arguments", "Deferred Tool Call arguments are not valid JSON"
            ) from exc
        if isinstance(parsed, dict):
            return parsed
    return {}


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _validate_existing_intent(
    intent: ToolCallIntent,
    *,
    session: AgentSession,
    call_id: str,
    tool_id: str,
) -> None:
    if (
        intent.run_id != session.run_id
        or intent.session_id != session.id
        or intent.engine_call_id != call_id
        or intent.tool_id != tool_id
    ):
        raise ApplicationConflictError(
            "tool_call_identity_mismatch",
            f"Persisted Tool Call {intent.id!r} does not match the deferred request",
        )
