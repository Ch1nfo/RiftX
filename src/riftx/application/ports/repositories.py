"""Repository interfaces consumed by application services."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from riftx.domain import (
    Approval,
    ApprovalGrant,
    ApprovalStatus,
    Artifact,
    Engagement,
    Execution,
    Finding,
    FindingSeverity,
    FindingStatus,
    Node,
    NodeStatus,
    Report,
    ReportFormat,
    Run,
    RunEvent,
    RunStatus,
    TerminalSession,
    ToolCall,
)


class EngagementRepository(Protocol):
    async def create(self, engagement: Engagement) -> Engagement: ...

    async def get(self, engagement_id: str) -> Engagement | None: ...


class NodeRepository(Protocol):
    async def create(self, node: Node) -> Node: ...

    async def get(self, node_id: str) -> Node | None: ...

    async def save(self, node: Node) -> Node: ...

    async def list(
        self,
        *,
        status: NodeStatus | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Sequence[Node]: ...


class RunRepository(Protocol):
    async def create(self, run: Run) -> Run: ...

    async def get(self, run_id: str) -> Run | None: ...

    async def list(
        self,
        *,
        status: RunStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Run]: ...

    async def update_status(self, run_id: str, target: RunStatus) -> Run: ...


class RunEventRepository(Protocol):
    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
    ) -> RunEvent: ...

    async def list_after(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> Sequence[RunEvent]: ...


class ApprovalRepository(Protocol):
    async def create_request(
        self,
        tool_call: ToolCall,
        approval: Approval,
    ) -> tuple[Approval, bool]: ...

    async def get(self, approval_id: str) -> Approval | None: ...

    async def get_tool_call(self, tool_call_id: str) -> ToolCall | None: ...

    async def list(
        self,
        run_id: str,
        *,
        status: ApprovalStatus | None = None,
    ) -> Sequence[Approval]: ...

    async def decide(
        self,
        approval_id: str,
        status: ApprovalStatus,
        *,
        decided_by: str,
        reason: str | None = None,
    ) -> tuple[Approval, bool]: ...

    async def grant_for_run(
        self,
        run_id: str,
        tool_id: str,
        *,
        created_by: str,
    ) -> ApprovalGrant: ...

    async def is_granted(self, run_id: str, tool_id: str) -> bool: ...


class ExecutionRepository(Protocol):
    async def create_if_absent(self, execution: Execution) -> tuple[Execution, bool]: ...

    async def get(self, execution_id: str) -> Execution | None: ...

    async def get_by_key(self, execution_key: str) -> Execution | None: ...

    async def save(self, execution: Execution) -> Execution: ...

    async def list_active(self) -> Sequence[Execution]: ...


class TerminalRepository(Protocol):
    async def create(self, terminal: TerminalSession) -> TerminalSession: ...

    async def get(self, session_id: str) -> TerminalSession | None: ...

    async def save(self, terminal: TerminalSession) -> TerminalSession: ...

    async def list_open(self) -> Sequence[TerminalSession]: ...


class ArtifactRepository(Protocol):
    async def create(self, artifact: Artifact) -> Artifact: ...

    async def get(self, artifact_id: str) -> Artifact | None: ...

    async def list(
        self,
        run_id: str,
        *,
        execution_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Artifact]: ...


class FindingRepository(Protocol):
    async def create(self, finding: Finding) -> Finding: ...

    async def get(self, finding_id: str) -> Finding | None: ...

    async def save(self, finding: Finding) -> Finding: ...

    async def list(
        self,
        run_id: str,
        *,
        severity: FindingSeverity | None = None,
        status: FindingStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Finding]: ...


class ReportRepository(Protocol):
    async def create(self, report: Report) -> Report: ...

    async def get(self, report_id: str) -> Report | None: ...

    async def list(
        self,
        run_id: str,
        *,
        format: ReportFormat | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Report]: ...
