from __future__ import annotations

import pytest
from temporalio.service import RPCError, RPCStatusCode

from riftx.application.errors import ApplicationConflictError, ServiceUnavailableError
from riftx.temporal.runtime import TemporalRunClient, TemporalRuntimeConfig


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
