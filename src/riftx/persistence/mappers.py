"""Mappings between domain objects and SQLAlchemy records."""

from __future__ import annotations

from riftx.domain import (
    ApprovalMode,
    Engagement,
    EntryPoint,
    Objective,
    Run,
    RunEvent,
    RunStatus,
    Scope,
    SuccessCriterion,
)

from .orm import EngagementRecord, RunEventRecord, RunRecord


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
