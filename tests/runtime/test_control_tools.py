from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from riftx.application.errors import ApplicationConflictError
from riftx.domain import Artifact, Execution, ExecutorType
from riftx.execution import ExecutionWaitResult, ExecutionWaitStatus
from riftx.runtime.control_tools import RuntimeControlToolService
from riftx.runtime.engine.agent_factory import RuntimeToolScope


class FakeEvents:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, object]]] = []

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> object:
        self.rows.append((run_id, event_type, payload))
        return object()


class FakeTranscript:
    def __init__(self) -> None:
        self.rows: list[tuple[str, object]] = []

    async def append(self, session_id: str, draft: object) -> object:
        self.rows.append((session_id, draft))
        return object()


class FakeExecutions:
    def __init__(self, executions: list[Execution]) -> None:
        self.items = {execution.id: execution for execution in executions}
        self.wait_calls: list[tuple[str, dict[str, object]]] = []
        self.cancel_calls: list[tuple[str, str | None]] = []
        self.get_error: Exception | None = None

    async def get(self, execution_id: str) -> Execution:
        if self.get_error is not None:
            raise self.get_error
        return self.items[execution_id]

    async def wait(self, execution_id: str, **kwargs: object) -> ExecutionWaitResult:
        self.wait_calls.append((execution_id, kwargs))
        return ExecutionWaitResult(
            execution=self.items[execution_id],
            wait_status=ExecutionWaitStatus.WAIT_TIMEOUT,
            partial_output="bounded output",
            next_poll_after_seconds=10,
            stdout_cursor=14,
            stderr_cursor=0,
        )

    async def cancel(self, execution_id: str, reason: str | None = None) -> Execution:
        self.cancel_calls.append((execution_id, reason))
        return self.items[execution_id]


class FakeArtifacts:
    def __init__(self, artifacts: list[tuple[Artifact, Path]] = []) -> None:  # noqa: B006
        self.items = {artifact.id: (artifact, path) for artifact, path in artifacts}
        self.content_path_calls: list[str] = []

    async def get(self, artifact_id: str) -> Artifact:
        return self.items[artifact_id][0]

    async def content_path(self, artifact_id: str) -> tuple[Artifact, Path]:
        self.content_path_calls.append(artifact_id)
        return self.items[artifact_id]


def execution(
    execution_id: str = "execution-1",
    *,
    run_id: str = "run-1",
    session_id: str | None = "session-1",
) -> Execution:
    return Execution(
        id=execution_id,
        execution_key=f"key-{execution_id}",
        run_id=run_id,
        session_id=session_id,
        tool_call_id=f"call-{execution_id}",
        node_id="local",
        executor_type=ExecutorType.PROCESS,
        cwd="/workspace",
        stdout_path=f"/tmp/{execution_id}.stdout",
        stderr_path=f"/tmp/{execution_id}.stderr",
    )


def service(
    *,
    executions: FakeExecutions | None = None,
    artifacts: FakeArtifacts | None = None,
) -> tuple[RuntimeControlToolService, FakeEvents, FakeTranscript, FakeExecutions]:
    events = FakeEvents()
    transcript = FakeTranscript()
    execution_service = executions or FakeExecutions([execution()])
    control = RuntimeControlToolService(
        tools=object(),  # type: ignore[arg-type]
        executions=execution_service,  # type: ignore[arg-type]
        artifacts=artifacts or FakeArtifacts(),  # type: ignore[arg-type]
        events=events,
        transcript=transcript,  # type: ignore[arg-type]
    )
    return control, events, transcript, execution_service


SCOPE = RuntimeToolScope(run_id="run-1", session_id="session-1", agent_id="primary")


async def test_execution_controls_are_owned_by_exact_agent_session() -> None:
    sibling = execution("execution-child", session_id="session-child")
    control, events, transcript, execution_service = service(executions=FakeExecutions([sibling]))

    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            SCOPE,
            "cancel_execution",
            {"execution_id": sibling.id, "reason": "do not cross session"},
            "call-1",
        )

    assert captured.value.code == "execution_scope_mismatch"
    assert execution_service.cancel_calls == []
    assert transcript.rows == []
    assert events.rows[-1][1:] == (
        "runtime.control_tool_failed",
        {
            "session_id": "session-1",
            "agent_id": "primary",
            "tool": "cancel_execution",
            "tool_call_id": "call-1",
            "error_type": "ApplicationConflictError",
            "error_code": "execution_scope_mismatch",
        },
    )


async def test_unowned_legacy_execution_is_not_visible_to_agent_session() -> None:
    legacy = execution("execution-unowned", session_id=None)
    control, _, _, _ = service(executions=FakeExecutions([legacy]))

    with pytest.raises(ApplicationConflictError, match="Agent Session"):
        await control(
            SCOPE,
            "get_execution",
            {"execution_id": legacy.id},
            "call-legacy",
        )


async def test_failed_audit_does_not_copy_exception_or_argument_secrets() -> None:
    secret = "Bearer top-secret-model-token"
    execution_service = FakeExecutions([execution()])
    execution_service.get_error = RuntimeError(f"upstream body included {secret}")
    control, events, _, _ = service(executions=execution_service)

    with pytest.raises(RuntimeError, match="upstream body"):
        await control(
            SCOPE,
            "get_execution",
            {"execution_id": secret},
            "call-secret",
        )

    failed_payload = events.rows[-1][2]
    assert failed_payload["error_code"] == "internal_error"
    assert secret not in json.dumps(failed_payload)
    assert "message" not in failed_payload


async def test_cancel_reason_is_bounded_before_execution_mutation() -> None:
    control, events, _, execution_service = service()

    with pytest.raises(ValidationError):
        await control(
            SCOPE,
            "cancel_execution",
            {"execution_id": "execution-1", "reason": "x" * 1001},
            "call-long-reason",
        )

    assert execution_service.cancel_calls == []
    assert events.rows[-1][2]["error_type"] == "ValidationError"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "wait_execution",
            {"execution_id": "execution-1", "timeout_seconds": 30.01},
        ),
        (
            "wait_execution",
            {"execution_id": "execution-1", "max_bytes": 64 * 1024 + 1},
        ),
        (
            "read_artifact",
            {"artifact_id": "artifact-1", "max_bytes": 64 * 1024 + 1},
        ),
    ],
)
async def test_wait_and_artifact_reads_reject_unbounded_arguments(
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    control, _, _, execution_service = service()

    with pytest.raises(ValidationError):
        await control(SCOPE, tool_name, arguments, "call-unbounded")

    assert execution_service.wait_calls == []


async def test_same_run_artifact_is_shareable_but_cross_run_artifact_is_denied(
    tmp_path: Path,
) -> None:
    content = b"immutable shared evidence"
    path = tmp_path / "evidence.txt"
    path.write_bytes(content)
    same_run = Artifact(
        id="artifact-same-run",
        run_id="run-1",
        name=path.name,
        path=str(path),
        mime_type="text/plain",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )
    other_run = same_run.model_copy(update={"id": "artifact-other-run", "run_id": "run-2"})
    artifact_service = FakeArtifacts([(same_run, path), (other_run, path)])
    control, _, transcript, _ = service(artifacts=artifact_service)

    result = await control(
        SCOPE,
        "read_artifact",
        {"artifact_id": same_run.id, "max_bytes": 9},
        "call-artifact",
    )

    assert result["content"] == "immutable"
    assert result["next_offset"] == 9
    assert result["eof"] is False
    assert transcript.rows[0][0] == "session-1"

    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            SCOPE,
            "read_artifact",
            {"artifact_id": other_run.id},
            "call-other-run",
        )
    assert captured.value.code == "artifact_run_mismatch"
    assert artifact_service.content_path_calls == [same_run.id]


async def test_successful_control_result_is_transcripted_and_digest_audited() -> None:
    control, events, transcript, _ = service()

    result = await control(
        SCOPE,
        "get_execution",
        {"execution_id": "execution-1"},
        "call-get",
    )

    assert result["id"] == "execution-1"
    assert [event_type for _, event_type, _ in events.rows] == [
        "runtime.control_tool_started",
        "runtime.control_tool_completed",
    ]
    completed = events.rows[-1][2]
    assert completed["result_bytes"] > 0
    assert len(str(completed["result_sha256"])) == 64
    session_id, draft = transcript.rows[0]
    assert session_id == "session-1"
    assert draft.tool_call_id == "call-get"
    assert draft.execution_id == "execution-1"
    assert draft.structured_content["status"] == "completed"


async def test_complete_run_records_bounded_completion_request() -> None:
    control, events, _, _ = service()

    result = await control(
        SCOPE,
        "complete_run",
        {"run_summary": "Authorized objective completed."},
        "call-complete",
    )

    assert result == {
        "completion_requested": True,
        "run_summary": "Authorized objective completed.",
    }
    assert [event_type for _, event_type, _ in events.rows] == [
        "runtime.control_tool_started",
        "agent.completion_requested",
        "runtime.control_tool_completed",
    ]
