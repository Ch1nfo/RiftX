"""Run aggregate and task constraints."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .base import DomainModel, new_id, utc_now
from .enums import (
    ApprovalMode,
    EntryPointKind,
    PentestProhibitedAction,
    PentestStopCondition,
    RunKind,
    RunStatus,
)
from .errors import InvalidStateTransitionError

_RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset(
        {
            RunStatus.INITIALIZING,
            RunStatus.WAITING_USER,
            RunStatus.PREPARING,
            RunStatus.COMPLETING,
            RunStatus.PAUSING,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.INITIALIZING: frozenset(
        {
            RunStatus.READY,
            RunStatus.COMPLETING,
            RunStatus.PAUSING,
            RunStatus.FAILED,
            RunStatus.CANCELLING,
        }
    ),
    RunStatus.READY: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.COMPLETING,
            RunStatus.PAUSING,
            RunStatus.FAILED,
            RunStatus.CANCELLING,
        }
    ),
    RunStatus.PREPARING: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.COMPLETING,
            RunStatus.PAUSING,
            RunStatus.FAILED,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_TOOL,
            RunStatus.WAITING_APPROVAL,
            RunStatus.WAITING_USER,
            RunStatus.PAUSING,
            RunStatus.PAUSED,
            RunStatus.COMPACTING,
            RunStatus.COMPLETING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_TOOL: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.COMPLETING,
            RunStatus.PAUSING,
            RunStatus.FAILED,
            RunStatus.CANCELLING,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.PAUSING,
            RunStatus.PAUSED,
            RunStatus.COMPLETING,
            RunStatus.FAILED,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_USER: frozenset(
        {
            RunStatus.PREPARING,
            RunStatus.RUNNING,
            RunStatus.COMPLETING,
            RunStatus.PAUSING,
            RunStatus.FAILED,
            RunStatus.CANCELLING,
        }
    ),
    RunStatus.PAUSING: frozenset({RunStatus.PAUSED, RunStatus.CANCELLING}),
    RunStatus.PAUSED: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.WAITING_USER,
            RunStatus.COMPLETING,
            RunStatus.FAILED,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.COMPACTING: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.COMPLETING,
            RunStatus.PAUSING,
            RunStatus.FAILED,
            RunStatus.CANCELLING,
        }
    ),
    RunStatus.COMPLETING: frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLING}),
    RunStatus.CANCELLING: frozenset({RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class Objective(DomainModel):
    description: str = Field(min_length=1)


class SuccessCriterion(DomainModel):
    description: str = Field(min_length=1)
    required: bool = True


class EntryPoint(DomainModel):
    kind: EntryPointKind
    value: str = Field(min_length=1)
    metadata: dict[str, object] = Field(default_factory=dict)


class Scope(DomainModel):
    cidrs: list[str] = Field(default_factory=list)
    ips: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    url_prefixes: list[str] = Field(default_factory=list)
    asset_tags: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None

    @field_validator("cidrs", "ips", "domains", "url_prefixes", "asset_tags", "exclusions")
    @classmethod
    def normalize_unique_values(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            if item and item not in seen:
                normalized.append(item)
                seen.add(item)
        return normalized

    @model_validator(mode="after")
    def validate_time_range(self) -> Scope:
        if self.starts_at and self.ends_at and self.starts_at >= self.ends_at:
            raise ValueError("scope starts_at must be earlier than ends_at")
        return self


class PentestBudget(DomainModel):
    max_duration_seconds: int = Field(gt=0)
    max_model_calls: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_target_interactions: int = Field(gt=0)
    max_concurrent_target_interactions: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_target_concurrency(self) -> PentestBudget:
        if self.max_concurrent_target_interactions > self.max_target_interactions:
            raise ValueError(
                "max_concurrent_target_interactions must not exceed "
                "max_target_interactions"
            )
        return self


_REQUIRED_PENTEST_PROHIBITIONS = (
    PentestProhibitedAction.DENIAL_OF_SERVICE,
    PentestProhibitedAction.DESTRUCTIVE_DATA_MODIFICATION,
    PentestProhibitedAction.PERSISTENCE,
    PentestProhibitedAction.OUT_OF_SCOPE_LATERAL_MOVEMENT,
)
_REQUIRED_PENTEST_STOP_CONDITIONS = (
    PentestStopCondition.SCOPE_VIOLATION,
    PentestStopCondition.SCOPE_WINDOW_EXPIRED,
    PentestStopCondition.BUDGET_EXHAUSTED,
    PentestStopCondition.OPERATOR_STOP,
    PentestStopCondition.RUN_CANCELLED,
)


class PentestAdmission(DomainModel):
    budget: PentestBudget
    prohibited_actions: list[PentestProhibitedAction] = Field(
        default_factory=lambda: list(_REQUIRED_PENTEST_PROHIBITIONS)
    )
    stop_conditions: list[PentestStopCondition] = Field(
        default_factory=lambda: list(_REQUIRED_PENTEST_STOP_CONDITIONS)
    )

    @field_validator("prohibited_actions", "stop_conditions")
    @classmethod
    def normalize_unique_enum_values(cls, values: list[object]) -> list[object]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_mandatory_safety_contract(self) -> PentestAdmission:
        if not set(_REQUIRED_PENTEST_PROHIBITIONS).issubset(self.prohibited_actions):
            raise ValueError("pentest admission must retain all required prohibited actions")
        if not set(_REQUIRED_PENTEST_STOP_CONDITIONS).issubset(self.stop_conditions):
            raise ValueError("pentest admission must retain all required stop conditions")
        return self


class Run(DomainModel):
    id: str = Field(default_factory=new_id)
    engagement_id: str
    node_id: str = Field(min_length=1, max_length=64)
    objective: Objective
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
    entry_points: list[EntryPoint] = Field(default_factory=list)
    scope: Scope = Field(default_factory=Scope)
    kind: RunKind = Field(frozen=True)
    status: RunStatus = RunStatus.CREATED
    approval_mode: ApprovalMode = ApprovalMode.BALANCED
    pentest_admission: PentestAdmission | None = None
    model_profile: str | None = Field(default=None, min_length=1, max_length=255)
    workspace_path: str
    temporal_workflow_id: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_kind_contract(self) -> Run:
        if self.kind is not RunKind.PENTEST:
            if self.pentest_admission is not None:
                raise ValueError("only Pentest Runs may carry pentest admission")
            return self
        if self.pentest_admission is None:
            raise ValueError("Pentest Runs require pentest admission")
        if not any(
            (self.scope.cidrs, self.scope.ips, self.scope.domains, self.scope.url_prefixes)
        ):
            raise ValueError("Pentest Runs require a concrete positive network scope")
        if not self.entry_points:
            raise ValueError("Pentest Runs require at least one network entry point")
        network_kinds = {
            EntryPointKind.CIDR,
            EntryPointKind.IP,
            EntryPointKind.DOMAIN,
            EntryPointKind.URL,
        }
        if any(
            entry.kind not in network_kinds or not entry.value.strip()
            for entry in self.entry_points
        ):
            raise ValueError("Pentest Run entry points must be non-empty network targets")
        return self

    def transition_to(self, target: RunStatus, *, at: AwareDatetime | None = None) -> None:
        """Move the run to a valid next state and maintain lifecycle timestamps."""

        if target not in _RUN_TRANSITIONS[self.status]:
            raise InvalidStateTransitionError("Run", self.status, target)

        changed_at = at or utc_now()
        self.status = target
        if target is RunStatus.RUNNING and self.started_at is None:
            self.started_at = changed_at
        if target in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            self.finished_at = changed_at

    def can_transition_to(self, target: RunStatus) -> bool:
        return target in _RUN_TRANSITIONS[self.status]
