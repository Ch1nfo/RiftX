"""Typed Planner commands and durable scheduling results."""

from __future__ import annotations

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from riftx.domain.base import DomainModel, new_id

from .models import Task, TaskAttempt


class TaskBudgetInput(DomainModel):
    max_model_calls: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_duration_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_a_limit(self) -> TaskBudgetInput:
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


class TaskEvidenceRequirementInput(DomainModel):
    id: str = Field(default_factory=new_id, min_length=1)
    evidence_type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    minimum_count: int = Field(default=1, ge=1)


class AddTaskCommand(DomainModel):
    run_id: str = Field(min_length=1)
    expected_graph_version: int = Field(ge=0)
    task_id: str = Field(default_factory=new_id, min_length=1)
    parent_task_id: str | None = None
    sequence: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1)
    description: str = ""
    input_scope: dict[str, JsonValue] = Field(default_factory=dict)
    expected_output_schema: dict[str, JsonValue] = Field(default_factory=dict)
    required_capability_ids: list[str] = Field(default_factory=list)
    workspace_owner: str | None = None
    session_owner_id: str | None = None
    stop_condition: str | None = None
    budget: TaskBudgetInput | None = None
    evidence_requirements: list[TaskEvidenceRequirementInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_collections(self) -> AddTaskCommand:
        if self.parent_task_id == self.task_id:
            raise ValueError("task cannot be its own parent")
        if len(self.required_capability_ids) != len(set(self.required_capability_ids)):
            raise ValueError("task capability requirements must be unique")
        requirement_ids = [item.id for item in self.evidence_requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("task evidence requirement IDs must be unique")
        return self


class UpdateTaskCommand(DomainModel):
    run_id: str = Field(min_length=1)
    expected_graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    sequence: int | None = Field(default=None, ge=1)
    input_scope: dict[str, JsonValue] | None = None
    expected_output_schema: dict[str, JsonValue] | None = None
    required_capability_ids: list[str] | None = None
    workspace_owner: str | None = None
    session_owner_id: str | None = None
    stop_condition: str | None = None

    @model_validator(mode="after")
    def require_a_change(self) -> UpdateTaskCommand:
        fields = self.model_fields_set - {
            "run_id",
            "expected_graph_version",
            "task_id",
        }
        if not fields:
            raise ValueError("update_task requires at least one changed field")
        for field in (
            "title",
            "description",
            "sequence",
            "input_scope",
            "expected_output_schema",
            "required_capability_ids",
        ):
            if field in fields and getattr(self, field) is None:
                raise ValueError(f"update_task field {field!r} cannot be null")
        if self.required_capability_ids is not None and len(self.required_capability_ids) != len(
            set(self.required_capability_ids)
        ):
            raise ValueError("task capability requirements must be unique")
        return self


class LinkTasksCommand(DomainModel):
    run_id: str = Field(min_length=1)
    expected_graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1)
    depends_on_task_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def reject_self_dependency(self) -> LinkTasksCommand:
        if self.task_id == self.depends_on_task_id:
            raise ValueError("task cannot depend on itself")
        return self


class BlockTaskCommand(DomainModel):
    run_id: str = Field(min_length=1)
    expected_graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class CompleteTaskCommand(DomainModel):
    run_id: str = Field(min_length=1)
    expected_graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    completion_summary: str = Field(min_length=1)
    evidence_refs_by_requirement: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_unique_evidence_refs(self) -> CompleteTaskCommand:
        for refs in self.evidence_refs_by_requirement.values():
            if len(refs) != len(set(refs)):
                raise ValueError("completion evidence references must be unique")
        return self


class FailTaskAttemptCommand(DomainModel):
    run_id: str = Field(min_length=1)
    expected_graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    failure_summary: str = Field(min_length=1)


class ReopenTaskCommand(DomainModel):
    run_id: str = Field(min_length=1)
    expected_graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class CancelTaskCommand(DomainModel):
    run_id: str = Field(min_length=1)
    expected_graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ClaimReadyTaskCommand(DomainModel):
    run_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    session_id: str | None = None
    preferred_task_id: str | None = None
    claimed_at: AwareDatetime | None = None


class TaskMutationResult(DomainModel):
    graph_version: int = Field(ge=1)
    task: Task
    attempt: TaskAttempt | None = None
