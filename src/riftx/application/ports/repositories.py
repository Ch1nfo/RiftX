"""Repository interfaces consumed by application services."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from riftx.application.actions import (
    ActionAggregateRead,
    ActionPageKey,
    ActionReadPage,
)
from riftx.application.finalization import RunFinalizationIntent
from riftx.domain import (
    Approval,
    ApprovalDecision,
    ApprovalGrant,
    ApprovalStatus,
    Artifact,
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
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
from riftx.runtime.types import ToolCallIntent, ToolCallStatus


@dataclass(frozen=True, slots=True)
class ToolCallIntentExecutionClaim:
    """Store-issued receipt for one exact deferred-execution claim."""

    intent: ToolCallIntent
    acquired: bool
    newly_acquired: bool
    execution_key: str
    attempt_group: str
    previous_status: ToolCallStatus | None = None
    previous_execution_key: str | None = None
    previous_attempt_group: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionAdmissionIdentity:
    """Exact logical identity of one durable execution admission.

    A matching key or deterministic ID alone is never sufficient: either can
    collide with a row owned by another Run, Session, Tool Call, attempt, or
    executor path. Historical rows may legitimately predate launch
    fingerprints, so a NULL persisted fingerprint is accepted only after all
    reconstructable identity fields match.
    """

    execution_key: str
    run_id: str
    session_id: str | None
    tool_call_id: str | None
    attempt_group: str | None
    executor_type: ExecutorType
    node_id: str
    argv: tuple[str, ...]
    command_text: str | None
    tool_id: str | None
    tool_version: str | None
    cwd: str
    env: dict[str, str | None]
    execution_id: str | None = None
    launch_fingerprint: str | None = None

    def matches(self, execution: Execution) -> bool:
        if self.execution_id is not None and execution.id != self.execution_id:
            return False
        if (
            execution.execution_key != self.execution_key
            or execution.run_id != self.run_id
            or execution.session_id != self.session_id
            or execution.tool_call_id != self.tool_call_id
            or execution.attempt_group != self.attempt_group
            or execution.executor_type is not self.executor_type
            or execution.node_id != self.node_id
            or execution.command_text != self.command_text
            or execution.tool_id != self.tool_id
            or execution.tool_version != self.tool_version
            or _canonical_path(execution.cwd) != _canonical_path(self.cwd)
            or execution.env_diff != self.env
        ):
            return False
        expected_argv = list(self.argv)
        if execution.argv != expected_argv and not (
            self.executor_type is ExecutorType.SHELL and not expected_argv and bool(execution.argv)
        ):
            return False
        return self.launch_fingerprint is None or execution.launch_fingerprint in {
            None,
            self.launch_fingerprint,
        }


def _canonical_path(value: str) -> str:
    return str(Path(value).expanduser().resolve())


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

    async def decide_runtime(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        decided_by: str,
        feedback: str | None = None,
        blocked_run_statuses: Collection[RunStatus] = (),
    ) -> tuple[Approval, bool]:
        """Atomically persist the public/runtime tuple, grant, and decision event."""
        ...

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

    async def find_admission(
        self,
        identity: ExecutionAdmissionIdentity,
    ) -> Execution | None:
        """Return a row only when its complete logical admission identity matches."""
        ...

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


class ToolCallIntentRepository(Protocol):
    async def create(self, intent: ToolCallIntent) -> ToolCallIntent: ...

    async def get(self, intent_id: str) -> ToolCallIntent | None: ...

    async def pending_for_session(self, session_id: str) -> list[ToolCallIntent]: ...

    async def active_for_run(
        self,
        run_id: str,
        *,
        tool_ids: Collection[str] | None = None,
    ) -> list[ToolCallIntent]: ...

    async def compare_and_set_status(
        self,
        intent_id: str,
        *,
        expected: Collection[ToolCallStatus],
        target: ToolCallStatus,
    ) -> tuple[ToolCallIntent, bool]: ...

    async def claim_execution(
        self,
        intent_id: str,
        *,
        execution_key: str,
        attempt_group: str,
    ) -> ToolCallIntentExecutionClaim: ...

    async def execution_claim_is_current(
        self,
        intent_id: str,
        *,
        execution_key: str,
        attempt_group: str,
    ) -> bool: ...

    async def project_execution_status(
        self,
        intent_id: str,
        *,
        execution_key: str,
        attempt_group: str,
        expected: Collection[ToolCallStatus],
        target: ToolCallStatus,
    ) -> tuple[ToolCallIntent, bool]: ...

    async def adopt_execution_claim(
        self,
        intent_id: str,
        *,
        execution_id: str,
        execution_key: str,
        attempt_group: str,
    ) -> tuple[ToolCallIntent, bool]: ...

    async def rollback_execution_claim(
        self,
        claim: ToolCallIntentExecutionClaim,
        *,
        admission: ExecutionAdmissionIdentity,
    ) -> tuple[ToolCallIntent, bool]: ...

    async def save(self, intent: ToolCallIntent) -> ToolCallIntent:
        """Persist mutable metadata only; status and claims remain store-owned."""
        ...


class ActionReadRepository(Protocol):
    """Batch-oriented persistence port for the application Action projection.

    Aggregate ``updated_at`` values are aware high-water marks for every
    Action-projected child metadata change, including children omitted or truncated
    from the returned projection. Non-projected database fields are outside this contract.
    """

    async def resolve_run(self, run_id: str) -> str | None: ...

    async def resolve_action_run(self, run_id: str, action_id: str) -> str | None: ...

    async def list_page(
        self,
        run_id: str,
        *,
        limit: int,
        after: ActionPageKey | None,
        snapshot: ActionPageKey | None,
    ) -> ActionReadPage:
        """Return at most ``limit + 1`` rows; the extra row is a pagination sentinel."""
        ...

    async def get(self, run_id: str, action_id: str) -> ActionAggregateRead | None: ...


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

    async def target_http_sensitive_ids(
        self,
        artifact_ids: Collection[str],
    ) -> frozenset[str]:
        """Classify IDs by global Target HTTP association without materializing Artifacts."""
        ...


class FindingRepository(Protocol):
    async def create(self, finding: Finding) -> Finding:
        """Persist and return repository-owned creation and mutation timestamps."""
        ...

    async def get(self, finding_id: str) -> Finding | None: ...

    async def save(
        self,
        finding: Finding,
        *,
        expected_updated_at: datetime,
    ) -> tuple[Finding, bool]:
        """CAS mutable payload and preserve ``updated_at`` as a monotonic high-water mark."""
        ...

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
