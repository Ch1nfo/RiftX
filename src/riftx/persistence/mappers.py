"""Mappings between domain objects and SQLAlchemy records."""

from __future__ import annotations

from riftx.domain import (
    Approval,
    ApprovalGrant,
    ApprovalMode,
    ApprovalStatus,
    Artifact,
    Engagement,
    EntryPoint,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Finding,
    FindingEvidence,
    FindingSeverity,
    FindingStatus,
    Node,
    NodeStatus,
    Objective,
    Report,
    ReportFormat,
    Run,
    RunEvent,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandStatus,
    RunnerCredential,
    RunStatus,
    Scope,
    SuccessCriterion,
    TerminalOwner,
    TerminalSession,
    TerminalStatus,
    ToolCall,
)

from .orm import (
    ApprovalGrantRecord,
    ApprovalRecord,
    ArtifactRecord,
    EngagementRecord,
    ExecutionRecord,
    FindingRecord,
    NodeRecord,
    ReportRecord,
    RunEventRecord,
    RunnerCommandRecord,
    RunnerCredentialRecord,
    RunRecord,
    TerminalSessionRecord,
    ToolCallRecord,
)


def node_to_record(node: Node) -> NodeRecord:
    return NodeRecord(
        id=node.id,
        name=node.name,
        platform=node.platform,
        architecture=node.architecture,
        runner_version=node.runner_version,
        status=node.status.value,
        capabilities_json=node.capabilities,
        labels_json=node.labels,
        last_seen_at=node.last_seen_at,
        created_at=node.created_at,
        updated_at=node.updated_at,
    )


def apply_node_to_record(node: Node, record: NodeRecord) -> None:
    record.name = node.name
    record.platform = node.platform
    record.architecture = node.architecture
    record.runner_version = node.runner_version
    record.status = node.status.value
    record.capabilities_json = node.capabilities
    record.labels_json = node.labels
    record.last_seen_at = node.last_seen_at
    record.updated_at = node.updated_at


def node_from_record(record: NodeRecord) -> Node:
    return Node(
        id=record.id,
        name=record.name,
        platform=record.platform,
        architecture=record.architecture,
        runner_version=record.runner_version,
        status=NodeStatus(record.status),
        capabilities=record.capabilities_json or [],
        labels=record.labels_json or {},
        last_seen_at=record.last_seen_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def artifact_to_record(artifact: Artifact) -> ArtifactRecord:
    return ArtifactRecord(
        id=artifact.id,
        run_id=artifact.run_id,
        execution_id=artifact.execution_id,
        name=artifact.name,
        path=artifact.path,
        mime_type=artifact.mime_type,
        sha256=artifact.sha256,
        size=artifact.size,
        description=artifact.description,
        created_at=artifact.created_at,
    )


def artifact_from_record(record: ArtifactRecord) -> Artifact:
    return Artifact(
        id=record.id,
        run_id=record.run_id,
        execution_id=record.execution_id,
        name=record.name,
        path=record.path,
        mime_type=record.mime_type,
        sha256=record.sha256,
        size=record.size,
        description=record.description,
        created_at=record.created_at,
    )


def engagement_to_record(engagement: Engagement) -> EngagementRecord:
    return EngagementRecord(
        id=engagement.id,
        name=engagement.name,
        description=engagement.description,
        authorization_reference=engagement.authorization_reference,
        created_at=engagement.created_at,
        updated_at=engagement.updated_at,
    )


def engagement_from_record(record: EngagementRecord) -> Engagement:
    return Engagement(
        id=record.id,
        name=record.name,
        description=record.description,
        authorization_reference=record.authorization_reference,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def runner_credential_to_record(credential: RunnerCredential) -> RunnerCredentialRecord:
    return RunnerCredentialRecord(
        node_id=credential.node_id,
        token_hash=credential.token_hash,
        token_prefix=credential.token_prefix,
        created_at=credential.created_at,
        rotated_at=credential.rotated_at,
        revoked_at=credential.revoked_at,
    )


def apply_runner_credential_to_record(
    credential: RunnerCredential,
    record: RunnerCredentialRecord,
) -> None:
    record.token_hash = credential.token_hash
    record.token_prefix = credential.token_prefix
    record.rotated_at = credential.rotated_at
    record.revoked_at = credential.revoked_at


def runner_credential_from_record(record: RunnerCredentialRecord) -> RunnerCredential:
    return RunnerCredential(
        node_id=record.node_id,
        token_hash=record.token_hash,
        token_prefix=record.token_prefix,
        created_at=record.created_at,
        rotated_at=record.rotated_at,
        revoked_at=record.revoked_at,
    )


def runner_command_to_record(command: RunnerCommand) -> RunnerCommandRecord:
    return RunnerCommandRecord(
        id=command.id,
        node_id=command.node_id,
        kind=command.kind.value,
        idempotency_key=command.idempotency_key,
        payload_json=command.payload,
        status=command.status.value,
        attempts=command.attempts,
        lease_id=command.lease_id,
        lease_expires_at=command.lease_expires_at,
        result_json=command.result,
        error=command.error,
        created_at=command.created_at,
        updated_at=command.updated_at,
        completed_at=command.completed_at,
    )


def runner_command_from_record(record: RunnerCommandRecord) -> RunnerCommand:
    return RunnerCommand(
        id=record.id,
        node_id=record.node_id,
        kind=RunnerCommandKind(record.kind),
        idempotency_key=record.idempotency_key,
        payload=record.payload_json or {},
        status=RunnerCommandStatus(record.status),
        attempts=record.attempts,
        lease_id=record.lease_id,
        lease_expires_at=record.lease_expires_at,
        result=record.result_json or {},
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        completed_at=record.completed_at,
    )


def run_to_record(run: Run) -> RunRecord:
    return RunRecord(
        id=run.id,
        engagement_id=run.engagement_id,
        node_id=run.node_id,
        objective=run.objective.description,
        success_criteria_json=[item.model_dump(mode="json") for item in run.success_criteria],
        entry_points_json=[item.model_dump(mode="json") for item in run.entry_points],
        scope_json=run.scope.model_dump(mode="json"),
        status=run.status.value,
        approval_mode=run.approval_mode.value,
        workspace_path=run.workspace_path,
        temporal_workflow_id=run.temporal_workflow_id,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def apply_run_to_record(run: Run, record: RunRecord) -> None:
    """Copy mutable run state onto an already-persisted record."""

    record.node_id = run.node_id
    record.objective = run.objective.description
    record.success_criteria_json = [item.model_dump(mode="json") for item in run.success_criteria]
    record.entry_points_json = [item.model_dump(mode="json") for item in run.entry_points]
    record.scope_json = run.scope.model_dump(mode="json")
    record.status = run.status.value
    record.approval_mode = run.approval_mode.value
    record.workspace_path = run.workspace_path
    record.temporal_workflow_id = run.temporal_workflow_id
    record.started_at = run.started_at
    record.finished_at = run.finished_at


def run_from_record(record: RunRecord) -> Run:
    return Run(
        id=record.id,
        engagement_id=record.engagement_id,
        node_id=record.node_id,
        objective=Objective(description=record.objective),
        success_criteria=[
            SuccessCriterion.model_validate(item) for item in record.success_criteria_json
        ],
        entry_points=[EntryPoint.model_validate(item) for item in record.entry_points_json],
        scope=Scope.model_validate(record.scope_json),
        status=RunStatus(record.status),
        approval_mode=ApprovalMode(record.approval_mode),
        workspace_path=record.workspace_path,
        temporal_workflow_id=record.temporal_workflow_id,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def event_to_record(event: RunEvent) -> RunEventRecord:
    return RunEventRecord(
        id=event.id,
        run_id=event.run_id,
        sequence=event.sequence,
        event_type=event.event_type,
        payload_json=event.payload,
        created_at=event.created_at,
    )


def event_from_record(record: RunEventRecord) -> RunEvent:
    return RunEvent(
        id=record.id,
        run_id=record.run_id,
        sequence=record.sequence,
        event_type=record.event_type,
        payload=record.payload_json,
        created_at=record.created_at,
    )


def execution_to_record(execution: Execution) -> ExecutionRecord:
    return ExecutionRecord(
        id=execution.id,
        execution_key=execution.execution_key,
        run_id=execution.run_id,
        node_id=execution.node_id,
        executor_type=execution.executor_type.value,
        argv_json=execution.argv,
        command_text=execution.command_text,
        cwd=execution.cwd,
        env_diff_json=execution.env_diff,
        status=execution.status.value,
        pid=execution.pid,
        process_group_id=execution.process_group_id,
        exit_code=execution.exit_code,
        stdout_path=execution.stdout_path,
        stderr_path=execution.stderr_path,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
    )


def apply_execution_to_record(execution: Execution, record: ExecutionRecord) -> None:
    record.node_id = execution.node_id
    record.executor_type = execution.executor_type.value
    record.argv_json = execution.argv
    record.command_text = execution.command_text
    record.cwd = execution.cwd
    record.env_diff_json = execution.env_diff
    record.status = execution.status.value
    record.pid = execution.pid
    record.process_group_id = execution.process_group_id
    record.exit_code = execution.exit_code
    record.stdout_path = execution.stdout_path
    record.stderr_path = execution.stderr_path
    record.started_at = execution.started_at
    record.finished_at = execution.finished_at


def execution_from_record(record: ExecutionRecord) -> Execution:
    return Execution(
        id=record.id,
        execution_key=record.execution_key,
        run_id=record.run_id,
        node_id=record.node_id,
        executor_type=ExecutorType(record.executor_type),
        argv=record.argv_json,
        command_text=record.command_text,
        cwd=record.cwd,
        env_diff=record.env_diff_json or {},
        status=ExecutionStatus(record.status),
        pid=record.pid,
        process_group_id=record.process_group_id,
        exit_code=record.exit_code,
        stdout_path=record.stdout_path,
        stderr_path=record.stderr_path,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def finding_to_record(finding: Finding) -> FindingRecord:
    return FindingRecord(
        id=finding.id,
        run_id=finding.run_id,
        title=finding.title,
        severity=finding.severity.value,
        status=finding.status.value,
        affected_assets_json=finding.affected_assets,
        description=finding.description,
        evidence_json=[item.model_dump(mode="json") for item in finding.evidence],
        reproduction_steps_json=finding.reproduction_steps,
        impact=finding.impact,
        recommendation=finding.recommendation,
        created_at=finding.created_at,
        updated_at=finding.updated_at,
    )


def apply_finding_to_record(finding: Finding, record: FindingRecord) -> None:
    record.title = finding.title
    record.severity = finding.severity.value
    record.status = finding.status.value
    record.affected_assets_json = finding.affected_assets
    record.description = finding.description
    record.evidence_json = [item.model_dump(mode="json") for item in finding.evidence]
    record.reproduction_steps_json = finding.reproduction_steps
    record.impact = finding.impact
    record.recommendation = finding.recommendation
    record.updated_at = finding.updated_at


def finding_from_record(record: FindingRecord) -> Finding:
    return Finding(
        id=record.id,
        run_id=record.run_id,
        title=record.title,
        severity=FindingSeverity(record.severity),
        status=FindingStatus(record.status),
        affected_assets=record.affected_assets_json,
        description=record.description,
        evidence=[FindingEvidence.model_validate(item) for item in record.evidence_json],
        reproduction_steps=record.reproduction_steps_json,
        impact=record.impact,
        recommendation=record.recommendation,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def tool_call_to_record(tool_call: ToolCall) -> ToolCallRecord:
    return ToolCallRecord(
        id=tool_call.id,
        sdk_call_id=tool_call.sdk_call_id,
        run_id=tool_call.run_id,
        agent_step_id=tool_call.agent_step_id,
        tool_id=tool_call.tool_id,
        skill_id=tool_call.skill_id,
        arguments_json=tool_call.arguments,
        approval_status=tool_call.approval_status.value,
        execution_id=tool_call.execution_id,
        created_at=tool_call.created_at,
    )


def tool_call_from_record(record: ToolCallRecord) -> ToolCall:
    return ToolCall(
        id=record.id,
        sdk_call_id=record.sdk_call_id or record.id,
        run_id=record.run_id,
        agent_step_id=record.agent_step_id,
        tool_id=record.tool_id,
        skill_id=record.skill_id,
        arguments=record.arguments_json,
        approval_status=ApprovalStatus(record.approval_status),
        execution_id=record.execution_id,
        created_at=record.created_at,
    )


def approval_to_record(approval: Approval) -> ApprovalRecord:
    return ApprovalRecord(
        id=approval.id,
        run_id=approval.run_id,
        tool_call_id=approval.tool_call_id,
        status=approval.status.value,
        tool_name=approval.tool_name,
        command_json=approval.command,
        cwd=approval.cwd,
        target_summary=approval.target_summary,
        env_diff_json=approval.env_diff,
        reason=approval.reason,
        decided_by=approval.decided_by,
        created_at=approval.created_at,
        decided_at=approval.decided_at,
    )


def apply_approval_to_record(approval: Approval, record: ApprovalRecord) -> None:
    record.status = approval.status.value
    record.reason = approval.reason
    record.decided_by = approval.decided_by
    record.decided_at = approval.decided_at


def approval_from_record(record: ApprovalRecord) -> Approval:
    return Approval(
        id=record.id,
        run_id=record.run_id,
        tool_call_id=record.tool_call_id,
        status=ApprovalStatus(record.status),
        tool_name=record.tool_name,
        command=record.command_json or [],
        cwd=record.cwd,
        target_summary=record.target_summary,
        env_diff=record.env_diff_json or {},
        reason=record.reason,
        decided_by=record.decided_by,
        created_at=record.created_at,
        decided_at=record.decided_at,
    )


def approval_grant_to_record(grant: ApprovalGrant) -> ApprovalGrantRecord:
    return ApprovalGrantRecord(
        id=grant.id,
        run_id=grant.run_id,
        tool_id=grant.tool_id,
        created_by=grant.created_by,
        created_at=grant.created_at,
    )


def approval_grant_from_record(record: ApprovalGrantRecord) -> ApprovalGrant:
    return ApprovalGrant(
        id=record.id,
        run_id=record.run_id,
        tool_id=record.tool_id,
        created_by=record.created_by,
        created_at=record.created_at,
    )


def terminal_to_record(terminal: TerminalSession) -> TerminalSessionRecord:
    return TerminalSessionRecord(
        id=terminal.id,
        run_id=terminal.run_id,
        execution_id=terminal.execution_id,
        status=terminal.status.value,
        owner=terminal.owner.value,
        cols=terminal.cols,
        rows=terminal.rows,
        created_at=terminal.created_at,
        closed_at=terminal.closed_at,
    )


def apply_terminal_to_record(
    terminal: TerminalSession,
    record: TerminalSessionRecord,
) -> None:
    record.status = terminal.status.value
    record.owner = terminal.owner.value
    record.cols = terminal.cols
    record.rows = terminal.rows
    record.closed_at = terminal.closed_at


def terminal_from_record(record: TerminalSessionRecord) -> TerminalSession:
    return TerminalSession(
        id=record.id,
        run_id=record.run_id,
        execution_id=record.execution_id,
        status=TerminalStatus(record.status),
        owner=TerminalOwner(record.owner),
        cols=record.cols,
        rows=record.rows,
        created_at=record.created_at,
        closed_at=record.closed_at,
    )


def report_to_record(report: Report) -> ReportRecord:
    return ReportRecord(
        id=report.id,
        run_id=report.run_id,
        format=report.format.value,
        artifact_id=report.artifact_id,
        finding_ids_json=report.finding_ids,
        created_at=report.created_at,
    )


def report_from_record(record: ReportRecord) -> Report:
    return Report(
        id=record.id,
        run_id=record.run_id,
        format=ReportFormat(record.format),
        artifact_id=record.artifact_id,
        finding_ids=record.finding_ids_json or [],
        created_at=record.created_at,
    )
