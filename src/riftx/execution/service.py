"""Durable idempotent Execution Service."""

from __future__ import annotations

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
)
from riftx.application.ports import ExecutionRepository, RunEventRepository, RunRepository
from riftx.domain import Execution, ExecutionStatus, Run, RunStatus
from riftx.persistence.runtime_repositories import (
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyToolCallIntentRepository,
)
from riftx.runner import ExecutionRunner
from riftx.runtime.types import ToolCallIntent, ToolCallStatus

from .models import ExecutionWaitResult, SubmitExecutionRequest
from .waiting import wait_for_execution

_SUBMITTABLE_INTENT_STATUSES = {
    ToolCallStatus.READY,
    ToolCallStatus.EXECUTING,
    ToolCallStatus.COMPLETED,
    ToolCallStatus.FAILED,
    ToolCallStatus.CANCELLED,
}

_EXECUTION_BLOCKED_RUN_STATUSES = {
    RunStatus.PAUSING,
    RunStatus.PAUSED,
    RunStatus.CANCELLING,
    RunStatus.CANCELLED,
    RunStatus.COMPLETING,
    RunStatus.COMPLETED,
    RunStatus.FAILED,
}


class ExecutionService:
    """Turn persisted Tool Call intents into idempotent Runner executions."""

    def __init__(
        self,
        *,
        execution_repository: ExecutionRepository,
        session_repository: SQLAlchemyAgentSessionRepository,
        tool_call_repository: SQLAlchemyToolCallIntentRepository,
        runner: ExecutionRunner,
        event_repository: RunEventRepository | None = None,
        run_repository: RunRepository | None = None,
    ) -> None:
        self._executions = execution_repository
        self._sessions = session_repository
        self._tool_calls = tool_call_repository
        self._runner = runner
        self._events = event_repository
        self._runs = run_repository

    async def submit(self, request: SubmitExecutionRequest) -> Execution:
        intent = await self._require_intent(request)
        existing = await self._executions.get_by_key(request.execution_key)
        if existing is not None:
            await self._sync_intent(intent, existing)
            return existing

        await self._require_execution_allowed(request.run_id)

        async def effect_guard() -> None:
            await self._require_execution_allowed(request.run_id)

        execution = await self._runner.start(
            request.to_launch_request(),
            effect_guard=effect_guard,
        )
        blocked_run = await self._blocked_run(request.run_id)
        if blocked_run is not None:
            if execution.status in {
                ExecutionStatus.CREATED,
                ExecutionStatus.QUEUED,
                ExecutionStatus.STARTING,
                ExecutionStatus.RUNNING,
            }:
                execution = await self._runner.cancel(execution.id)
            await self._sync_intent(intent, execution)
            await self._append_event(
                request.run_id,
                "execution.blocked_by_run_stop",
                {
                    "execution_id": execution.id,
                    "run_status": blocked_run.status.value,
                },
            )
            raise self._execution_blocked_error(blocked_run)
        if execution.execution_key != request.execution_key:
            raise RepositoryConflictError(
                f"Runner returned mismatched execution key for {request.tool_call_id!r}"
            )
        await self._sync_intent(intent, execution)
        await self._append_event(
            execution.run_id,
            "execution.submitted",
            {
                "execution_id": execution.id,
                "session_id": request.session_id,
                "tool_call_id": request.tool_call_id,
                "attempt_group": request.attempt_group,
                "execution_key": request.execution_key,
            },
        )
        return execution

    async def get(self, execution_id: str) -> Execution:
        execution = await self._executions.get(execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        return execution

    async def wait(
        self,
        execution_id: str,
        *,
        timeout_seconds: float = 30.0,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 64 * 1024,
        next_poll_after_seconds: int = 10,
    ) -> ExecutionWaitResult:
        execution = await self.get(execution_id)
        result = await wait_for_execution(
            self._runner,
            execution,
            timeout_seconds=timeout_seconds,
            stdout_cursor=stdout_cursor,
            stderr_cursor=stderr_cursor,
            max_bytes=max_bytes,
            next_poll_after_seconds=next_poll_after_seconds,
        )
        await self._sync_execution_intent(result.execution)
        await self._append_event(
            result.execution.run_id,
            "execution.wait_completed",
            {
                "execution_id": result.execution.id,
                "wait_status": result.wait_status.value,
                "execution_status": result.execution.status.value,
            },
        )
        return result

    async def cancel(self, execution_id: str, reason: str | None = None) -> Execution:
        await self.get(execution_id)
        execution = await self._runner.cancel(execution_id)
        await self._sync_execution_intent(execution)
        await self._append_event(
            execution.run_id,
            "execution.cancel_requested",
            {"execution_id": execution.id, "reason": reason},
        )
        return execution

    async def _require_intent(self, request: SubmitExecutionRequest) -> ToolCallIntent:
        session = await self._sessions.get(request.session_id)
        if session is None or session.run_id != request.run_id:
            raise EntityNotFoundError("AgentSession", request.session_id)
        intent = await self._tool_calls.get(request.tool_call_id)
        if intent is None:
            raise EntityNotFoundError("ToolCallIntent", request.tool_call_id)
        if intent.run_id != request.run_id or intent.session_id != request.session_id:
            raise ApplicationConflictError(
                "execution_identity_mismatch",
                "Tool Call intent does not belong to the requested Run and Session",
            )
        if intent.status not in _SUBMITTABLE_INTENT_STATUSES:
            raise ApplicationConflictError(
                "tool_call_not_ready",
                f"Tool Call intent cannot execute from status {intent.status.value!r}",
            )
        return intent

    async def _require_execution_allowed(self, run_id: str) -> None:
        blocked = await self._blocked_run(run_id)
        if blocked is not None:
            raise self._execution_blocked_error(blocked)

    async def _blocked_run(self, run_id: str) -> Run | None:
        if self._runs is None:
            return None
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        return run if run.status in _EXECUTION_BLOCKED_RUN_STATUSES else None

    @staticmethod
    def _execution_blocked_error(run: Run) -> ApplicationConflictError:
        return ApplicationConflictError(
            "run_execution_blocked",
            f"Run {run.id!r} cannot start a new execution while it is {run.status.value}",
            details={"run_id": run.id, "status": run.status.value},
        )

    async def _sync_execution_intent(self, execution: Execution) -> None:
        if execution.tool_call_id is None:
            return
        intent = await self._tool_calls.get(execution.tool_call_id)
        if intent is not None:
            await self._sync_intent(intent, execution)

    async def _sync_intent(self, intent: ToolCallIntent, execution: Execution) -> None:
        target = _intent_status(execution.status)
        if intent.status is target:
            return
        intent.status = target
        await self._tool_calls.save(intent)

    async def _append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        if self._events is not None:
            await self._events.append(run_id, event_type, payload)


def _intent_status(status: ExecutionStatus) -> ToolCallStatus:
    if status in {
        ExecutionStatus.QUEUED,
        ExecutionStatus.CREATED,
        ExecutionStatus.STARTING,
        ExecutionStatus.RUNNING,
    }:
        return ToolCallStatus.EXECUTING
    if status in {ExecutionStatus.COMPLETED, ExecutionStatus.EXITED}:
        return ToolCallStatus.COMPLETED
    if status is ExecutionStatus.CANCELLED:
        return ToolCallStatus.CANCELLED
    return ToolCallStatus.FAILED
