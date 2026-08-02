"""Bounded, non-destructive waits for durable Executions."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from riftx.domain import Execution, ExecutionStatus
from riftx.runner import ExecutionOutput, ExecutionRunner

from .models import ExecutionWaitResult, ExecutionWaitStatus

_TERMINAL_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
    ExecutionStatus.LOST,
}


async def wait_for_execution(
    runner: ExecutionRunner,
    execution: Execution,
    *,
    timeout_seconds: float,
    stdout_cursor: int = 0,
    stderr_cursor: int = 0,
    max_bytes: int = 64 * 1024,
    next_poll_after_seconds: int = 10,
) -> ExecutionWaitResult:
    """Wait for one terminal state without mutating it when only the wait expires."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if next_poll_after_seconds <= 0:
        raise ValueError("next_poll_after_seconds must be positive")

    wait_timed_out = False
    if execution.status not in _TERMINAL_STATUSES:
        waiter = asyncio.create_task(
            runner.wait(execution.id),
            name=f"riftx-wait-execution-{execution.id}",
        )
        done, _ = await asyncio.wait({waiter}, timeout=timeout_seconds)
        if done:
            execution = waiter.result()
        else:
            wait_timed_out = True
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter
            execution = await runner.get(execution.id)

    output = await runner.read_output(
        execution.id,
        stdout_cursor=stdout_cursor,
        stderr_cursor=stderr_cursor,
        max_bytes=max_bytes,
    )
    return ExecutionWaitResult(
        execution=execution,
        wait_status=_wait_status(execution, wait_timed_out=wait_timed_out),
        partial_output=_partial_output(output),
        next_poll_after_seconds=(
            next_poll_after_seconds
            if wait_timed_out and execution.status not in _TERMINAL_STATUSES
            else None
        ),
        stdout_cursor=output.stdout.next_cursor,
        stderr_cursor=output.stderr.next_cursor,
    )


def _wait_status(
    execution: Execution, *, wait_timed_out: bool
) -> ExecutionWaitStatus:
    if execution.status is ExecutionStatus.CANCELLED:
        return ExecutionWaitStatus.EXECUTION_CANCELLED
    if execution.status is ExecutionStatus.LOST:
        return ExecutionWaitStatus.EXECUTION_LOST
    if execution.status in _TERMINAL_STATUSES:
        return ExecutionWaitStatus.EXECUTION_COMPLETED
    if wait_timed_out:
        return ExecutionWaitStatus.WAIT_TIMEOUT
    return ExecutionWaitStatus.WAIT_TIMEOUT


def _partial_output(output: ExecutionOutput) -> str | None:
    chunks = [
        output.stdout.data.decode("utf-8", errors="replace"),
        output.stderr.data.decode("utf-8", errors="replace"),
    ]
    combined = "".join(chunks)
    return combined or None
