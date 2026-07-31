"""Repository interfaces consumed by application services."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Protocol

from riftx.application.finalization import RunFinalizationIntent
from riftx.domain import (
    Approval,
    ApprovalGrant,
    ApprovalStatus,
    Artifact,
    Engagement,
    Execution,
    ExecutionStatus,
    Finding,
    FindingSeverity,
    FindingStatus,
    Node,
    NodeStatus,
    Report,
    ReportFormat,
    Run,
    RunEvent,
    RunnerCommand,
    RunnerCommandStatus,
    RunnerCredential,
    RunnerPrincipal,
    RunStatus,
    TerminalSession,
    TerminalStatus,
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


class RunnerCredentialRepository(Protocol):
    async def issue(
        self,
        node_id: str,
        *,
        token_hash: str,
        token_prefix: str,
        issued_at: datetime,
        instance_id: str | None = None,
    ) -> RunnerCredential: ...

    async def get(self, node_id: str) -> RunnerCredential | None: ...

    async def get_current(self, node_id: str) -> RunnerCredential | None: ...

    async def get_by_principal(
        self,
        node_id: str,
        principal: RunnerPrincipal,
    ) -> RunnerCredential | None: ...

    async def get_by_token_hash(
        self,
        node_id: str,
        token_hash: str,
    ) -> RunnerCredential | None: ...

    async def save(self, credential: RunnerCredential) -> RunnerCredential: ...


class RunnerCommandRepository(Protocol):
    async def enqueue(self, command: RunnerCommand) -> tuple[RunnerCommand, bool]: ...

    async def get(self, command_id: str) -> RunnerCommand | None: ...

    async def lease_next(
        self,
        node_id: str,
        *,
        principal: RunnerPrincipal,
        lease_id: str,
        leased_until: datetime,
        now: datetime,
        safety_only: bool = False,
    ) -> RunnerCommand | None: ...

    async def renew_lease(
        self,
        command_id: str,
        *,
        principal: RunnerPrincipal,
        lease_id: str,
        leased_until: datetime,
        now: datetime,
    ) -> RunnerCommand: ...

    async def finish(
        self,
        command_id: str,
        *,
        principal: RunnerPrincipal,
        lease_id: str,
        status: RunnerCommandStatus,
        result: dict[str, object],
        error: str,
        completed_at: datetime,
    ) -> RunnerCommand: ...


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

    async def list_for_reconciliation(
        self,
        *,
        status: RunStatus,
        created_through: datetime,
        after_created_at: datetime | None = None,
        after_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[Run]: ...

    async def update_status(self, run_id: str, target: RunStatus) -> Run: ...

    async def complete_if_no_pending_user_messages(
        self,
        run_id: str,
        *,
        consumed_user_message_ids: Sequence[str],
    ) -> tuple[Run, Sequence[str]]: ...

    async def fence_completion_if_no_pending_user_messages(
        self,
        run_id: str,
        *,
        consumed_user_message_ids: Sequence[str],
        defer_cleanup_event: bool = False,
    ) -> tuple[Run, Sequence[str]]: ...

    async def fence_finalization(
        self,
        run_id: str,
        target: RunStatus,
        *,
        defer_cleanup_event: bool = False,
    ) -> Run: ...

    async def commit_finalization(
        self,
        run_id: str,
        target: RunStatus,
        *,
        defer_cleanup_event: bool = False,
    ) -> Run: ...

    async def record_finalization_intent(
        self,
        run_id: str,
        target: RunStatus,
        *,
        defer_cleanup_event: bool = False,
    ) -> Run: ...

    async def get_finalization_intent(self, run_id: str) -> RunFinalizationIntent | None: ...

    async def update_model_profile(self, run_id: str, model_profile: str) -> Run: ...


class RunEventRepository(Protocol):
    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        event_id: str | None = None,
    ) -> RunEvent: ...

    async def get(self, event_id: str) -> RunEvent | None: ...

    async def append_terminal_projection_if_current(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        event_id: str,
        session_id: str,
        expected_terminal_status: TerminalStatus,
        expected_execution_status: ExecutionStatus,
    ) -> RunEvent | None:
        """Append only while the durable Terminal and Execution still match.

        Returning ``None`` means a newer projection won the database race.
        Implementations must make the state check and append one serialized
        transaction so a late lower-state event cannot follow a higher one.
        """
        ...

    async def append_user_message(
        self,
        run_id: str,
        message: str,
        *,
        event_id: str | None = None,
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
        blocked_run_statuses: Collection[RunStatus] = (),
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

    async def save_if_status(
        self,
        execution: Execution,
        *,
        expected: Collection[ExecutionStatus],
    ) -> tuple[Execution, bool]: ...

    async def list(
        self,
        run_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Execution]: ...

    async def list_active(self) -> Sequence[Execution]: ...


class TerminalRepository(Protocol):
    async def create(self, terminal: TerminalSession) -> TerminalSession: ...

    async def get(self, session_id: str) -> TerminalSession | None: ...

    async def get_by_execution(self, execution_id: str) -> TerminalSession | None: ...

    async def save(self, terminal: TerminalSession) -> TerminalSession: ...

    async def save_if_status(
        self,
        terminal: TerminalSession,
        *,
        expected: Collection[TerminalStatus],
    ) -> tuple[TerminalSession, bool]: ...

    async def list_open(self) -> Sequence[TerminalSession]: ...

    async def list_active(self) -> Sequence[TerminalSession]: ...


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
