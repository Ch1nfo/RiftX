"""Transactional Planner commands and durable Ready Task scheduling."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.domain.base import utc_now
from riftx.tasks import (
    AddTaskCommand,
    BlockTaskCommand,
    CancelTaskCommand,
    ClaimReadyTaskCommand,
    CompleteTaskCommand,
    FailTaskAttemptCommand,
    LinkTasksCommand,
    ReopenTaskCommand,
    Task,
    TaskAttempt,
    TaskAttemptStatus,
    TaskBudget,
    TaskEvidenceRequirement,
    TaskMutationResult,
    TaskStatus,
    UpdateTaskCommand,
)

from .orm import (
    RunRecord,
    TaskAttemptRecord,
    TaskDependencyRecord,
    TaskEvidenceRequirementRecord,
    TaskGraphRecord,
    TaskRecord,
)
from .task_repositories import (
    _attempt_from_record,
    _attempt_record,
    _budget_record,
    _evidence_requirement_record,
    _task_from_record,
    _task_record,
)
from .transactions import SessionFactory, serialized_write

_REPLANNABLE_STATUSES = {TaskStatus.PENDING, TaskStatus.BLOCKED, TaskStatus.FAILED}
_REOPENABLE_STATUSES = {
    TaskStatus.BLOCKED,
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
}


class SQLAlchemyTaskPlanner:
    """Apply typed Planner commands under one Run-scoped Task Graph lock."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def add_task(self, command: AddTaskCommand) -> TaskMutationResult:
        try:
            async with serialized_write(self._session_factory) as session:
                graph, created = await _lock_or_create_graph(
                    session,
                    command.run_id,
                    command.expected_graph_version,
                )
                if command.parent_task_id is not None:
                    await _require_task(session, command.run_id, command.parent_task_id)
                sequence = command.sequence
                if sequence is None:
                    sequence = (
                        await session.scalar(
                            select(func.max(TaskRecord.sequence)).where(
                                TaskRecord.run_id == command.run_id
                            )
                        )
                        or 0
                    ) + 1
                now = utc_now()
                task = Task(
                    id=command.task_id,
                    run_id=command.run_id,
                    parent_task_id=command.parent_task_id,
                    sequence=sequence,
                    title=command.title,
                    description=command.description,
                    input_scope=command.input_scope,
                    expected_output_schema=command.expected_output_schema,
                    required_capability_ids=command.required_capability_ids,
                    workspace_owner=command.workspace_owner,
                    session_owner_id=command.session_owner_id,
                    stop_condition=command.stop_condition,
                    created_at=now,
                    updated_at=now,
                )
                session.add(_task_record(task))
                await session.flush()
                if command.budget is not None:
                    session.add(
                        _budget_record(
                            TaskBudget(
                                run_id=command.run_id,
                                task_id=task.id,
                                **command.budget.model_dump(),
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    )
                session.add_all(
                    _evidence_requirement_record(
                        TaskEvidenceRequirement(
                            run_id=command.run_id,
                            task_id=task.id,
                            **requirement.model_dump(),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    for requirement in command.evidence_requirements
                )
                if not created:
                    _advance_graph(graph, now)
                await session.flush()
                return TaskMutationResult(graph_version=graph.version, task=task)
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not add Task {command.task_id!r} to Run {command.run_id!r}"
            ) from exc

    async def update_task(self, command: UpdateTaskCommand) -> TaskMutationResult:
        try:
            async with serialized_write(self._session_factory) as session:
                graph = await _lock_graph(
                    session,
                    command.run_id,
                    command.expected_graph_version,
                )
                record = await _require_task(session, command.run_id, command.task_id, lock=True)
                _require_status(record, _REPLANNABLE_STATUSES, "update")
                for field, column in (
                    ("title", "title"),
                    ("description", "description"),
                    ("sequence", "sequence"),
                    ("input_scope", "input_scope_json"),
                    ("expected_output_schema", "expected_output_schema_json"),
                    ("required_capability_ids", "required_capability_ids_json"),
                    ("workspace_owner", "workspace_owner"),
                    ("session_owner_id", "session_owner_id"),
                    ("stop_condition", "stop_condition"),
                ):
                    if field in command.model_fields_set:
                        setattr(record, column, getattr(command, field))
                now = utc_now()
                _touch_task(record, now)
                _advance_graph(graph, now)
                await session.flush()
                return TaskMutationResult(
                    graph_version=graph.version,
                    task=_task_from_record(record),
                )
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not update Task {command.task_id!r} in Run {command.run_id!r}"
            ) from exc

    async def link_tasks(self, command: LinkTasksCommand) -> TaskMutationResult:
        try:
            async with serialized_write(self._session_factory) as session:
                graph = await _lock_graph(
                    session,
                    command.run_id,
                    command.expected_graph_version,
                )
                task = await _require_task(session, command.run_id, command.task_id, lock=True)
                _require_status(task, _REPLANNABLE_STATUSES, "link dependencies for")
                await _require_task(session, command.run_id, command.depends_on_task_id)
                identity = {
                    "run_id": command.run_id,
                    "task_id": command.task_id,
                    "depends_on_task_id": command.depends_on_task_id,
                }
                if await session.get(TaskDependencyRecord, identity) is not None:
                    return TaskMutationResult(
                        graph_version=graph.version,
                        task=_task_from_record(task),
                    )
                if await _dependency_reaches(
                    session,
                    command.run_id,
                    start=command.depends_on_task_id,
                    target=command.task_id,
                ):
                    raise RepositoryConflictError("task dependency would create a cycle")
                session.add(TaskDependencyRecord(**identity, created_at=utc_now()))
                now = utc_now()
                _touch_task(task, now)
                _advance_graph(graph, now)
                await session.flush()
                return TaskMutationResult(
                    graph_version=graph.version,
                    task=_task_from_record(task),
                )
        except IntegrityError as exc:
            raise RepositoryConflictError("could not persist Task dependency") from exc

    async def block_task(self, command: BlockTaskCommand) -> TaskMutationResult:
        async with serialized_write(self._session_factory) as session:
            graph = await _lock_graph(
                session,
                command.run_id,
                command.expected_graph_version,
            )
            task = await _require_task(session, command.run_id, command.task_id, lock=True)
            if task.status == TaskStatus.BLOCKED.value and task.blocked_reason == command.reason:
                return TaskMutationResult(
                    graph_version=graph.version,
                    task=_task_from_record(task),
                )
            _require_status(
                task,
                {TaskStatus.PENDING, TaskStatus.FAILED},
                "block",
            )
            task.status = TaskStatus.BLOCKED.value
            task.blocked_reason = command.reason
            now = utc_now()
            _touch_task(task, now)
            _advance_graph(graph, now)
            await session.flush()
            return TaskMutationResult(
                graph_version=graph.version,
                task=_task_from_record(task),
            )

    async def complete_task(self, command: CompleteTaskCommand) -> TaskMutationResult:
        async with serialized_write(self._session_factory) as session:
            graph = await _lock_graph(
                session,
                command.run_id,
                command.expected_graph_version,
            )
            task = await _require_task(session, command.run_id, command.task_id, lock=True)
            _require_status(task, {TaskStatus.RUNNING}, "complete")
            attempt = await _require_attempt(
                session,
                command.run_id,
                command.task_id,
                command.attempt_id,
            )
            if attempt.status != TaskAttemptStatus.RUNNING.value:
                raise RepositoryConflictError("only a running Task Attempt can complete its Task")
            requirements = tuple(
                await session.scalars(
                    select(TaskEvidenceRequirementRecord)
                    .where(
                        TaskEvidenceRequirementRecord.run_id == command.run_id,
                        TaskEvidenceRequirementRecord.task_id == command.task_id,
                    )
                    .with_for_update()
                )
            )
            by_id = {requirement.id: requirement for requirement in requirements}
            unknown = set(command.evidence_refs_by_requirement) - set(by_id)
            if unknown:
                raise RepositoryConflictError(
                    f"completion references unknown Task Evidence Requirements: {sorted(unknown)}"
                )
            now = utc_now()
            for requirement_id, refs in command.evidence_refs_by_requirement.items():
                requirement = by_id[requirement_id]
                requirement.evidence_refs_json = list(
                    dict.fromkeys([*requirement.evidence_refs_json, *refs])
                )
                requirement.updated_at = now
            unsatisfied = [
                requirement.id
                for requirement in requirements
                if len(requirement.evidence_refs_json) < requirement.minimum_count
            ]
            if unsatisfied:
                raise RepositoryConflictError(
                    f"Task Evidence Requirements are not satisfied: {sorted(unsatisfied)}"
                )
            attempt.status = TaskAttemptStatus.SUCCEEDED.value
            attempt.finished_at = now
            task.status = TaskStatus.COMPLETED.value
            task.completion_summary = command.completion_summary
            task.completed_at = now
            task.blocked_reason = None
            _touch_task(task, now)
            _advance_graph(graph, now)
            await session.flush()
            return TaskMutationResult(
                graph_version=graph.version,
                task=_task_from_record(task),
                attempt=_attempt_from_record(attempt),
            )

    async def fail_task_attempt(
        self,
        command: FailTaskAttemptCommand,
    ) -> TaskMutationResult:
        async with serialized_write(self._session_factory) as session:
            graph = await _lock_graph(
                session,
                command.run_id,
                command.expected_graph_version,
            )
            task = await _require_task(session, command.run_id, command.task_id, lock=True)
            _require_status(task, {TaskStatus.RUNNING}, "fail")
            attempt = await _require_attempt(
                session,
                command.run_id,
                command.task_id,
                command.attempt_id,
            )
            if attempt.status != TaskAttemptStatus.RUNNING.value:
                raise RepositoryConflictError("only a running Task Attempt can fail")
            now = utc_now()
            attempt.status = TaskAttemptStatus.FAILED.value
            attempt.failure_summary = command.failure_summary
            attempt.finished_at = now
            task.status = TaskStatus.FAILED.value
            task.blocked_reason = None
            _touch_task(task, now)
            _advance_graph(graph, now)
            await session.flush()
            return TaskMutationResult(
                graph_version=graph.version,
                task=_task_from_record(task),
                attempt=_attempt_from_record(attempt),
            )

    async def reopen_task(self, command: ReopenTaskCommand) -> TaskMutationResult:
        async with serialized_write(self._session_factory) as session:
            graph = await _lock_graph(
                session,
                command.run_id,
                command.expected_graph_version,
            )
            task = await _require_task(session, command.run_id, command.task_id, lock=True)
            _require_status(task, _REOPENABLE_STATUSES, "reopen")
            task.status = TaskStatus.PENDING.value
            task.blocked_reason = None
            task.completion_summary = None
            task.completed_at = None
            task.reopen_history_json = [*task.reopen_history_json, command.reason]
            now = utc_now()
            _touch_task(task, now)
            _advance_graph(graph, now)
            await session.flush()
            return TaskMutationResult(
                graph_version=graph.version,
                task=_task_from_record(task),
            )

    async def cancel_task(self, command: CancelTaskCommand) -> TaskMutationResult:
        async with serialized_write(self._session_factory) as session:
            graph = await _lock_graph(
                session,
                command.run_id,
                command.expected_graph_version,
            )
            task = await _require_task(session, command.run_id, command.task_id, lock=True)
            if task.status == TaskStatus.COMPLETED.value:
                raise RepositoryConflictError("completed Task must be reopened before cancellation")
            if task.status == TaskStatus.CANCELLED.value:
                marker = f"cancelled: {command.reason}"
                if not task.reopen_history_json or task.reopen_history_json[-1] != marker:
                    raise RepositoryConflictError(
                        "cancelled Task already records a different cancellation reason"
                    )
                return TaskMutationResult(
                    graph_version=graph.version,
                    task=_task_from_record(task),
                )
            attempt: TaskAttemptRecord | None = None
            if task.status == TaskStatus.RUNNING.value:
                attempt = await _running_attempt(session, command.run_id, command.task_id)
                if attempt is None:
                    raise RepositoryConflictError("running Task has no running Task Attempt")
                attempt.status = TaskAttemptStatus.CANCELLED.value
                attempt.finished_at = utc_now()
            now = utc_now()
            task.status = TaskStatus.CANCELLED.value
            task.blocked_reason = None
            task.completed_at = None
            task.reopen_history_json = [
                *task.reopen_history_json,
                f"cancelled: {command.reason}",
            ]
            _touch_task(task, now)
            _advance_graph(graph, now)
            await session.flush()
            return TaskMutationResult(
                graph_version=graph.version,
                task=_task_from_record(task),
                attempt=_attempt_from_record(attempt) if attempt is not None else None,
            )

    async def list_ready(self, run_id: str, *, limit: int = 100) -> tuple[Task, ...]:
        if limit < 1 or limit > 1_000:
            raise ValueError("Ready Task limit must be between 1 and 1000")
        async with self._session_factory() as session:
            records = tuple(await session.scalars(_ready_tasks_statement(run_id).limit(limit)))
            return tuple(_task_from_record(record) for record in records)

    async def claim_ready_task(
        self,
        command: ClaimReadyTaskCommand,
    ) -> TaskMutationResult | None:
        async with serialized_write(self._session_factory) as session:
            graph = await session.scalar(
                select(TaskGraphRecord)
                .where(TaskGraphRecord.run_id == command.run_id)
                .with_for_update()
            )
            if graph is None:
                return None
            statement = _ready_tasks_statement(command.run_id)
            statement = statement.where(
                TaskRecord.session_owner_id.is_(None)
                if command.session_id is None
                else or_(
                    TaskRecord.session_owner_id.is_(None),
                    TaskRecord.session_owner_id == command.session_id,
                )
            )
            if command.preferred_task_id is not None:
                statement = statement.where(TaskRecord.id == command.preferred_task_id)
            task = await session.scalar(statement.limit(1).with_for_update())
            if task is None:
                return None
            latest_attempt = await session.scalar(
                select(TaskAttemptRecord)
                .where(
                    TaskAttemptRecord.run_id == command.run_id,
                    TaskAttemptRecord.task_id == task.id,
                )
                .order_by(TaskAttemptRecord.sequence.desc())
                .limit(1)
                .with_for_update()
            )
            now = command.claimed_at or utc_now()
            attempt = TaskAttempt(
                run_id=command.run_id,
                task_id=task.id,
                sequence=(latest_attempt.sequence + 1) if latest_attempt is not None else 1,
                status=TaskAttemptStatus.RUNNING,
                session_id=command.session_id,
                worker_id=command.worker_id,
                retry_of_attempt_id=(
                    latest_attempt.id
                    if latest_attempt is not None
                    and latest_attempt.status == TaskAttemptStatus.FAILED.value
                    else None
                ),
                created_at=now,
                started_at=now,
            )
            session.add(_attempt_record(attempt))
            task.status = TaskStatus.RUNNING.value
            task.blocked_reason = None
            _touch_task(task, now)
            _advance_graph(graph, now)
            await session.flush()
            return TaskMutationResult(
                graph_version=graph.version,
                task=_task_from_record(task),
                attempt=attempt,
            )


async def _lock_or_create_graph(
    session: AsyncSession,
    run_id: str,
    expected_version: int,
) -> tuple[TaskGraphRecord, bool]:
    graph = await session.scalar(
        select(TaskGraphRecord).where(TaskGraphRecord.run_id == run_id).with_for_update()
    )
    if graph is not None:
        if graph.version != expected_version:
            raise RepositoryConflictError(
                f"Task Graph version conflict; expected {expected_version}, current {graph.version}"
            )
        return graph, False
    if expected_version != 0:
        raise RepositoryConflictError(
            f"Task Graph for Run {run_id!r} does not exist; expected version must be 0"
        )
    run = await session.scalar(select(RunRecord).where(RunRecord.id == run_id).with_for_update())
    if run is None:
        raise EntityNotFoundError("Run", run_id)
    now = utc_now()
    graph = TaskGraphRecord(run_id=run_id, version=1, created_at=now, updated_at=now)
    session.add(graph)
    await session.flush()
    return graph, True


async def _lock_graph(
    session: AsyncSession,
    run_id: str,
    expected_version: int,
) -> TaskGraphRecord:
    graph = await session.scalar(
        select(TaskGraphRecord).where(TaskGraphRecord.run_id == run_id).with_for_update()
    )
    if graph is None:
        raise EntityNotFoundError("TaskGraph", run_id)
    if graph.version != expected_version:
        raise RepositoryConflictError(
            f"Task Graph version conflict; expected {expected_version}, current {graph.version}"
        )
    return graph


async def _require_task(
    session: AsyncSession,
    run_id: str,
    task_id: str,
    *,
    lock: bool = False,
) -> TaskRecord:
    statement = select(TaskRecord).where(
        TaskRecord.run_id == run_id,
        TaskRecord.id == task_id,
    )
    if lock:
        statement = statement.with_for_update()
    record = await session.scalar(statement)
    if record is None:
        raise EntityNotFoundError("Task", task_id)
    return record


async def _require_attempt(
    session: AsyncSession,
    run_id: str,
    task_id: str,
    attempt_id: str,
) -> TaskAttemptRecord:
    attempt = await session.scalar(
        select(TaskAttemptRecord)
        .where(
            TaskAttemptRecord.run_id == run_id,
            TaskAttemptRecord.task_id == task_id,
            TaskAttemptRecord.id == attempt_id,
        )
        .with_for_update()
    )
    if attempt is None:
        raise EntityNotFoundError("TaskAttempt", attempt_id)
    return attempt


async def _running_attempt(
    session: AsyncSession,
    run_id: str,
    task_id: str,
) -> TaskAttemptRecord | None:
    return await session.scalar(
        select(TaskAttemptRecord)
        .where(
            TaskAttemptRecord.run_id == run_id,
            TaskAttemptRecord.task_id == task_id,
            TaskAttemptRecord.status == TaskAttemptStatus.RUNNING.value,
        )
        .with_for_update()
    )


async def _dependency_reaches(
    session: AsyncSession,
    run_id: str,
    *,
    start: str,
    target: str,
) -> bool:
    rows = await session.execute(
        select(
            TaskDependencyRecord.task_id,
            TaskDependencyRecord.depends_on_task_id,
        ).where(TaskDependencyRecord.run_id == run_id)
    )
    edges: dict[str, list[str]] = {}
    for task_id, dependency_id in rows:
        edges.setdefault(task_id, []).append(dependency_id)
    pending = [start]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(edges.get(current, ()))
    return False


def _ready_tasks_statement(run_id: str):
    dependency = aliased(TaskDependencyRecord)
    prerequisite = aliased(TaskRecord)
    unsatisfied_dependency = exists(
        select(1)
        .select_from(dependency)
        .join(
            prerequisite,
            and_(
                prerequisite.run_id == dependency.run_id,
                prerequisite.id == dependency.depends_on_task_id,
            ),
        )
        .where(
            dependency.run_id == TaskRecord.run_id,
            dependency.task_id == TaskRecord.id,
            prerequisite.status != TaskStatus.COMPLETED.value,
        )
    )
    return (
        select(TaskRecord)
        .where(
            TaskRecord.run_id == run_id,
            TaskRecord.status == TaskStatus.PENDING.value,
            ~unsatisfied_dependency,
        )
        .order_by(TaskRecord.sequence, TaskRecord.id)
    )


def _require_status(
    task: TaskRecord,
    allowed: Iterable[TaskStatus],
    operation: str,
) -> None:
    allowed_values = {status.value for status in allowed}
    if task.status not in allowed_values:
        raise RepositoryConflictError(
            f"cannot {operation} Task {task.id!r} while status is {task.status!r}"
        )


def _touch_task(task: TaskRecord, now) -> None:
    task.version += 1
    task.updated_at = now


def _advance_graph(graph: TaskGraphRecord, now) -> None:
    graph.version += 1
    graph.updated_at = now
