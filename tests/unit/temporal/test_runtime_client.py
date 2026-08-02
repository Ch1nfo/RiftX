from __future__ import annotations

import pytest
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from riftx.application.errors import ApplicationConflictError, ServiceUnavailableError
from riftx.temporal.models import RunWorkflowInput
from riftx.temporal.runtime import (
    LazyTemporalRunClient,
    TemporalRunClient,
    TemporalRuntimeConfig,
)


class FailingWorkflowHandle:
    def __init__(self, status: RPCStatusCode) -> None:
        self.status = status

    async def signal(self, *_: object) -> None:
        raise RPCError("test workflow control failure", self.status, b"")


class FailingTemporalClient:
    def __init__(self, status: RPCStatusCode) -> None:
        self.handle = FailingWorkflowHandle(status)

    def get_workflow_handle(self, _: str) -> FailingWorkflowHandle:
        return self.handle


class RecordingTemporalClient:
    def __init__(self) -> None:
        self.args: tuple[object, ...] | None = None
        self.kwargs: dict[str, object] | None = None

    async def start_workflow(self, *args: object, **kwargs: object) -> object:
        self.args = args
        self.kwargs = kwargs
        return object()


class UnavailableStartTemporalClient:
    async def start_workflow(self, *_: object, **__: object) -> object:
        raise RPCError("Temporal transport unavailable", RPCStatusCode.UNAVAILABLE, b"")


async def test_start_run_waits_for_the_first_user_instruction() -> None:
    temporal = RecordingTemporalClient()
    client = TemporalRunClient(
        temporal,  # type: ignore[arg-type]
        TemporalRuntimeConfig(task_queue="test-queue", workflow_id_prefix="test-run"),
    )

    await client.start_run("run-1")

    assert temporal.args is not None
    assert temporal.args[1] == RunWorkflowInput(
        run_id="run-1",
        await_initial_instruction=True,
    )
    assert temporal.kwargs == {
        "id": "test-run-run-1",
        "task_queue": "test-queue",
        "id_reuse_policy": WorkflowIDReusePolicy.REJECT_DUPLICATE,
        "id_conflict_policy": WorkflowIDConflictPolicy.USE_EXISTING,
    }


async def test_user_message_uses_signal_with_start_and_never_restarts_closed_workflow() -> None:
    temporal = RecordingTemporalClient()
    client = TemporalRunClient(
        temporal,  # type: ignore[arg-type]
        TemporalRuntimeConfig(task_queue="test-queue", workflow_id_prefix="test-run"),
    )

    await client.append_user_message("run-1", "message-1")

    assert temporal.args is not None
    assert temporal.args[1] == RunWorkflowInput(
        run_id="run-1",
        await_initial_instruction=True,
    )
    assert temporal.kwargs == {
        "id": "test-run-run-1",
        "task_queue": "test-queue",
        "id_reuse_policy": WorkflowIDReusePolicy.REJECT_DUPLICATE,
        "id_conflict_policy": WorkflowIDConflictPolicy.USE_EXISTING,
        "start_signal": "user_input",
        "start_signal_args": ["message-1"],
    }


class AlreadyClosedTemporalClient:
    async def start_workflow(self, *_: object, **__: object) -> object:
        raise WorkflowAlreadyStartedError(
            "test-run-run-1",
            "RiftXRunWorkflow",
            run_id="closed-temporal-run",
        )


async def test_signal_with_start_classifies_closed_workflow_without_restarting_it() -> None:
    client = TemporalRunClient(
        AlreadyClosedTemporalClient(),  # type: ignore[arg-type]
        TemporalRuntimeConfig(task_queue="test-queue", workflow_id_prefix="test-run"),
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await client.append_user_message("run-1", "message-1")

    assert captured.value.code == "workflow_already_closed"
    assert captured.value.details == {
        "run_id": "run-1",
        "workflow_id": "test-run-run-1",
        "temporal_run_id": "closed-temporal-run",
    }


async def test_lazy_client_retries_connection_after_initial_temporal_outage() -> None:
    temporal = RecordingTemporalClient()
    attempts = 0

    async def connect() -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("Temporal is starting")
        return temporal

    client = LazyTemporalRunClient(
        connect,  # type: ignore[arg-type]
        TemporalRuntimeConfig(task_queue="test-queue", workflow_id_prefix="test-run"),
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        await client.append_user_message("run-1", "message-1")

    assert captured.value.code == "temporal_unavailable"
    assert captured.value.details == {
        "run_id": "run-1",
        "workflow_id": "test-run-run-1",
        "error_type": "ConnectionError",
        "reason": "Temporal is starting",
    }

    await client.append_user_message("run-1", "message-2")

    assert attempts == 2
    assert temporal.kwargs is not None
    assert temporal.kwargs["start_signal_args"] == ["message-2"]


async def test_lazy_client_discards_rpc_failed_client_and_reconnects_on_retry() -> None:
    recovered_temporal = RecordingTemporalClient()
    temporal_clients = [UnavailableStartTemporalClient(), recovered_temporal]
    attempts = 0

    async def connect() -> object:
        nonlocal attempts
        client = temporal_clients[attempts]
        attempts += 1
        return client

    client = LazyTemporalRunClient(
        connect,  # type: ignore[arg-type]
        TemporalRuntimeConfig(task_queue="test-queue", workflow_id_prefix="test-run"),
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        await client.append_user_message("run-1", "message-1")

    assert captured.value.code == "temporal_unavailable"
    await client.append_user_message("run-1", "message-2")

    assert attempts == 2
    assert recovered_temporal.kwargs is not None
    assert recovered_temporal.kwargs["start_signal_args"] == ["message-2"]


@pytest.mark.parametrize(
    ("status", "error_type", "error_code"),
    [
        (RPCStatusCode.UNAVAILABLE, ServiceUnavailableError, "temporal_unavailable"),
        (RPCStatusCode.DEADLINE_EXCEEDED, ServiceUnavailableError, "temporal_unavailable"),
        (RPCStatusCode.NOT_FOUND, ApplicationConflictError, "workflow_not_running"),
        (RPCStatusCode.FAILED_PRECONDITION, ApplicationConflictError, "workflow_control_failed"),
    ],
)
async def test_temporal_control_errors_are_classified(
    status: RPCStatusCode,
    error_type: type[ApplicationConflictError] | type[ServiceUnavailableError],
    error_code: str,
) -> None:
    client = TemporalRunClient(
        FailingTemporalClient(status),  # type: ignore[arg-type]
        TemporalRuntimeConfig(workflow_id_prefix="test-run"),
    )

    with pytest.raises(error_type) as captured:
        await client.pause("run-1")

    assert captured.value.code == error_code
    assert captured.value.details == {
        "run_id": "run-1",
        "workflow_id": "test-run-run-1",
        "rpc_status": status.name.lower(),
        "reason": "test workflow control failure",
    }
