"""Runtime-only services captured by Agent function tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from riftx.application.ports import ApprovalRepository, FindingRepository, RunEventRepository
from riftx.runner import ProcessSupervisor
from riftx.skills import SkillRegistry
from riftx.tools import ToolRegistry


@dataclass(frozen=True, slots=True)
class AgentRuntimeServices:
    tool_registry: ToolRegistry
    skill_registry: SkillRegistry
    supervisor: ProcessSupervisor
    finding_repository: FindingRepository
    event_repository: RunEventRepository
    approval_repository: ApprovalRepository | None = None
    node_environment: dict[str, str | None] = field(default_factory=dict)
    run_environment: dict[str, str | None] = field(default_factory=dict)
