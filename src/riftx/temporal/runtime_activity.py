"""Temporal Activity adapter for the durable Runtime Coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Protocol

from temporalio import activity

from riftx.runtime.lifecycle import RunCycleRequest, RunCycleResult

from .models import (
    RunAgentCycleActivityInput,
    RunAgentCycleActivityResult,
    RuntimeYieldReason,
)


class RuntimeCycleRunner(Protocol):
    async def run_cycle(self, request: RunCycleRequest) -> RunCycleResult: ...


class RuntimeSessionInitializer(Protocol):
    async def ensure_primary_session(self, run_id: str, session_id: str) -> None: ...


class RuntimeUserInputResolver(Protocol):
    async def resolve_user_input(
        self,
        run_id: str,
        session_id: str,
        user_input_id: str,
    ) -> str: ...


class RuntimeExecutionInputResolver(Protocol):
    async def resolve_execution_input(
        self,
        run_id: str,
        execution_id: str,
    ) -> dict[str, object]: ...


class RuntimeCycleActivities:
    """Execute one idempotent Runtime Cycle outside Temporal Workflow History."""

    def __init__(
        self,
        coordinator: RuntimeCycleRunner,
        *,
        worker_id: str,
        session_initializer: RuntimeSessionInitializer | None = None,
        user_input_resolver: RuntimeUserInputResolver | None = None,
        execution_input_resolver: RuntimeExecutionInputResolver | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._worker_id = worker_id
        self._session_initializer = session_initializer
        self._user_input_resolver = user_input_resolver
        self._execution_input_resolver = execution_input_resolver

    @activity.defn(name="run_agent_cycle_activity")
    async def run_agent_cycle_activity(
        self,
        input: RunAgentCycleActivityInput,
    ) -> RunAgentCycleActivityResult:
        if self._session_initializer is not None:
            await self._session_initializer.ensure_primary_session(
                input.run_id,
                input.session_id,
            )
        latest_user_message_id = input.latest_user_message_id
        if latest_user_message_id is not None and self._user_input_resolver is not None:
            latest_user_message_id = await self._user_input_resolver.resolve_user_input(
                input.run_id,
                input.session_id,
                latest_user_message_id,
            )
        input_items: list[dict[str, object]] = []
        if input.completed_execution_id is not None:
            if self._execution_input_resolver is not None:
                input_items.append(
                    await self._execution_input_resolver.resolve_execution_input(
                        input.run_id,
                        input.completed_execution_id,
                    )
                )
            else:
                input_items.append(
                    {
                        "type": "execution_completion",
                        "execution_id": input.completed_execution_id,
                        "source_refs": [f"execution://{input.completed_execution_id}"],
                    }
                )
        result = await _await_with_heartbeats(
            self._coordinator.run_cycle(
                RunCycleRequest(
                    run_id=input.run_id,
                    session_id=input.session_id,
                    worker_id=self._worker_id,
                    cycle_id=input.cycle_id,
                    latest_user_message_id=latest_user_message_id,
                    approval_id=input.approval_id,
                    input_items=input_items,
                    # The Workflow owns final completion because another user
                    # message may already be queued while this Cycle is in
                    # flight. Completing the Run here would make that queued
                    # message impossible to execute in the next Cycle.
                    defer_run_completion=input.defer_run_completion,
                )
            ),
            heartbeat_detail=f"runtime-cycle:{input.run_id}:{input.cycle_id}",
        )
        return RunAgentCycleActivityResult(
            run_id=result.run_id,
            session_id=result.session_id,
            cycle_id=result.cycle_id,
            yield_reason=RuntimeYieldReason(result.yield_reason.value),
            waiting_object_id=result.waiting_object_id or result.waiting_execution_id,
            checkpoint_id=result.provider_state_id,
        )

    def registered(self) -> list[object]:
        return [self.run_agent_cycle_activity]


async def _await_with_heartbeats[ResultT](
    awaitable: Awaitable[ResultT],
    *,
    heartbeat_detail: str,
    interval_seconds: float = 20.0,
) -> ResultT:
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval_seconds)
            if done:
                return await task
            _heartbeat(heartbeat_detail)
    finally:
        if not task.done():
            task.cancel()


def _heartbeat(detail: str) -> None:
    try:
        activity.heartbeat(detail)
    except RuntimeError:
        # Direct Activity unit tests do not install a Temporal activity context.
        pass
