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


class RuntimeCycleActivities:
    """Execute one idempotent Runtime Cycle outside Temporal Workflow History."""

    def __init__(self, coordinator: RuntimeCycleRunner, *, worker_id: str) -> None:
        self._coordinator = coordinator
        self._worker_id = worker_id

    @activity.defn(name="run_agent_cycle_activity")
    async def run_agent_cycle_activity(
        self,
        input: RunAgentCycleActivityInput,
    ) -> RunAgentCycleActivityResult:
        input_items: list[dict[str, object]] = []
        if input.completed_execution_id is not None:
            input_items.append(
                {
                    "type": "execution_completion",
                    "execution_id": input.completed_execution_id,
                    "source_refs": [f"execution://{input.completed_execution_id}"],
                }
            )
        if input.approval_id is not None:
            input_items.append(
                {
                    "type": "approval_decision",
                    "approval_id": input.approval_id,
                    "source_refs": [f"approval://{input.approval_id}"],
                }
            )
        result = await _await_with_heartbeats(
            self._coordinator.run_cycle(
                RunCycleRequest(
                    run_id=input.run_id,
                    session_id=input.session_id,
                    worker_id=self._worker_id,
                    cycle_id=input.cycle_id,
                    latest_user_message_id=input.latest_user_message_id,
                    input_items=input_items,
                )
            ),
            heartbeat_detail=f"runtime-cycle:{input.run_id}:{input.cycle_id}",
        )
        return RunAgentCycleActivityResult(
            run_id=result.run_id,
            session_id=result.session_id,
            cycle_id=result.cycle_id,
            yield_reason=RuntimeYieldReason(result.yield_reason.value),
            waiting_object_id=result.waiting_execution_id,
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
