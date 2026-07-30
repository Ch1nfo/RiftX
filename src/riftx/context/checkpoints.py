"""Provider-neutral context checkpoints used for compaction and model switching."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from riftx.domain.base import DomainModel, new_id, utc_now


class CheckpointType(StrEnum):
    CONVERSATION_SUMMARY = "conversation_summary"
    CANONICAL = "canonical"
    EMERGENCY = "emergency"
    MODEL_SWITCH = "model_switch"


class CompactionStage(StrEnum):
    TOOL_PREVIEW_CLEANUP = "tool_preview_cleanup"
    CONVERSATION_SUMMARY = "conversation_summary"
    CANONICAL_CHECKPOINT = "canonical_checkpoint"
    EMERGENCY_COMPACTION = "emergency_compaction"


class ContextCheckpoint(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    checkpoint_type: CheckpointType
    compaction_stage: CompactionStage
    model_profile: str = Field(min_length=1)

    objective: str = Field(min_length=1)
    success_criteria: list[dict[str, object]] = Field(default_factory=list)
    scope: dict[str, object]
    current_phase: str = Field(min_length=1)
    plan: dict[str, object]
    completed_work: list[dict[str, object]] = Field(default_factory=list)
    confirmed_facts: list[dict[str, object]] = Field(default_factory=list)
    hypotheses: list[dict[str, object]] = Field(default_factory=list)
    failed_attempts: list[dict[str, object]] = Field(default_factory=list)
    user_decisions: list[dict[str, object]] = Field(default_factory=list)
    pending_approval_ids: list[str] = Field(default_factory=list)
    active_execution_ids: list[str] = Field(default_factory=list)
    active_terminal_ids: list[str] = Field(default_factory=list)
    unresolved_questions: list[dict[str, object]] = Field(default_factory=list)
    next_action: dict[str, object] | None = None

    context_compilation_id: str | None = None
    context_manifest_id: str | None = None
    working_memory_version: int | None = Field(default=None, ge=1)
    provider_state_id: str | None = None
    retained_message_ids: list[str] = Field(default_factory=list)
    retained_tool_result_ids: list[str] = Field(default_factory=list)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_canonical_resume_state(self) -> ContextCheckpoint:
        if not self.plan:
            raise ValueError("context checkpoint requires a current plan")
        if self.context_compilation_id and not self.context_manifest_id:
            raise ValueError(
                "context checkpoint with a compilation requires a context manifest ID"
            )
        return self


def compaction_stage_for_usage(usage_ratio: float) -> CompactionStage | None:
    if usage_ratio >= 0.90:
        return CompactionStage.EMERGENCY_COMPACTION
    if usage_ratio >= 0.82:
        return CompactionStage.CANONICAL_CHECKPOINT
    if usage_ratio >= 0.70:
        return CompactionStage.CONVERSATION_SUMMARY
    if usage_ratio >= 0.55:
        return CompactionStage.TOOL_PREVIEW_CLEANUP
    return None
