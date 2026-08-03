from __future__ import annotations

from pathlib import Path

import pytest

from riftx.application.errors import (
    ApplicationConflictError,
    ResourceNotAccessibleError,
)
from riftx.application.services import (
    ApprovalApplicationService,
    DecideApproval,
    ExecutionApplicationService,
)
from riftx.domain import (
    Approval,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
    RunKind,
)
from riftx.runner import ExecutionOutput, OutputSlice


def _audit_run(tmp_path: Path) -> Run:
    return Run(
        kind=RunKind.CODE_AUDIT,
        id="audit-run",
        engagement_id="engagement-1",
        node_id="local",
        objective=Objective(description="Generic effect fence"),
        workspace_path=str(tmp_path / "audit-output"),
    )


class _Runs:
    def __init__(self, run: Run) -> None:
        self.run = run

    async def get(self, run_id: str) -> Run | None:
        return self.run if run_id == self.run.id else None


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        del payload
        self.items.append((run_id, event_type))
        return object()


async def test_code_audit_execution_cancel_rejects_before_runner_and_event(
    tmp_path: Path,
) -> None:
    run = _audit_run(tmp_path)
    execution = Execution(
        id="audit-execution",
        execution_key="audit-execution-key",
        run_id=run.id,
        node_id=run.node_id,
        executor_type=ExecutorType.PROCESS,
        argv=["true"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "stdout"),
        stderr_path=str(tmp_path / "stderr"),
    )

    class _Executions:
        async def get(self, execution_id: str) -> Execution | None:
            return execution if execution_id == execution.id else None

    class _Runner:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        async def cancel(self, execution_id: str) -> Execution:
            self.cancelled.append(execution_id)
            return execution

    runner = _Runner()
    events = _Events()
    service = ExecutionApplicationService(
        run_repository=_Runs(run),  # type: ignore[arg-type]
        execution_repository=_Executions(),  # type: ignore[arg-type]
        event_repository=events,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.cancel(execution.id)

    assert captured.value.code == "run_kind_operation_unsupported"
    assert runner.cancelled == []
    assert events.items == []


async def test_code_audit_approval_decisions_reject_before_grant_event_or_workflow(
    tmp_path: Path,
) -> None:
    run = _audit_run(tmp_path)
    approval = Approval(
        id="audit-approval",
        run_id=run.id,
        tool_call_id="audit-tool-call",
        tool_name="shell",
    )

    class _Approvals:
        def __init__(self) -> None:
            self.decisions = 0

        async def get(self, approval_id: str) -> Approval | None:
            return approval if approval_id == approval.id else None

        async def decide_runtime(self, *_: object, **__: object) -> tuple[Approval, bool]:
            self.decisions += 1
            return approval, True

    class _Workflow:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def approve(self, run_id: str, approval_id: str) -> None:
            self.calls.append(f"approve:{run_id}:{approval_id}")

        async def reject(self, run_id: str, approval_id: str) -> None:
            self.calls.append(f"reject:{run_id}:{approval_id}")

    approvals = _Approvals()
    workflow = _Workflow()
    events = _Events()
    service = ApprovalApplicationService(
        approval_repository=approvals,  # type: ignore[arg-type]
        run_repository=_Runs(run),  # type: ignore[arg-type]
        event_repository=events,  # type: ignore[arg-type]
        workflow_client=workflow,
    )

    for operation in (
        service.approve(
            approval.id,
            DecideApproval(decided_by="operator", approve_for_run=True),
        ),
        service.reject(
            approval.id,
            DecideApproval(decided_by="operator", reason="reject"),
        ),
    ):
        with pytest.raises(ApplicationConflictError) as captured:
            await operation
        assert captured.value.code == "run_kind_operation_unsupported"

    assert approvals.decisions == 0
    assert events.items == []
    assert workflow.calls == []


async def test_execution_wait_rejects_same_run_foreign_execution_before_output(
    tmp_path: Path,
) -> None:
    run = _audit_run(tmp_path)
    execution = Execution(
        id="audit-execution",
        execution_key="audit-execution-key",
        run_id=run.id,
        node_id=run.node_id,
        executor_type=ExecutorType.PROCESS,
        argv=["true"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "stdout"),
        stderr_path=str(tmp_path / "stderr"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    foreign = execution.model_copy(update={"id": "same-run-foreign-execution"})

    class _Executions:
        async def get(self, execution_id: str) -> Execution | None:
            return execution if execution_id == execution.id else None

    class _Runner:
        def __init__(self) -> None:
            self.output_calls = 0

        async def wait(self, execution_id: str) -> Execution:
            assert execution_id == execution.id
            return foreign

        async def get(self, execution_id: str) -> Execution:
            assert execution_id == execution.id
            return foreign

        async def read_output(self, *_: object, **__: object) -> ExecutionOutput:
            self.output_calls += 1
            return ExecutionOutput(
                stdout=OutputSlice(data=b"secret", cursor=0, next_cursor=6, eof=True),
                stderr=OutputSlice(data=b"", cursor=0, next_cursor=0, eof=True),
            )

    runner = _Runner()
    events = _Events()
    service = ExecutionApplicationService(
        run_repository=_Runs(run),  # type: ignore[arg-type]
        execution_repository=_Executions(),  # type: ignore[arg-type]
        event_repository=events,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
    )

    with pytest.raises(ResourceNotAccessibleError) as captured:
        await service.wait(
            execution.id,
            timeout_seconds=1,
            expected_run_id=run.id,
        )

    assert captured.value.code == "resource_not_accessible"
    assert runner.output_calls == 0
    assert events.items == []
