"""Run request and response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from riftx.application.services import CreateEngagement, CreateRun
from riftx.domain import (
    ApprovalMode,
    EntryPoint,
    EntryPointKind,
    Objective,
    Run,
    RunStatus,
    Scope,
    SuccessCriterion,
)


class EngagementCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    authorization_reference: str | None = None

    def to_command(self) -> CreateEngagement:
        return CreateEngagement(
            name=self.name,
            description=self.description,
            authorization_reference=self.authorization_reference,
        )


class SuccessCriterionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    required: bool = True


class EntryPointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EntryPointKind
    value: str = Field(min_length=1)
    metadata: dict[str, object] = Field(default_factory=dict)


class ScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cidrs: list[str] = Field(default_factory=list)
    ips: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    url_prefixes: list[str] = Field(default_factory=list)
    asset_tags: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1)
    node_id: str | None = Field(default=None, min_length=1)
    approval_mode: ApprovalMode = ApprovalMode.BALANCED
    model_profile: str | None = Field(default=None, min_length=1)
    success_criteria: list[SuccessCriterionRequest] = Field(default_factory=list)
    entry_points: list[EntryPointRequest] = Field(default_factory=list)
    scope: ScopeRequest = Field(default_factory=ScopeRequest)
    workspace_path: str | None = Field(default=None, min_length=1)
    engagement_id: str | None = Field(default=None, min_length=1)
    engagement: EngagementCreateRequest | None = None

    @model_validator(mode="after")
    def validate_engagement_source(self) -> CreateRunRequest:
        if self.engagement_id is not None and self.engagement is not None:
            raise ValueError("provide either engagement_id or engagement, not both")
        return self

    def to_command(self, *, default_node_id: str) -> CreateRun:
        return CreateRun(
            objective=self.objective,
            node_id=self.node_id or default_node_id,
            approval_mode=self.approval_mode,
            model_profile=self.model_profile,
            success_criteria=[
                SuccessCriterion(description=item.description, required=item.required)
                for item in self.success_criteria
            ],
            entry_points=[
                EntryPoint(kind=item.kind, value=item.value, metadata=item.metadata)
                for item in self.entry_points
            ],
            scope=Scope.model_validate(self.scope.model_dump()),
            workspace_path=self.workspace_path,
            engagement_id=self.engagement_id,
            engagement=self.engagement.to_command() if self.engagement else None,
        )


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    engagement_id: str
    node_id: str
    objective: Objective
    success_criteria: list[SuccessCriterion]
    entry_points: list[EntryPoint]
    scope: Scope
    status: RunStatus
    approval_mode: ApprovalMode
    model_profile: str | None
    workspace_path: str
    temporal_workflow_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_domain(cls, run: Run) -> RunResponse:
        return cls.model_validate(run)


class RunListResponse(BaseModel):
    items: list[RunResponse]
    limit: int
    offset: int


class RunActionResponse(BaseModel):
    accepted: bool = True
    run: RunResponse


class RunMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)


class CompactRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_history_items: int = Field(default=100, ge=1, le=10_000)


class SwitchRunModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_profile: str = Field(min_length=1)
