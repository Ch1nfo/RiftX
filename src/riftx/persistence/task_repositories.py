"""SQLAlchemy persistence for the durable Task Graph aggregate."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from riftx.application.errors import RepositoryConflictError
from riftx.tasks import (
    Task,
    TaskAttempt,
    TaskAttemptStatus,
    TaskBudget,
    TaskDependency,
    TaskEvidenceRequirement,
    TaskGraph,
    TaskStatus,
)

from .orm import (
    RunRecord,
    TaskAttemptRecord,
    TaskBudgetRecord,
    TaskDependencyRecord,
    TaskEvidenceRequirementRecord,
    TaskGraphRecord,
    TaskRecord,
)
from .transactions import SessionFactory, consistent_read, serialized_write


class SQLAlchemyTaskGraphRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, graph: TaskGraph) -> TaskGraph:
        try:
            async with serialized_write(self._session_factory) as session:
                run = await session.get(RunRecord, graph.run_id, with_for_update=True)
                if run is None:
                    raise RepositoryConflictError(
                        f"cannot create Task Graph for unknown Run {graph.run_id!r}"
                    )
                if await session.get(TaskGraphRecord, graph.run_id) is not None:
                    raise RepositoryConflictError(
                        f"Task Graph for Run {graph.run_id!r} already exists"
                    )
                session.add(_graph_record(graph))
                session.add_all(_task_record(task) for task in graph.tasks)
                await session.flush()
                session.add_all(_dependency_record(dependency) for dependency in graph.dependencies)
                session.add_all(_attempt_record(attempt) for attempt in graph.attempts)
                session.add_all(_budget_record(budget) for budget in graph.budgets)
                session.add_all(
                    _evidence_requirement_record(requirement)
                    for requirement in graph.evidence_requirements
                )
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not persist Task Graph for Run {graph.run_id!r}"
            ) from exc
        return graph

    async def get(self, run_id: str) -> TaskGraph | None:
        async with consistent_read(self._session_factory) as session:
            graph = await session.get(TaskGraphRecord, run_id)
            if graph is None:
                return None
            tasks = tuple(
                await session.scalars(
                    select(TaskRecord)
                    .where(TaskRecord.run_id == run_id)
                    .order_by(TaskRecord.sequence, TaskRecord.id)
                )
            )
            dependencies = tuple(
                await session.scalars(
                    select(TaskDependencyRecord)
                    .where(TaskDependencyRecord.run_id == run_id)
                    .order_by(
                        TaskDependencyRecord.task_id,
                        TaskDependencyRecord.depends_on_task_id,
                    )
                )
            )
            attempts = tuple(
                await session.scalars(
                    select(TaskAttemptRecord)
                    .where(TaskAttemptRecord.run_id == run_id)
                    .order_by(TaskAttemptRecord.task_id, TaskAttemptRecord.sequence)
                )
            )
            budgets = tuple(
                await session.scalars(
                    select(TaskBudgetRecord)
                    .where(TaskBudgetRecord.run_id == run_id)
                    .order_by(TaskBudgetRecord.task_id)
                )
            )
            requirements = tuple(
                await session.scalars(
                    select(TaskEvidenceRequirementRecord)
                    .where(TaskEvidenceRequirementRecord.run_id == run_id)
                    .order_by(
                        TaskEvidenceRequirementRecord.task_id,
                        TaskEvidenceRequirementRecord.id,
                    )
                )
            )
            return TaskGraph(
                run_id=graph.run_id,
                version=graph.version,
                tasks=[_task_from_record(item) for item in tasks],
                dependencies=[_dependency_from_record(item) for item in dependencies],
                attempts=[_attempt_from_record(item) for item in attempts],
                budgets=[_budget_from_record(item) for item in budgets],
                evidence_requirements=[
                    _evidence_requirement_from_record(item) for item in requirements
                ],
                created_at=graph.created_at,
                updated_at=graph.updated_at,
            )


def _graph_record(graph: TaskGraph) -> TaskGraphRecord:
    return TaskGraphRecord(
        run_id=graph.run_id,
        version=graph.version,
        created_at=graph.created_at,
        updated_at=graph.updated_at,
    )


def _task_record(task: Task) -> TaskRecord:
    return TaskRecord(
        id=task.id,
        run_id=task.run_id,
        parent_task_id=task.parent_task_id,
        sequence=task.sequence,
        title=task.title,
        description=task.description,
        status=task.status.value,
        input_scope_json=task.input_scope,
        expected_output_schema_json=task.expected_output_schema,
        required_capability_ids_json=task.required_capability_ids,
        workspace_owner=task.workspace_owner,
        session_owner_id=task.session_owner_id,
        stop_condition=task.stop_condition,
        completion_summary=task.completion_summary,
        blocked_reason=task.blocked_reason,
        reopen_history_json=task.reopen_history,
        version=task.version,
        created_at=task.created_at,
        updated_at=task.updated_at,
        completed_at=task.completed_at,
    )


def _task_from_record(record: TaskRecord) -> Task:
    return Task(
        id=record.id,
        run_id=record.run_id,
        parent_task_id=record.parent_task_id,
        sequence=record.sequence,
        title=record.title,
        description=record.description,
        status=TaskStatus(record.status),
        input_scope=record.input_scope_json,
        expected_output_schema=record.expected_output_schema_json,
        required_capability_ids=record.required_capability_ids_json,
        workspace_owner=record.workspace_owner,
        session_owner_id=record.session_owner_id,
        stop_condition=record.stop_condition,
        completion_summary=record.completion_summary,
        blocked_reason=record.blocked_reason,
        reopen_history=record.reopen_history_json,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        completed_at=record.completed_at,
    )


def _dependency_record(dependency: TaskDependency) -> TaskDependencyRecord:
    return TaskDependencyRecord(
        run_id=dependency.run_id,
        task_id=dependency.task_id,
        depends_on_task_id=dependency.depends_on_task_id,
        created_at=dependency.created_at,
    )


def _dependency_from_record(record: TaskDependencyRecord) -> TaskDependency:
    return TaskDependency(
        run_id=record.run_id,
        task_id=record.task_id,
        depends_on_task_id=record.depends_on_task_id,
        created_at=record.created_at,
    )


def _attempt_record(attempt: TaskAttempt) -> TaskAttemptRecord:
    return TaskAttemptRecord(
        id=attempt.id,
        run_id=attempt.run_id,
        task_id=attempt.task_id,
        sequence=attempt.sequence,
        status=attempt.status.value,
        session_id=attempt.session_id,
        worker_id=attempt.worker_id,
        retry_of_attempt_id=attempt.retry_of_attempt_id,
        failure_summary=attempt.failure_summary,
        created_at=attempt.created_at,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
    )


def _attempt_from_record(record: TaskAttemptRecord) -> TaskAttempt:
    return TaskAttempt(
        id=record.id,
        run_id=record.run_id,
        task_id=record.task_id,
        sequence=record.sequence,
        status=TaskAttemptStatus(record.status),
        session_id=record.session_id,
        worker_id=record.worker_id,
        retry_of_attempt_id=record.retry_of_attempt_id,
        failure_summary=record.failure_summary,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def _budget_record(budget: TaskBudget) -> TaskBudgetRecord:
    return TaskBudgetRecord(
        run_id=budget.run_id,
        task_id=budget.task_id,
        max_model_calls=budget.max_model_calls,
        max_tool_calls=budget.max_tool_calls,
        max_tokens=budget.max_tokens,
        max_duration_seconds=budget.max_duration_seconds,
        created_at=budget.created_at,
        updated_at=budget.updated_at,
    )


def _budget_from_record(record: TaskBudgetRecord) -> TaskBudget:
    return TaskBudget(
        run_id=record.run_id,
        task_id=record.task_id,
        max_model_calls=record.max_model_calls,
        max_tool_calls=record.max_tool_calls,
        max_tokens=record.max_tokens,
        max_duration_seconds=record.max_duration_seconds,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _evidence_requirement_record(
    requirement: TaskEvidenceRequirement,
) -> TaskEvidenceRequirementRecord:
    return TaskEvidenceRequirementRecord(
        id=requirement.id,
        run_id=requirement.run_id,
        task_id=requirement.task_id,
        evidence_type=requirement.evidence_type,
        description=requirement.description,
        minimum_count=requirement.minimum_count,
        success_criterion_index=requirement.success_criterion_index,
        evidence_refs_json=requirement.evidence_refs,
        created_at=requirement.created_at,
        updated_at=requirement.updated_at,
    )


def _evidence_requirement_from_record(
    record: TaskEvidenceRequirementRecord,
) -> TaskEvidenceRequirement:
    return TaskEvidenceRequirement(
        id=record.id,
        run_id=record.run_id,
        task_id=record.task_id,
        evidence_type=record.evidence_type,
        description=record.description,
        minimum_count=record.minimum_count,
        success_criterion_index=record.success_criterion_index,
        evidence_refs=record.evidence_refs_json,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
