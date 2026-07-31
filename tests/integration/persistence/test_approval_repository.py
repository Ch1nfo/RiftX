from pathlib import Path

import pytest

from riftx.application.errors import RepositoryConflictError
from riftx.domain import (
    Approval,
    ApprovalStatus,
    Engagement,
    Objective,
    Run,
    RunStatus,
    ToolCall,
)
from riftx.persistence import (
    Database,
    SQLAlchemyApprovalRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
)


async def test_approval_repository_is_idempotent_and_persists_run_grants(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Approval repository")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Approval test"),
            workspace_path=str(tmp_path),
        )
    )
    repository = SQLAlchemyApprovalRepository(database.session_factory)
    tool_call = ToolCall(
        id="tool-call-1",
        sdk_call_id="sdk-call-1",
        run_id="run-1",
        agent_step_id="step-1",
        tool_id="scanner",
        arguments={"args": ["--safe"]},
    )
    approval = Approval(
        id="approval-1",
        run_id="run-1",
        tool_call_id=tool_call.id,
        tool_name="scanner",
        command=["scanner", "--safe"],
        cwd=str(tmp_path),
        target_summary="domain:example.test",
        env_diff={"TOKEN": None},
        reason="The Agent needs to validate the target.",
    )

    created, changed = await repository.create_request(tool_call, approval)
    duplicate, duplicate_changed = await repository.create_request(tool_call, approval)

    assert changed is True
    assert duplicate_changed is False
    assert duplicate.id == created.id
    listed = await repository.list("run-1", status=ApprovalStatus.PENDING)
    assert [item.id for item in listed] == ["approval-1"]
    assert listed[0].command == ["scanner", "--safe"]
    assert listed[0].env_diff == {"TOKEN": None}

    decided, decision_changed = await repository.decide(
        "approval-1",
        ApprovalStatus.APPROVED,
        decided_by="tester",
    )
    repeated, repeated_changed = await repository.decide(
        "approval-1",
        ApprovalStatus.APPROVED,
        decided_by="tester",
    )
    assert decision_changed is True
    assert repeated_changed is False
    assert decided.status is ApprovalStatus.APPROVED
    assert repeated.status is ApprovalStatus.APPROVED
    persisted_call = await repository.get_tool_call("tool-call-1")
    assert persisted_call is not None
    assert persisted_call.approval_status is ApprovalStatus.APPROVED

    first_grant = await repository.grant_for_run("run-1", "scanner", created_by="tester")
    second_grant = await repository.grant_for_run("run-1", "scanner", created_by="tester")
    assert first_grant.id == second_grant.id
    assert await repository.is_granted("run-1", "scanner")

    await database.dispose()


@pytest.mark.parametrize(
    "fence_status",
    [RunStatus.PAUSING, RunStatus.CANCELLING, RunStatus.COMPLETING],
)
async def test_approval_decision_is_atomically_blocked_by_run_safety_fence(
    tmp_path: Path,
    fence_status: RunStatus,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'approval-fence.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Approval fence")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Approval fence test"),
            workspace_path=str(tmp_path),
        )
    )
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    repository = SQLAlchemyApprovalRepository(database.session_factory)
    tool_call = ToolCall(
        id="tool-call-1",
        sdk_call_id="sdk-call-1",
        run_id="run-1",
        agent_step_id="step-1",
        tool_id="scanner",
    )
    approval = Approval(
        id="approval-1",
        run_id="run-1",
        tool_call_id=tool_call.id,
        tool_name="scanner",
    )
    await repository.create_request(tool_call, approval)
    await runs.update_status("run-1", fence_status)

    with pytest.raises(RepositoryConflictError, match=fence_status.value):
        await repository.decide(
            approval.id,
            ApprovalStatus.APPROVED,
            decided_by="operator",
            blocked_run_statuses={
                RunStatus.PAUSING,
                RunStatus.CANCELLING,
                RunStatus.COMPLETING,
            },
        )

    persisted = await repository.get(approval.id)
    persisted_call = await repository.get_tool_call(tool_call.id)
    assert persisted is not None and persisted.status is ApprovalStatus.PENDING
    assert persisted_call is not None and persisted_call.approval_status is ApprovalStatus.PENDING
    await database.dispose()
