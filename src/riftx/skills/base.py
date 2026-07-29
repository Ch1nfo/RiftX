"""Executable Skill contracts independent of any model SDK."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from riftx.domain import ApprovalLevel, ExecutionStatus
from riftx.runner import ExecutionRunner
from riftx.tools import ToolRegistry


@dataclass(frozen=True, slots=True)
class SkillContext:
    run_id: str
    node_id: str
    agent_step_id: str
    cwd: Path
    supervisor: ExecutionRunner
    tool_registry: ToolRegistry
    node_environment: dict[str, str | None] = field(default_factory=dict)
    run_environment: dict[str, str | None] = field(default_factory=dict)
    stdout_excerpt_bytes: int = 16 * 1024
    stderr_excerpt_bytes: int = 8 * 1024


class SkillResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    summary: str
    structured: dict[str, object] = Field(default_factory=dict)
    stdout_excerpt: bytes = b""
    stderr_excerpt: bytes = b""
    artifact_ids: list[str] = Field(default_factory=list)
    execution_id: str
    status: ExecutionStatus
    exit_code: int | None = None


class BaseSkill(ABC):
    id: ClassVar[str]
    description: ClassVar[str]
    required_capabilities: ClassVar[frozenset[str]] = frozenset()
    preferred_tools: ClassVar[tuple[str, ...]] = ()
    approval_level: ClassVar[ApprovalLevel] = ApprovalLevel.NEVER
    arguments_model: ClassVar[type[BaseModel]]

    @abstractmethod
    async def execute(self, context: SkillContext, arguments: BaseModel) -> SkillResult:
        """Execute the skill and return a bounded Agent-facing result."""
