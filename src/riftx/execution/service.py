"""Durable idempotent Execution Service."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
)
from riftx.application.ports import (
    ExecutionAdmissionIdentity,
    ExecutionRepository,
    RunEventRepository,
    RunRepository,
    ToolCallIntentExecutionClaim,
    ToolCallIntentRepository,
)
from riftx.domain import Execution, ExecutionStatus, ExecutorType, Run, RunStatus
from riftx.persistence.runtime_repositories import SQLAlchemyAgentSessionRepository
from riftx.runner import ExecutionLaunchRequest, ExecutionRunner
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

_INTENT_SYNC_EXPECTED = {
    ToolCallStatus.EXECUTING: frozenset({ToolCallStatus.READY}),
    ToolCallStatus.COMPLETED: frozenset(
        {
            ToolCallStatus.READY,
            ToolCallStatus.EXECUTING,
        }
    ),
    ToolCallStatus.FAILED: frozenset(
        {
            ToolCallStatus.READY,
            ToolCallStatus.EXECUTING,
        }
    ),
    ToolCallStatus.CANCELLED: frozenset(
        {
            ToolCallStatus.READY,
            ToolCallStatus.EXECUTING,
            ToolCallStatus.FAILED,
        }
    ),
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
        tool_call_repository: ToolCallIntentRepository,
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
        launch = _freeze_launch_request(request.to_launch_request())
        admission = _launch_admission_identity(launch)
        intent = await self._require_intent(admission)
        existing = await self._executions.get_by_key(admission.execution_key)
        if existing is not None:
            _require_execution_matches_admission(existing, admission)
            await self._sync_intent(intent, existing)
            return existing

        await self._require_execution_allowed(admission.run_id)
        claim = await self._tool_calls.claim_execution(
            intent.id,
            execution_key=admission.execution_key,
            attempt_group=admission.attempt_group,
        )
        if not claim.acquired:
            raise ApplicationConflictError(
                "tool_call_not_ready",
                f"Tool Call intent cannot claim execution {admission.execution_key!r} "
                f"from status {claim.intent.status.value!r}",
            )

        async def effect_guard() -> None:
            await self._require_execution_allowed(admission.run_id)
            if not await self._tool_calls.execution_claim_is_current(
                intent.id,
                execution_key=admission.execution_key,
                attempt_group=admission.attempt_group,
            ):
                raise ApplicationConflictError(
                    "tool_call_execution_claim_lost",
                    f"Tool Call {intent.id!r} no longer owns execution {admission.execution_key!r}",
                )

        try:
            execution = await self._runner.start(
                launch.model_copy(deep=True),
                effect_guard=effect_guard,
            )
        except BaseException as exc:
            try:
                await asyncio.shield(self._settle_failed_submit(admission, claim))
            except BaseException as settlement_error:
                exc.add_note(
                    "Durable execution admission settlement also failed; the execution "
                    f"claim was left fail-closed: {settlement_error!r}"
                )
            raise
        blocked_run = await self._blocked_run(admission.run_id)
        if blocked_run is not None:
            if execution.status in {
                ExecutionStatus.CREATED,
                ExecutionStatus.QUEUED,
                ExecutionStatus.STARTING,
                ExecutionStatus.RUNNING,
            }:
                execution = await self._runner.cancel(execution.id)
            await self._sync_intent(claim.intent, execution)
            await self._append_event(
                admission.run_id,
                "execution.blocked_by_run_stop",
                {
                    "execution_id": execution.id,
                    "run_status": blocked_run.status.value,
                },
            )
            raise self._execution_blocked_error(blocked_run)
        _require_execution_matches_admission(execution, admission)
        await self._sync_intent(claim.intent, execution)
        await self._append_event(
            admission.run_id,
            "execution.submitted",
            {
                "execution_id": execution.id,
                "session_id": admission.session_id,
                "tool_call_id": admission.tool_call_id,
                "attempt_group": admission.attempt_group,
                "execution_key": admission.execution_key,
            },
            event_id=_submitted_event_id(admission.execution_key),
        )
        return execution

    async def get(self, execution_id: str) -> Execution:
        execution = await self._executions.get(execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        return execution

    async def find_admission(
        self,
        identity: ExecutionAdmissionIdentity,
    ) -> Execution | None:
        """Return only a durable row with the complete expected admission identity."""

        return await self._executions.find_admission(identity)

    async def sync_intent_execution(
        self,
        intent: ToolCallIntent,
        execution: Execution,
    ) -> ToolCallIntent:
        """Project an Execution only while its exact durable claim remains current."""

        return await self._sync_intent(intent, execution)

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

    async def _require_intent(
        self,
        admission: ExecutionAdmissionIdentity,
    ) -> ToolCallIntent:
        session = await self._sessions.get(admission.session_id)
        if session is None or session.run_id != admission.run_id:
            raise EntityNotFoundError("AgentSession", admission.session_id)
        intent = await self._tool_calls.get(admission.tool_call_id)
        if intent is None:
            raise EntityNotFoundError("ToolCallIntent", admission.tool_call_id)
        if intent.run_id != admission.run_id or intent.session_id != admission.session_id:
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

    async def _sync_intent(
        self,
        intent: ToolCallIntent,
        execution: Execution,
    ) -> ToolCallIntent:
        attempt_group = execution.attempt_group
        if attempt_group is None and execution.executor_type is ExecutorType.PTY:
            # Pre-claim PTY rows had a deterministic initial identity but did
            # not persist attempt_group.  The repository adopts and backfills
            # only a unique, otherwise exact legacy row.
            attempt_group = "initial"
        if attempt_group is None:
            return intent
        target = _intent_status(execution.status)
        expected = set(_INTENT_SYNC_EXPECTED[target])
        authoritative, projected = await self._tool_calls.project_execution_status(
            intent.id,
            execution_key=execution.execution_key,
            attempt_group=attempt_group,
            expected=expected,
            target=target,
        )
        if projected:
            return authoritative
        authoritative, adopted = await self._tool_calls.adopt_execution_claim(
            intent.id,
            execution_id=execution.id,
            execution_key=execution.execution_key,
            attempt_group=attempt_group,
        )
        if not adopted:
            return authoritative
        authoritative, _ = await self._tool_calls.project_execution_status(
            intent.id,
            execution_key=execution.execution_key,
            attempt_group=attempt_group,
            expected=expected,
            target=target,
        )
        return authoritative

    async def _settle_failed_submit(
        self,
        admission: ExecutionAdmissionIdentity,
        claim: ToolCallIntentExecutionClaim,
    ) -> None:
        admitted = await self._executions.find_admission(admission)
        if admitted is not None:
            _require_execution_matches_admission(admitted, admission)
            await self._sync_intent(claim.intent, admitted)
            return
        if not claim.newly_acquired:
            return
        authoritative, _ = await self._rollback_claim(claim, admission=admission)
        # An exact starter may have registered its row while settlement was
        # checking or rolling back. Re-read the complete identity in either
        # outcome and converge it; a foreign same-key row is never authoritative.
        admitted = await self._executions.find_admission(admission)
        if admitted is not None:
            _require_execution_matches_admission(admitted, admission)
            await self._sync_intent(authoritative, admitted)

    async def _rollback_claim(
        self,
        claim: ToolCallIntentExecutionClaim,
        *,
        admission: ExecutionAdmissionIdentity,
    ) -> tuple[ToolCallIntent, bool]:
        return await self._tool_calls.rollback_execution_claim(
            claim,
            admission=admission,
        )

    async def _append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        *,
        event_id: str | None = None,
    ) -> None:
        if self._events is not None:
            await self._events.append(run_id, event_type, payload, event_id=event_id)


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


def _require_execution_matches_admission(
    execution: Execution,
    admission: ExecutionAdmissionIdentity,
) -> None:
    mismatched: list[str] = []
    expected_fields: tuple[tuple[str, object, object], ...] = (
        ("execution_key", execution.execution_key, admission.execution_key),
        ("run_id", execution.run_id, admission.run_id),
        ("session_id", execution.session_id, admission.session_id),
        ("tool_call_id", execution.tool_call_id, admission.tool_call_id),
        ("attempt_group", execution.attempt_group, admission.attempt_group),
        ("node_id", execution.node_id, admission.node_id),
        ("executor_type", execution.executor_type, admission.executor_type),
        ("command_text", execution.command_text, admission.command_text),
        ("tool_id", execution.tool_id, admission.tool_id),
        ("tool_version", execution.tool_version, admission.tool_version),
        ("cwd", str(Path(execution.cwd).expanduser().resolve()), admission.cwd),
        ("env", execution.env_diff, admission.env),
    )
    mismatched.extend(
        field_name for field_name, persisted, requested in expected_fields if persisted != requested
    )
    if admission.execution_id is not None and execution.id != admission.execution_id:
        mismatched.append("execution_id")
    if admission.executor_type is ExecutorType.PROCESS and execution.argv != list(admission.argv):
        mismatched.append("argv")
    if (
        execution.launch_fingerprint is not None
        and execution.launch_fingerprint != admission.launch_fingerprint
    ):
        mismatched.append("launch_fingerprint")
    if not mismatched:
        return
    raise ApplicationConflictError(
        "execution_idempotency_conflict",
        f"Execution key {admission.execution_key!r} is already bound to a different launch",
        details={
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
            "mismatched_fields": sorted(set(mismatched)),
        },
    )


def _freeze_launch_request(
    launch: ExecutionLaunchRequest,
) -> ExecutionLaunchRequest:
    """Detach mutable inputs and resolve paths once at the submit boundary."""

    updates: dict[str, object] = {
        "cwd": launch.cwd.expanduser().resolve(),
    }
    if launch.shell_path is not None:
        updates["shell_path"] = launch.shell_path.expanduser().resolve()
    return launch.model_copy(update=updates, deep=True)


def _launch_admission_identity(
    launch: ExecutionLaunchRequest,
) -> ExecutionAdmissionIdentity:
    return ExecutionAdmissionIdentity(
        execution_key=launch.execution_key,
        run_id=launch.run_id,
        session_id=launch.session_id,
        tool_call_id=launch.tool_call_id,
        attempt_group=launch.attempt_group,
        executor_type=launch.executor_type,
        node_id=launch.node_id,
        argv=tuple(launch.argv),
        command_text=launch.command_text,
        tool_id=launch.tool_id,
        tool_version=launch.tool_version,
        cwd=str(launch.cwd),
        env=dict(launch.env),
        execution_id=launch.execution_id,
        launch_fingerprint=launch.launch_fingerprint,
    )


def _submitted_event_id(execution_key: str) -> str:
    digest = hashlib.sha256(execution_key.encode()).hexdigest()
    return f"execution-submitted:{digest[:44]}"
