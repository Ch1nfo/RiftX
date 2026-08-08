"""Authoritative durable Task Graph domain contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from riftx.domain.base import DomainModel, new_id, utc_now


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskAttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(DomainModel):
    id: str = Field(default_factory=new_id, min_length=1)
    run_id: str = Field(min_length=1)
    parent_task_id: str | None = None
    sequence: int = Field(ge=1)
    title: str = Field(min_length=1)
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    input_scope: dict[str, JsonValue] = Field(default_factory=dict)
    expected_output_schema: dict[str, JsonValue] = Field(default_factory=dict)
    required_capability_ids: list[str] = Field(default_factory=list)
    workspace_owner: str | None = None
    session_owner_id: str | None = None
    stop_condition: str | None = None
    completion_summary: str | None = None
    blocked_reason: str | None = None
    reopen_history: list[str] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Task:
        if self.parent_task_id == self.id:
            raise ValueError("task cannot be its own parent")
        if len(self.required_capability_ids) != len(set(self.required_capability_ids)):
            raise ValueError("task capability requirements must be unique")
        if self.status is TaskStatus.BLOCKED and not self.blocked_reason:
            raise ValueError("blocked task requires a reason")
        if self.status is TaskStatus.COMPLETED:
            if not self.completion_summary or self.completed_at is None:
                raise ValueError("completed task requires a summary and completion time")
        elif self.completed_at is not None:
            raise ValueError("only completed tasks may have a completion time")
        return self


class TaskDependency(DomainModel):
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    depends_on_task_id: str = Field(min_length=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def reject_self_dependency(self) -> TaskDependency:
        if self.task_id == self.depends_on_task_id:
            raise ValueError("task cannot depend on itself")
        return self


class TaskAttempt(DomainModel):
    id: str = Field(default_factory=new_id, min_length=1)
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    status: TaskAttemptStatus
    session_id: str | None = None
    worker_id: str = Field(min_length=1)
    retry_of_attempt_id: str | None = None
    failure_summary: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_attempt(self) -> TaskAttempt:
        if self.retry_of_attempt_id == self.id:
            raise ValueError("task attempt cannot retry itself")
        if self.status is TaskAttemptStatus.RUNNING:
            if self.finished_at is not None:
                raise ValueError("running task attempt cannot have a finish time")
            if self.failure_summary is not None:
                raise ValueError("running task attempt cannot have a failure summary")
        else:
            if self.finished_at is None:
                raise ValueError("terminal task attempt requires a finish time")
            if self.finished_at < self.started_at:
                raise ValueError("task attempt cannot finish before it starts")
        if self.status is TaskAttemptStatus.FAILED and not self.failure_summary:
            raise ValueError("failed task attempt requires a failure summary")
        if self.status is not TaskAttemptStatus.FAILED and self.failure_summary is not None:
            raise ValueError("only failed task attempts may have a failure summary")
        return self


class TaskBudget(DomainModel):
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    max_model_calls: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_duration_seconds: float | None = Field(default=None, gt=0)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_a_limit(self) -> TaskBudget:
        if all(
            value is None
            for value in (
                self.max_model_calls,
                self.max_tool_calls,
                self.max_tokens,
                self.max_duration_seconds,
            )
        ):
            raise ValueError("task budget requires at least one limit")
        return self


class TaskEvidenceRequirement(DomainModel):
    id: str = Field(default_factory=new_id, min_length=1)
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    evidence_type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    minimum_count: int = Field(default=1, ge=1)
    success_criterion_index: int | None = Field(default=None, ge=0)
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_unique_refs(self) -> TaskEvidenceRequirement:
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("task evidence references must be unique")
        return self

    @property
    def satisfied(self) -> bool:
        return len(self.evidence_refs) >= self.minimum_count


class TaskGraph(DomainModel):
    run_id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    tasks: list[Task] = Field(default_factory=list)
    dependencies: list[TaskDependency] = Field(default_factory=list)
    attempts: list[TaskAttempt] = Field(default_factory=list)
    budgets: list[TaskBudget] = Field(default_factory=list)
    evidence_requirements: list[TaskEvidenceRequirement] = Field(default_factory=list)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_graph(self) -> TaskGraph:
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task IDs must be unique")
        sequences = [task.sequence for task in self.tasks]
        if len(sequences) != len(set(sequences)):
            raise ValueError("task sequences must be unique within a Run")
        if any(task.run_id != self.run_id for task in self.tasks):
            raise ValueError("all tasks must belong to the Task Graph Run")
        known_tasks = set(task_ids)
        for task in self.tasks:
            if task.parent_task_id is not None and task.parent_task_id not in known_tasks:
                raise ValueError(f"task {task.id!r} references an unknown parent")

        dependency_keys: list[tuple[str, str]] = []
        for dependency in self.dependencies:
            if dependency.run_id != self.run_id:
                raise ValueError("all dependencies must belong to the Task Graph Run")
            if {dependency.task_id, dependency.depends_on_task_id} - known_tasks:
                raise ValueError("task dependency references an unknown task")
            dependency_keys.append((dependency.task_id, dependency.depends_on_task_id))
        if len(dependency_keys) != len(set(dependency_keys)):
            raise ValueError("task dependencies must be unique")

        self._require_acyclic(
            {task.id: [task.parent_task_id] if task.parent_task_id else [] for task in self.tasks},
            "task parent hierarchy",
        )
        dependency_edges: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
        for dependency in self.dependencies:
            dependency_edges[dependency.task_id].append(dependency.depends_on_task_id)
        self._require_acyclic(dependency_edges, "task dependency graph")

        attempts_by_task: dict[str, list[TaskAttempt]] = {task_id: [] for task_id in task_ids}
        attempt_ids: set[str] = set()
        for attempt in self.attempts:
            if attempt.run_id != self.run_id or attempt.task_id not in known_tasks:
                raise ValueError("task attempt must belong to a Task Graph task")
            if attempt.id in attempt_ids:
                raise ValueError("task attempt IDs must be unique")
            attempt_ids.add(attempt.id)
            attempts_by_task[attempt.task_id].append(attempt)
        for attempts in attempts_by_task.values():
            by_id = {attempt.id: attempt for attempt in attempts}
            sequences = [attempt.sequence for attempt in attempts]
            if len(sequences) != len(set(sequences)):
                raise ValueError("task attempt sequences must be unique per task")
            for attempt in attempts:
                if attempt.retry_of_attempt_id is None:
                    continue
                previous = by_id.get(attempt.retry_of_attempt_id)
                if previous is None or previous.sequence >= attempt.sequence:
                    raise ValueError(
                        "task retry must reference an earlier attempt on the same task"
                    )
                if previous.status is not TaskAttemptStatus.FAILED:
                    raise ValueError("task retry must reference a failed attempt")
        tasks_by_id = {task.id: task for task in self.tasks}
        for task_id, attempts in attempts_by_task.items():
            running_attempts = [
                attempt for attempt in attempts if attempt.status is TaskAttemptStatus.RUNNING
            ]
            task = tasks_by_id[task_id]
            if task.status is TaskStatus.RUNNING and len(running_attempts) != 1:
                raise ValueError("running task requires exactly one running attempt")
            if task.status is not TaskStatus.RUNNING and running_attempts:
                raise ValueError("only a running task may own a running attempt")
            if not attempts:
                continue
            latest = max(attempts, key=lambda attempt: attempt.sequence)
            if (
                task.status is TaskStatus.COMPLETED
                and latest.status is not TaskAttemptStatus.SUCCEEDED
            ):
                raise ValueError("completed task requires a succeeded latest attempt")
            if task.status is TaskStatus.FAILED and latest.status is not TaskAttemptStatus.FAILED:
                raise ValueError("failed task requires a failed latest attempt")

        budget_task_ids: list[str] = []
        for budget in self.budgets:
            if budget.run_id != self.run_id or budget.task_id not in known_tasks:
                raise ValueError("task budget must belong to a Task Graph task")
            budget_task_ids.append(budget.task_id)
        if len(budget_task_ids) != len(set(budget_task_ids)):
            raise ValueError("only one task budget is allowed per task")

        requirement_ids = [item.id for item in self.evidence_requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("task evidence requirement IDs must be unique")
        for requirement in self.evidence_requirements:
            if requirement.run_id != self.run_id or requirement.task_id not in known_tasks:
                raise ValueError("task evidence requirement must belong to a Task Graph task")
        return self

    @staticmethod
    def _require_acyclic(edges: dict[str, list[str]], label: str) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError(f"{label} must be acyclic")
            if node in visited:
                return
            visiting.add(node)
            for dependency in edges[node]:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for node in edges:
            visit(node)


@runtime_checkable
class TaskGraphRepository(Protocol):
    async def create(self, graph: TaskGraph) -> TaskGraph: ...

    async def get(self, run_id: str) -> TaskGraph | None: ...
