"""Temporal Worker and Client adapters used by the control plane."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from riftx.application.errors import ApplicationConflictError, ServiceUnavailableError

from .activities import RiftXActivities
from .models import RunWorkflowInput, RunWorkflowResult, RunWorkflowStatus
from .runtime_activity import RuntimeCycleActivities
from .workflow import RiftXRunWorkflow

ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class TemporalRuntimeConfig:
    task_queue: str = "riftx-v2"
    workflow_id_prefix: str = "riftx-run"
    max_concurrent_activities: int = 20
    max_cached_workflows: int = 1000


class TemporalRunClient:
    def __init__(self, client: Client, config: TemporalRuntimeConfig) -> None:
        self._client = client
        self._config = config

    async def start_run(self, run_id: str) -> WorkflowHandle[RiftXRunWorkflow, RunWorkflowResult]:
        return await self._invoke(
            run_id,
            "start",
            lambda: self._client.start_workflow(
                RiftXRunWorkflow.run,
                RunWorkflowInput(run_id=run_id, await_initial_instruction=True),
                id=self.workflow_id(run_id),
                task_queue=self._config.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            ),
        )

    def get_handle(self, run_id: str) -> WorkflowHandle[RiftXRunWorkflow, RunWorkflowResult]:
        return self._client.get_workflow_handle(self.workflow_id(run_id))

    async def pause(self, run_id: str) -> None:
        await self._invoke(
            run_id,
            "pause",
            lambda: self.get_handle(run_id).signal(RiftXRunWorkflow.pause),
        )

    async def resume(self, run_id: str) -> None:
        await self._invoke(
            run_id,
            "resume",
            lambda: self.get_handle(run_id).signal(RiftXRunWorkflow.resume),
        )

    async def approve(self, run_id: str, approval_id: str) -> None:
        await self._invoke(
            run_id,
            "approve",
            lambda: self.get_handle(run_id).signal(RiftXRunWorkflow.approve, approval_id),
        )

    async def reject(self, run_id: str, approval_id: str) -> None:
        await self._invoke(
            run_id,
            "reject",
            lambda: self.get_handle(run_id).signal(RiftXRunWorkflow.reject, approval_id),
        )

    async def execution_completed(self, run_id: str, execution_id: str) -> None:
        await self._invoke(
            run_id,
            "report execution completion",
            lambda: self.get_handle(run_id).signal(
                RiftXRunWorkflow.execution_completed,
                execution_id,
            ),
        )

    async def user_input(self, run_id: str, message_id: str) -> None:
        await self._invoke(
            run_id,
            "send user input",
            # Signal-with-start removes the race between checking for a
            # Workflow and starting/signalling it. USE_EXISTING makes every
            # later message target the same open execution, while
            # REJECT_DUPLICATE ensures a closed Run can never be restarted
            # under the same durable Workflow ID.
            lambda: self._client.start_workflow(
                RiftXRunWorkflow.run,
                RunWorkflowInput(run_id=run_id, await_initial_instruction=True),
                id=self.workflow_id(run_id),
                task_queue=self._config.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                start_signal="user_input",
                start_signal_args=[message_id],
            ),
        )

    async def cancel_current_execution(self, run_id: str) -> None:
        await self._invoke(
            run_id,
            "cancel current execution",
            lambda: self.get_handle(run_id).signal(RiftXRunWorkflow.cancel_current_execution),
        )

    async def cancel(self, run_id: str) -> None:
        await self._invoke(
            run_id,
            "cancel",
            lambda: self.get_handle(run_id).signal(RiftXRunWorkflow.cancel),
        )

    async def compact(self, run_id: str, max_history_items: int = 100) -> None:
        await self._invoke(
            run_id,
            "compact",
            lambda: self.get_handle(run_id).signal(RiftXRunWorkflow.compact, max_history_items),
        )

    async def switch_model(self, run_id: str, model_profile: str) -> None:
        await self._invoke(
            run_id,
            "switch model",
            lambda: self.get_handle(run_id).signal(RiftXRunWorkflow.switch_model, model_profile),
        )

    async def append_user_message(self, run_id: str, message_id: str) -> None:
        await self.user_input(run_id, message_id)

    async def status(self, run_id: str) -> RunWorkflowStatus:
        return await self._invoke(
            run_id,
            "query status",
            lambda: self.get_handle(run_id).query(RiftXRunWorkflow.get_status),
        )

    def workflow_id(self, run_id: str) -> str:
        return f"{self._config.workflow_id_prefix}-{run_id}"

    async def _invoke(
        self,
        run_id: str,
        action: str,
        operation: Callable[[], Awaitable[ResultT]],
    ) -> ResultT:
        try:
            return await operation()
        except WorkflowAlreadyStartedError as exc:
            raise ApplicationConflictError(
                "workflow_already_closed",
                f"Could not {action} because this Run's Workflow has already closed",
                details={
                    "run_id": run_id,
                    "workflow_id": self.workflow_id(run_id),
                    "temporal_run_id": exc.run_id,
                },
            ) from exc
        except RPCError as exc:
            details: dict[str, object] = {
                "run_id": run_id,
                "workflow_id": self.workflow_id(run_id),
                "rpc_status": exc.status.name.lower(),
                "reason": exc.message,
            }
            if exc.status in {
                RPCStatusCode.UNAVAILABLE,
                RPCStatusCode.DEADLINE_EXCEEDED,
            }:
                raise ServiceUnavailableError(
                    "temporal_unavailable",
                    f"Could not {action} because Temporal is unavailable",
                    details=details,
                ) from exc
            if exc.status is RPCStatusCode.NOT_FOUND:
                raise ApplicationConflictError(
                    "workflow_not_running",
                    f"Could not {action} because the Workflow is no longer running",
                    details=details,
                ) from exc
            raise ApplicationConflictError(
                "workflow_control_failed",
                f"Could not {action} because Temporal rejected the Workflow request",
                details=details,
            ) from exc


class LazyTemporalRunClient:
    """Connect on first control request and retry after connection outages.

    Run creation only needs :meth:`workflow_id`, so constructing this adapter
    never contacts Temporal. A failed connection is intentionally not cached;
    the next message or control request gets a fresh connection attempt.
    """

    def __init__(
        self,
        connector: Callable[[], Awaitable[Client]],
        config: TemporalRuntimeConfig,
    ) -> None:
        self._connector = connector
        self._config = config
        self._client: TemporalRunClient | None = None
        self._connect_lock = asyncio.Lock()

    async def start_run(self, run_id: str) -> object:
        return await self._invoke(run_id, "start", lambda client: client.start_run(run_id))

    async def pause(self, run_id: str) -> None:
        await self._invoke(run_id, "pause", lambda client: client.pause(run_id))

    async def resume(self, run_id: str) -> None:
        await self._invoke(run_id, "resume", lambda client: client.resume(run_id))

    async def approve(self, run_id: str, approval_id: str) -> None:
        await self._invoke(
            run_id,
            "approve",
            lambda client: client.approve(run_id, approval_id),
        )

    async def reject(self, run_id: str, approval_id: str) -> None:
        await self._invoke(
            run_id,
            "reject",
            lambda client: client.reject(run_id, approval_id),
        )

    async def execution_completed(self, run_id: str, execution_id: str) -> None:
        await self._invoke(
            run_id,
            "report execution completion",
            lambda client: client.execution_completed(run_id, execution_id),
        )

    async def user_input(self, run_id: str, message_id: str) -> None:
        await self._invoke(
            run_id,
            "send user input",
            lambda client: client.user_input(run_id, message_id),
        )

    async def cancel_current_execution(self, run_id: str) -> None:
        await self._invoke(
            run_id,
            "cancel current execution",
            lambda client: client.cancel_current_execution(run_id),
        )

    async def cancel(self, run_id: str) -> None:
        await self._invoke(run_id, "cancel", lambda client: client.cancel(run_id))

    async def compact(self, run_id: str, max_history_items: int = 100) -> None:
        await self._invoke(
            run_id,
            "compact",
            lambda client: client.compact(run_id, max_history_items),
        )

    async def switch_model(self, run_id: str, model_profile: str) -> None:
        await self._invoke(
            run_id,
            "switch model",
            lambda client: client.switch_model(run_id, model_profile),
        )

    async def append_user_message(self, run_id: str, message_id: str) -> None:
        await self.user_input(run_id, message_id)

    async def status(self, run_id: str) -> RunWorkflowStatus:
        return await self._invoke(
            run_id,
            "query status",
            lambda client: client.status(run_id),
        )

    def workflow_id(self, run_id: str) -> str:
        return f"{self._config.workflow_id_prefix}-{run_id}"

    async def _invoke(
        self,
        run_id: str,
        action: str,
        operation: Callable[[TemporalRunClient], Awaitable[ResultT]],
    ) -> ResultT:
        client = await self._get_client(run_id, action)
        try:
            return await operation(client)
        except ServiceUnavailableError:
            # Temporal's Client normally reconnects by itself. Discarding it on
            # a classified transport outage also lets a later request recover
            # from a stale or otherwise unusable client object. We do not retry
            # the current signal because the server may already have accepted
            # it; the caller receives an honest structured error instead.
            async with self._connect_lock:
                if self._client is client:
                    self._client = None
            raise

    async def _get_client(self, run_id: str, action: str) -> TemporalRunClient:
        if self._client is not None:
            return self._client
        async with self._connect_lock:
            if self._client is not None:
                return self._client
            try:
                temporal_client = await self._connector()
            except Exception as exc:
                raise ServiceUnavailableError(
                    "temporal_unavailable",
                    f"Could not {action} because Temporal is unavailable",
                    details={
                        "run_id": run_id,
                        "workflow_id": self.workflow_id(run_id),
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                    },
                ) from exc
            self._client = TemporalRunClient(temporal_client, self._config)
            return self._client


def create_worker(
    client: Client,
    activities: RiftXActivities,
    config: TemporalRuntimeConfig,
    *,
    runtime_cycle_activities: RuntimeCycleActivities | None = None,
) -> Worker:
    registered = activities.registered(
        include_runtime_cycle_compat=runtime_cycle_activities is None
    )
    if runtime_cycle_activities is not None:
        registered.extend(runtime_cycle_activities.registered())
    return Worker(
        client,
        task_queue=config.task_queue,
        workflows=[RiftXRunWorkflow],
        activities=cast("list[Callable[..., Any]]", registered),
        max_concurrent_activities=config.max_concurrent_activities,
        max_cached_workflows=config.max_cached_workflows,
    )
