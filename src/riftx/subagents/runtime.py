"""Agent Engine adapters for durable, independently executed Subagent sessions."""

from __future__ import annotations

import json
from time import monotonic
from typing import Protocol

from riftx.application.errors import ApplicationConflictError
from riftx.domain import MessageRole, MessageType
from riftx.domain.base import new_id
from riftx.runtime.lifecycle import RunCycleRequest, RunCycleResult
from riftx.runtime.session import SessionManager
from riftx.runtime.types import YieldReason

from .manager import SubagentHandle
from .models import DelegationPacket, SubagentResult, SubagentStatus
from .orchestrator import SubagentOrchestrator


class SubagentCycleCoordinator(Protocol):
    async def run_cycle(self, request: RunCycleRequest) -> RunCycleResult: ...


class SubagentExecutionInputResolver(Protocol):
    async def wait_for_execution_input(
        self,
        run_id: str,
        execution_id: str,
        *,
        timeout_seconds: float,
    ) -> dict[str, object]: ...


class DurableSubagentTaskRunner:
    """Drive one child Session through bounded Runtime cycles and tool waits."""

    def __init__(
        self,
        *,
        coordinator: SubagentCycleCoordinator,
        sessions: SessionManager,
        execution_inputs: SubagentExecutionInputResolver | None = None,
        worker_id: str = "subagent-runtime",
    ) -> None:
        self._coordinator = coordinator
        self._sessions = sessions
        self._execution_inputs = execution_inputs
        self._worker_id = worker_id

    async def run(self, handle: SubagentHandle) -> SubagentResult:
        deadline = monotonic() + handle.delegation.timeout_seconds
        model_calls = 0
        tool_calls = 0
        input_items: list[dict[str, object]] = []
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Subagent Runtime deadline expired")
            cycle = await self._coordinator.run_cycle(
                RunCycleRequest(
                    run_id=handle.session.run_id,
                    session_id=handle.session.id,
                    worker_id=f"{self._worker_id}:{handle.session.id}",
                    cycle_id=new_id(),
                    input_items=input_items,
                    current_path=handle.delegation.workspace,
                    subagent_mode=True,
                )
            )
            input_items = []
            model_calls += cycle.model_call_count
            tool_calls += cycle.tool_call_count
            if (
                model_calls >= handle.delegation.max_turns
                and cycle.yield_reason is not YieldReason.RUN_COMPLETED
            ):
                return self._partial(handle, "Subagent model-call limit reached.")
            if (
                tool_calls >= handle.delegation.max_tool_calls
                and cycle.yield_reason is not YieldReason.RUN_COMPLETED
            ):
                return self._partial(handle, "Subagent tool-call limit reached.")
            if cycle.yield_reason in {YieldReason.TOOL_RUNNING, YieldReason.TERMINAL_OPEN}:
                execution_id = cycle.waiting_execution_id or cycle.waiting_object_id
                if execution_id is None or self._execution_inputs is None:
                    return self._partial(
                        handle,
                        "Subagent cannot resolve its deferred tool execution.",
                    )
                input_items = [
                    await self._execution_inputs.wait_for_execution_input(
                        handle.session.run_id,
                        execution_id,
                        timeout_seconds=max(0.001, deadline - monotonic()),
                    )
                ]
                continue
            if cycle.yield_reason is YieldReason.CYCLE_LIMIT_REACHED:
                continue
            if cycle.yield_reason is YieldReason.RUN_COMPLETED:
                return await self._result_packet(handle)
            if cycle.yield_reason in {
                YieldReason.APPROVAL_REQUIRED,
                YieldReason.USER_INPUT_REQUIRED,
            }:
                return self._partial(
                    handle,
                    f"Subagent stopped at unsupported wait: {cycle.yield_reason.value}.",
                )
            if cycle.yield_reason in {
                YieldReason.FATAL_FAILURE,
                YieldReason.RUN_CANCELLED,
            }:
                return SubagentResult(
                    task_id=handle.delegation.task_id,
                    status=SubagentStatus.FAILED,
                    summary=f"Subagent stopped: {cycle.yield_reason.value}.",
                    unresolved_questions=[handle.delegation.task],
                )
            return self._partial(
                handle,
                f"Subagent yielded without a resumable result: {cycle.yield_reason.value}.",
            )

    async def _result_packet(self, handle: SubagentHandle) -> SubagentResult:
        loaded = await self._sessions.load_session(handle.session.id)
        for message in reversed(loaded.transcript):
            if (
                message.role is not MessageRole.ASSISTANT
                or message.message_type is not MessageType.ASSISTANT_MESSAGE
            ):
                continue
            payload = _result_payload(message.structured_content, message.content)
            if payload is None:
                continue
            payload.setdefault("task_id", handle.delegation.task_id)
            try:
                result = SubagentResult.model_validate(payload)
            except ValueError:
                continue
            if result.task_id == handle.delegation.task_id:
                return result
        return self._partial(
            handle,
            "Subagent completed without a valid structured Result Packet.",
        )

    @staticmethod
    def _partial(handle: SubagentHandle, summary: str) -> SubagentResult:
        return SubagentResult(
            task_id=handle.delegation.task_id,
            status=SubagentStatus.PARTIAL,
            summary=summary,
            unresolved_questions=[handle.delegation.task],
        )


class ModelDelegationExecutor:
    def __init__(self, orchestrator: SubagentOrchestrator) -> None:
        self._orchestrator = orchestrator

    async def execute(
        self,
        parent_session_id: str,
        requests: list[dict[str, object]],
    ) -> list[SubagentResult]:
        delegations = [
            DelegationPacket.model_validate(_arguments(request)) for request in requests
        ]
        return await self._orchestrator.execute_many(
            parent_session_id=parent_session_id,
            delegations=delegations,
        )


def _arguments(request: dict[str, object]) -> dict[str, object]:
    raw = request.get("arguments")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApplicationConflictError(
                "invalid_subagent_delegation",
                "Subagent delegation arguments are not valid JSON",
            ) from exc
        if isinstance(payload, dict):
            return payload
    if "task" in request:
        return dict(request)
    raise ApplicationConflictError(
        "invalid_subagent_delegation",
        "Subagent delegation requires a structured Delegation Packet",
    )


def _result_payload(
    structured_content: dict[str, object] | None,
    content: str | None,
) -> dict[str, object] | None:
    candidates: list[object] = [structured_content, content]
    if isinstance(structured_content, dict):
        candidates.extend(
            structured_content.get(key) for key in ("result", "output", "content")
        )
    for candidate in candidates:
        if isinstance(candidate, dict):
            if "summary" in candidate:
                return dict(candidate)
            continue
        if not isinstance(candidate, str):
            continue
        raw = candidate.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            if raw.lstrip().startswith("json"):
                raw = raw.lstrip()[4:].lstrip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
