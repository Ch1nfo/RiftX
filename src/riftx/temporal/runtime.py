"""Temporal Worker and Client adapters used by the control plane."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio.client import Client, WorkflowHandle
from temporalio.worker import Worker

from .activities import RiftXActivities
from .models import RunWorkflowInput, RunWorkflowResult, RunWorkflowStatus
from .workflow import RiftXRunWorkflow


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

    async def start_run(self, run_id: str) -> WorkflowHandle[RunWorkflowResult, RunWorkflowStatus]:
        return await self._client.start_workflow(
            RiftXRunWorkflow.run,
            RunWorkflowInput(run_id=run_id),
            id=self.workflow_id(run_id),
            task_queue=self._config.task_queue,
        )

    def get_handle(self, run_id: str) -> WorkflowHandle[RunWorkflowResult, RunWorkflowStatus]:
        return self._client.get_workflow_handle(self.workflow_id(run_id))

    async def pause(self, run_id: str) -> None:
        await self.get_handle(run_id).signal(RiftXRunWorkflow.pause)

    async def resume(self, run_id: str) -> None:
        await self.get_handle(run_id).signal(RiftXRunWorkflow.resume)

    async def approve(self, run_id: str, call_id: str) -> None:
        await self.get_handle(run_id).signal(RiftXRunWorkflow.approve, call_id)

    async def reject(self, run_id: str, call_id: str) -> None:
        await self.get_handle(run_id).signal(RiftXRunWorkflow.reject, call_id)

    async def cancel_current_execution(self, run_id: str) -> None:
        await self.get_handle(run_id).signal(RiftXRunWorkflow.cancel_current_execution)

    async def append_user_message(self, run_id: str, message: str) -> None:
        await self.get_handle(run_id).signal(RiftXRunWorkflow.append_user_message, message)

    async def status(self, run_id: str) -> RunWorkflowStatus:
        return await self.get_handle(run_id).query(RiftXRunWorkflow.get_status)

    def workflow_id(self, run_id: str) -> str:
        return f"{self._config.workflow_id_prefix}-{run_id}"


def create_worker(
    client: Client,
    activities: RiftXActivities,
    config: TemporalRuntimeConfig,
) -> Worker:
    return Worker(
        client,
        task_queue=config.task_queue,
        workflows=[RiftXRunWorkflow],
        activities=activities.registered(),
        max_concurrent_activities=config.max_concurrent_activities,
        max_cached_workflows=config.max_cached_workflows,
    )
