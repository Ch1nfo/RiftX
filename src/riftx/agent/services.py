"""Runtime-only services captured by Agent function tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from riftx.application.ports import ApprovalRepository, FindingRepository, RunEventRepository
from riftx.application.services import (
    ArtifactApplicationService,
    FindingApplicationService,
    TerminalApplicationService,
)
from riftx.runner import ExecutionRunner
from riftx.skills import SkillRegistry
from riftx.tools import ToolRegistry


@dataclass(frozen=True, slots=True)
class AgentRuntimeServices:
    tool_registry: ToolRegistry
    skill_registry: SkillRegistry
    supervisor: ExecutionRunner
    finding_repository: FindingRepository
    event_repository: RunEventRepository
    finding_service: FindingApplicationService
    artifact_service: ArtifactApplicationService
    terminal_service: TerminalApplicationService
    approval_repository: ApprovalRepository | None = None
    node_environment: dict[str, str | None] = field(default_factory=dict)
    run_environment: dict[str, str | None] = field(default_factory=dict)
