import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from riftx.domain import (
    AgentCheckpoint,
    AgentMessage,
    AgentStep,
    Approval,
    Artifact,
    Engagement,
    EntryPoint,
    EntryPointKind,
    Execution,
    ExecutorType,
    Finding,
    FindingEvidence,
    FindingSeverity,
    MessageRole,
    MessageType,
    Node,
    Objective,
    PentestAdmission,
    PentestBudget,
    PentestProhibitedAction,
    PentestStopCondition,
    Report,
    ReportFormat,
    Run,
    RunEvent,
    RunKind,
    Scope,
    Skill,
    SuccessCriterion,
    TerminalSession,
    Tool,
    ToolCall,
    ToolState,
)


def representative_models() -> list[object]:
    run = Run(
        kind="general",
        id="run-1",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="Verify the authorized service"),
        success_criteria=[SuccessCriterion(description="Produce evidence")],
        entry_points=[EntryPoint(kind=EntryPointKind.URL, value="https://example.test")],
        scope=Scope(domains=["example.test"]),
        workspace_path="/tmp/riftx/run-1",
    )
    return [
        Engagement(id="engagement-1", name="Authorized assessment"),
        run,
        Node(id="node-1", name="local", platform="darwin", architecture="arm64"),
        Tool(id="printf", name="printf", command=["printf"]),
        ToolState(tool_id="printf", node_id="node-1"),
        Skill(id="shell", name="Shell", description="Run an approved command"),
        AgentStep(id="step-1", run_id=run.id, sequence=1),
        ToolCall(
            id="call-1",
            sdk_call_id="sdk-call-1",
            run_id=run.id,
            agent_step_id="step-1",
            tool_id="printf",
            arguments={"args": ["ok"]},
        ),
        Execution(
            id="execution-1",
            execution_key="key-1",
            run_id=run.id,
            node_id="node-1",
            executor_type=ExecutorType.PROCESS,
            argv=["printf", "ok"],
            cwd="/tmp/riftx/run-1",
            stdout_path="/tmp/riftx/run-1/stdout.log",
            stderr_path="/tmp/riftx/run-1/stderr.log",
        ),
        TerminalSession(run_id=run.id, execution_id="execution-1"),
        Approval(run_id=run.id, tool_call_id="call-1"),
        Artifact(
            id="artifact-1",
            run_id=run.id,
            execution_id="execution-1",
            name="stdout",
            path="stdout.log",
            mime_type="text/plain",
            sha256="a" * 64,
            size=2,
        ),
        Finding(
            id="finding-1",
            run_id=run.id,
            title="Example finding",
            severity=FindingSeverity.INFO,
            evidence=[FindingEvidence(artifact_id="artifact-1")],
        ),
        Report(
            run_id=run.id,
            format=ReportFormat.MARKDOWN,
            artifact_id="artifact-1",
            finding_ids=["finding-1"],
        ),
        RunEvent(
            run_id=run.id,
            sequence=1,
            event_type="run.created",
            payload={"status": "created"},
        ),
        AgentMessage(
            run_id=run.id,
            session_id="session-1",
            agent_id="primary",
            role=MessageRole.USER,
            message_type=MessageType.USER_MESSAGE,
            content="Start",
            sequence=1,
        ),
        AgentCheckpoint(run_id=run.id, sdk_state={"cursor": 1}),
    ]


@pytest.mark.parametrize("model", representative_models())
def test_domain_models_are_json_serializable(model: object) -> None:
    payload = json.loads(model.model_dump_json())  # type: ignore[attr-defined]
    assert isinstance(payload, dict)


def test_scope_normalizes_duplicate_and_blank_values() -> None:
    scope = Scope(domains=[" example.test ", "", "example.test", "api.example.test"])
    assert scope.domains == ["example.test", "api.example.test"]


def test_scope_rejects_inverted_time_range() -> None:
    starts_at = datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(ValidationError, match="starts_at must be earlier"):
        Scope(starts_at=starts_at, ends_at=starts_at - timedelta(seconds=1))


def test_run_kind_is_required_strict_and_immutable() -> None:
    required = {
        "engagement_id": "engagement-1",
        "node_id": "node-1",
        "objective": Objective(description="Classify the run"),
        "workspace_path": "/tmp/riftx/run-kind",
    }

    with pytest.raises(ValidationError, match="kind"):
        Run(**required)

    for invalid in ("agent", "audit", "unknown", " general "):
        with pytest.raises(ValidationError, match="kind"):
            Run(kind=invalid, **required)

    run = Run(kind=RunKind.CODE_AUDIT, **required)

    assert run.kind is RunKind.CODE_AUDIT
    assert json.loads(run.model_dump_json())["kind"] == "code_audit"
    with pytest.raises(ValidationError, match="frozen"):
        run.kind = RunKind.GENERAL


def _pentest_admission() -> PentestAdmission:
    return PentestAdmission(
        budget=PentestBudget(
            max_duration_seconds=3600,
            max_model_calls=100,
            max_tokens=100_000,
            max_tool_calls=200,
            max_target_interactions=50,
            max_concurrent_target_interactions=2,
        )
    )


def test_pentest_run_requires_admission_network_scope_and_entry_point() -> None:
    required = {
        "kind": RunKind.PENTEST,
        "engagement_id": "engagement-1",
        "node_id": "node-1",
        "objective": Objective(description="Assess the authorized target"),
        "workspace_path": "/tmp/riftx/pentest",
    }

    with pytest.raises(ValidationError, match="require pentest admission"):
        Run(**required)
    with pytest.raises(ValidationError, match="positive network scope"):
        Run(pentest_admission=_pentest_admission(), **required)
    with pytest.raises(ValidationError, match="network entry point"):
        Run(
            pentest_admission=_pentest_admission(),
            scope=Scope(domains=["example.test"]),
            **required,
        )
    with pytest.raises(ValidationError, match="non-empty network targets"):
        Run(
            pentest_admission=_pentest_admission(),
            scope=Scope(domains=["example.test"]),
            entry_points=[EntryPoint(kind=EntryPointKind.FILE, value="/tmp/target")],
            **required,
        )

    run = Run(
        pentest_admission=_pentest_admission(),
        scope=Scope(domains=["example.test"]),
        entry_points=[EntryPoint(kind=EntryPointKind.DOMAIN, value="example.test")],
        **required,
    )
    assert run.kind is RunKind.PENTEST


def test_non_pentest_run_rejects_pentest_admission() -> None:
    with pytest.raises(ValidationError, match="only Pentest Runs"):
        Run(
            kind=RunKind.GENERAL,
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="General work"),
            workspace_path="/tmp/riftx/general",
            pentest_admission=_pentest_admission(),
        )


def test_pentest_admission_requires_bounded_budget_and_mandatory_safety_rules() -> None:
    budget = _pentest_admission().budget

    with pytest.raises(ValidationError, match="greater than 0"):
        PentestBudget(
            max_duration_seconds=0,
            max_model_calls=1,
            max_tokens=1,
            max_tool_calls=1,
            max_target_interactions=1,
            max_concurrent_target_interactions=1,
        )
    with pytest.raises(ValidationError, match="must not exceed"):
        PentestBudget(
            max_duration_seconds=1,
            max_model_calls=1,
            max_tokens=1,
            max_tool_calls=1,
            max_target_interactions=1,
            max_concurrent_target_interactions=2,
        )
    with pytest.raises(ValidationError, match="required prohibited actions"):
        PentestAdmission(
            budget=budget,
            prohibited_actions=[PentestProhibitedAction.DENIAL_OF_SERVICE],
        )
    with pytest.raises(ValidationError, match="required stop conditions"):
        PentestAdmission(
            budget=budget,
            stop_conditions=[PentestStopCondition.OPERATOR_STOP],
        )


def test_run_model_profile_matches_its_database_bound() -> None:
    required = {
        "kind": RunKind.CODE_AUDIT,
        "engagement_id": "engagement-1",
        "node_id": "node-1",
        "objective": Objective(description="Bind the model profile"),
        "workspace_path": "/tmp/riftx/run-model-profile",
    }

    assert len(Run(model_profile="m" * 255, **required).model_profile or "") == 255
    with pytest.raises(ValidationError, match="at most 255"):
        Run(model_profile="m" * 256, **required)


def test_tool_rejects_empty_command() -> None:
    with pytest.raises(ValidationError, match="tool command"):
        Tool(id="broken", name="Broken", command=[])


def test_event_type_must_be_namespaced() -> None:
    with pytest.raises(ValidationError):
        RunEvent(run_id="run-1", sequence=1, event_type="created")


def test_execution_creation_time_is_immutable() -> None:
    execution = Execution(
        execution_key="immutable-created-at",
        run_id="run-1",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        cwd="/tmp",
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
    )

    with pytest.raises(ValidationError, match="Field is frozen"):
        execution.created_at = datetime(2026, 8, 2, tzinfo=UTC)
