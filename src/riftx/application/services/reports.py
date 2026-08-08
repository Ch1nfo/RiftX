"""Restricted report composition and immutable report generation."""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, Field

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    resource_not_accessible,
)
from riftx.application.event_projection import (
    redact_sensitive_event,
    target_http_artifact_candidates,
)
from riftx.application.ports import (
    ArtifactRepository,
    EngagementRepository,
    ExecutionRepository,
    FindingRepository,
    PentestStatusReader,
    ReportRepository,
    RunEventRepository,
    RunRepository,
)
from riftx.context.working_memory import AttemptRecord, WorkingMemoryRepository
from riftx.domain import (
    ArtifactContentTrust,
    Engagement,
    EntryPoint,
    EntryPointKind,
    Execution,
    Finding,
    Report,
    ReportFormat,
    Run,
    RunEvent,
    RunKind,
    RunStatus,
)
from riftx.domain.base import utc_now
from riftx.evidence import (
    ArtifactSpanLocator,
    CodeLocationLocator,
    Evidence,
    EvidenceLedgerRepository,
)
from riftx.reasoning import ReasoningGraphRepository, ReasoningNode
from riftx.target_http.redaction import safe_url_metadata
from riftx.tasks import Task, TaskGraphRepository

from .artifacts import ArtifactApplicationService, RegisterArtifactContent
from .closure import CLOSURE_EVALUATED_EVENT_TYPE, ClosureOutcome
from .runs import require_interactive_run_operation

_MAX_SUMMARY_LENGTH = 2_000
_MAX_TEXT_LENGTH = 10_000
_REPORT_EVENT_FIELDS: Mapping[str, frozenset[str]] = {
    "run.created": frozenset({"status"}),
    "run.status_changed": frozenset({"from", "to", "status"}),
    "run.pause_requested": frozenset(
        {"workflow_synced", "failed_resource_types", "pause_fence_acquired"}
    ),
    "run.resume_requested": frozenset(),
    "run.cleanup_reconciled": frozenset({"workflow_synced", "failed_resource_types"}),
    "pentest.budget_exhausted": frozenset({"budget_name", "limit", "used", "reason"}),
    "agent.plan_updated": frozenset({"agent_step_id", "plan_summary"}),
    "agent.completion_requested": frozenset({"agent_step_id", "run_summary"}),
    "agent.cycle_completed": frozenset(
        {"agent_step_id", "completed", "needs_input", "run_summary"}
    ),
    "finding.created": frozenset({"finding_id", "agent_step_id", "severity", "status"}),
    "finding.updated": frozenset({"finding_id", "severity", "status"}),
    "artifact.registered": frozenset(
        {
            "artifact_id",
            "execution_id",
            "name",
            "mime_type",
            "sha256",
            "size",
            "artifact_class",
            "content_restricted",
        }
    ),
    "tool.approval_required": frozenset(
        {"approval_id", "tool_call_id", "tool_name", "agent_step_id"}
    ),
    "tool.approved": frozenset({"approval_id", "tool_call_id", "tool_name", "decided_by"}),
    "tool.rejected": frozenset({"approval_id", "tool_call_id", "tool_name", "decided_by"}),
    "agent.tool_completed": frozenset(
        {
            "agent_step_id",
            "tool",
            "registered_tool_id",
            "tool_call_id",
            "execution_id",
            "status",
            "exit_code",
        }
    ),
    "agent.tool_failed": frozenset(
        {"agent_step_id", "tool", "registered_tool_id", "tool_call_id", "error_type"}
    ),
    CLOSURE_EVALUATED_EVENT_TYPE: frozenset(
        {
            "outcome",
            "reason_codes",
            "report_digest",
            "task_graph_version",
            "reasoning_graph_version",
            "success_criterion_count",
            "satisfied_success_criterion_count",
            "incomplete_task_count",
            "confirmed_finding_count",
            "replayable_confirmed_finding_count",
        }
    ),
}


class ReportEvidence(BaseModel):
    artifact_id: str | None = None
    execution_id: str | None = None
    description: str = ""
    location: str | None = None
    content_url: str | None = None


class ReportFinding(BaseModel):
    id: str
    title: str
    severity: str
    status: str
    affected_assets: list[str] = Field(default_factory=list)
    description: str = ""
    evidence: list[ReportEvidence] = Field(default_factory=list)
    reproduction_steps: list[str] = Field(default_factory=list)
    impact: str = ""
    recommendation: str = ""


class ReportArtifactSummary(BaseModel):
    id: str
    execution_id: str | None = None
    name: str
    mime_type: str
    sha256: str
    size: int
    description: str = ""
    content_url: str


class ReportEventSummary(BaseModel):
    sequence: int
    event_type: str
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: AwareDatetime


class ReportEngagement(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    authorization_reference_present: bool = False
    authorization_reference_scheme: str | None = None


class ReportPentestAdmission(BaseModel):
    approval_mode: str
    model_profile: str | None = None
    entry_points: list[dict[str, object]] = Field(default_factory=list)
    prohibited_actions: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)


class ReportCapabilitySelection(BaseModel):
    kind: str
    capability_id: str
    version: str
    digest: str
    source: str
    active: bool


class ReportPackLock(BaseModel):
    lock_id: str
    capability_id: str
    capability_version_id: str
    capability_version: str
    capability_digest: str
    active: bool


class ReportPentestBudget(BaseModel):
    limits: dict[str, int]
    elapsed_seconds: int
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    token_usage_complete: bool
    observed_target_interactions: int
    active_target_interactions: int


class ReportStopStatus(BaseModel):
    latest_event_type: str | None = None
    confirmed: bool
    workflow_synced: bool | None = None
    failed_resource_types: list[str] = Field(default_factory=list)


class ReportExecutionSummary(BaseModel):
    id: str
    session_id: str | None = None
    tool_call_id: str | None = None
    tool_id: str | None = None
    tool_version: str | None = None
    node_id: str
    status: str
    exit_code: int | None = None
    physical_stop_confirmed: bool
    created_at: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None


class ReportLedgerEvidence(BaseModel):
    id: str
    kind: str
    digest: str
    ledger_digest: str
    artifact_id: str | None = None
    task_id: str | None = None
    trust_class: str
    redaction_status: str
    replay_strategy: str
    replayable: bool
    locator: dict[str, object]


class ReportReasoningNode(BaseModel):
    id: str
    kind: str
    status: str
    claim: str
    evidence_ids: list[str] = Field(default_factory=list)


class ReportReasoningEdge(BaseModel):
    source_node_id: str
    target_node_id: str
    relation_type: str
    evidence_ids: list[str] = Field(default_factory=list)


class ReportAttemptSummary(BaseModel):
    id: str
    action_signature: str
    tool_id: str
    result_status: str
    result_summary: str
    retryable: bool


class ReportTaskSummary(BaseModel):
    id: str
    sequence: int
    title: str
    status: str
    completion_summary: str | None = None
    blocked_reason: str | None = None
    stop_condition: str | None = None


class ReportPentestSource(BaseModel):
    admission: ReportPentestAdmission
    capabilities: list[ReportCapabilitySelection] = Field(default_factory=list)
    capability_allowlists: dict[str, list[str]] = Field(default_factory=dict)
    pack_locks: list[ReportPackLock] = Field(default_factory=list)
    budget: ReportPentestBudget
    stop: ReportStopStatus
    executions: list[ReportExecutionSummary] = Field(default_factory=list)
    evidence: list[ReportLedgerEvidence] = Field(default_factory=list)
    reasoning_nodes: list[ReportReasoningNode] = Field(default_factory=list)
    reasoning_edges: list[ReportReasoningEdge] = Field(default_factory=list)
    attempts: list[ReportAttemptSummary] = Field(default_factory=list)
    tasks: list[ReportTaskSummary] = Field(default_factory=list)


class ReportSource(BaseModel):
    run_id: str
    engagement: ReportEngagement | None = None
    objective: str
    scope: dict[str, object]
    success_criteria: list[dict[str, object]] = Field(default_factory=list)
    run_status: str
    run_summary: str
    closure_outcome: ClosureOutcome = ClosureOutcome.PARTIAL
    closure_reason_codes: list[str] = Field(
        default_factory=lambda: ["closure_verification_missing"]
    )
    findings: list[ReportFinding] = Field(default_factory=list)
    artifacts: list[ReportArtifactSummary] = Field(default_factory=list)
    key_events: list[ReportEventSummary] = Field(default_factory=list)
    pentest: ReportPentestSource | None = None
    generated_at: AwareDatetime = Field(default_factory=utc_now)


class StructuredReport(BaseModel):
    schema_version: str = "riftx.report.v2"
    title: str
    executive_summary: str
    source: ReportSource


class ReportComposer(Protocol):
    """Report Agent boundary; implementations only receive the restricted source."""

    async def compose(self, source: ReportSource) -> StructuredReport: ...


class DeterministicReportComposer:
    """Safe default Report Agent that never needs raw terminal transcripts."""

    async def compose(self, source: ReportSource) -> StructuredReport:
        finding_count = len(source.findings)
        critical_count = sum(item.severity in {"critical", "high"} for item in source.findings)
        summary = source.run_summary.strip() or (
            f"Run finished with status {source.run_status}. "
            f"{finding_count} structured finding(s) were recorded."
        )
        if critical_count:
            summary = f"{summary} {critical_count} high or critical finding(s) require attention."
        if source.closure_outcome is ClosureOutcome.PARTIAL:
            summary = f"{summary} Closure verification returned a partial outcome."
        return StructuredReport(
            title=f"RiftX Run Report — {source.objective}",
            executive_summary=_truncate(summary, _MAX_SUMMARY_LENGTH),
            source=source,
        )


@dataclass(frozen=True, slots=True)
class GenerateReports:
    formats: list[ReportFormat] = field(
        default_factory=lambda: [
            ReportFormat.MARKDOWN,
            ReportFormat.HTML,
            ReportFormat.JSON,
        ]
    )
    reuse_existing: bool = False


class ReportApplicationService:
    def __init__(
        self,
        *,
        run_repository: RunRepository,
        finding_repository: FindingRepository,
        artifact_repository: ArtifactRepository,
        report_repository: ReportRepository,
        event_repository: RunEventRepository,
        artifact_service: ArtifactApplicationService,
        engagement_repository: EngagementRepository | None = None,
        execution_repository: ExecutionRepository | None = None,
        evidence_repository: EvidenceLedgerRepository | None = None,
        reasoning_graph_repository: ReasoningGraphRepository | None = None,
        task_graph_repository: TaskGraphRepository | None = None,
        working_memory_repository: WorkingMemoryRepository | None = None,
        pentest_status_reader: PentestStatusReader | None = None,
        composer: ReportComposer | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._finding_repository = finding_repository
        self._artifact_repository = artifact_repository
        self._report_repository = report_repository
        self._event_repository = event_repository
        self._artifact_service = artifact_service
        self._engagement_repository = engagement_repository
        self._execution_repository = execution_repository
        self._evidence_repository = evidence_repository
        self._reasoning_graph_repository = reasoning_graph_repository
        self._task_graph_repository = task_graph_repository
        self._working_memory_repository = working_memory_repository
        self._pentest_status_reader = pentest_status_reader
        self._composer = composer or DeterministicReportComposer()

    async def generate(self, run_id: str, command: GenerateReports | None = None) -> list[Report]:
        request = command or GenerateReports()
        formats = _normalize_formats(request.formats)
        run = await self._require_run(run_id)
        require_interactive_run_operation(run)
        if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise ApplicationConflictError(
                "run_not_reportable",
                "Reports can only be generated after a Run reaches a final status",
                details={"run_id": run.id, "status": run.status.value},
            )
        if request.reuse_existing:
            existing = await self._latest_by_format(run_id)
            if all(report_format in existing for report_format in formats):
                return [existing[report_format] for report_format in formats]

        await self._event_repository.append(
            run_id,
            "report.generation_requested",
            {"formats": [item.value for item in formats]},
        )
        source = await self.build_source(run)
        structured = await self._composer.compose(source)
        generated: list[Report] = []
        existing = await self._latest_by_format(run_id) if request.reuse_existing else {}
        for report_format in formats:
            if report_format in existing:
                generated.append(existing[report_format])
                continue
            content, name, mime_type = render_report(structured, report_format)
            artifact = await self._artifact_service.register_content(
                run_id,
                RegisterArtifactContent(
                    content=content.encode("utf-8"),
                    name=name,
                    mime_type=mime_type,
                    description=f"Generated {report_format.value} report for Run {run_id}",
                    content_trust=ArtifactContentTrust.GENERATED,
                ),
            )
            report = Report(
                run_id=run_id,
                format=report_format,
                artifact_id=artifact.id,
                finding_ids=[item.id for item in source.findings],
            )
            await self._report_repository.create(report)
            await self._event_repository.append(
                run_id,
                "report.generated",
                {
                    "report_id": report.id,
                    "format": report.format.value,
                    "artifact_id": report.artifact_id,
                    "finding_ids": report.finding_ids,
                },
            )
            generated.append(report)
        return generated

    async def get(self, report_id: str) -> Report:
        report = await self._report_repository.get(report_id)
        if report is None:
            raise EntityNotFoundError("Report", report_id)
        return report

    async def resolve_run_id(self, report_id: str) -> str:
        run_id = await self._report_repository.get_run_id(report_id)
        if run_id is None:
            raise resource_not_accessible()
        return run_id

    async def list(
        self,
        run_id: str,
        *,
        format: ReportFormat | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Report]:
        await self._require_run(run_id)
        return list(
            await self._report_repository.list(
                run_id,
                format=format,
                limit=limit,
                offset=offset,
            )
        )

    async def build_source(self, run: Run | str) -> ReportSource:
        target = await self._require_run(run) if isinstance(run, str) else run
        findings = list(await self._finding_repository.list(target.id, limit=1000))
        reports = list(await self._report_repository.list(target.id, limit=1000))
        report_artifact_ids = {item.artifact_id for item in reports}
        all_artifacts = list(await self._artifact_repository.list(target.id, limit=1000))
        artifacts = [item for item in all_artifacts if item.id not in report_artifact_ids]
        raw_events = await self._all_events(target.id)
        sensitive_artifact_ids = await self._artifact_repository.target_http_sensitive_ids(
            target_http_artifact_candidates(raw_events)
        )
        restricted_artifact_ids = await self._artifact_repository.restricted_artifact_ids(
            target_http_artifact_candidates(raw_events)
        )
        protected_artifact_ids = sensitive_artifact_ids | restricted_artifact_ids
        artifacts = [item for item in artifacts if item.id not in protected_artifact_ids]
        events = [
            redact_sensitive_event(
                event,
                sensitive_artifact_ids=sensitive_artifact_ids,
                restricted_artifact_ids=restricted_artifact_ids,
            )
            for event in raw_events
        ]
        artifact_ids = {item.id for item in all_artifacts if item.id not in protected_artifact_ids}
        report_findings = [
            _finding_for_report(item, artifact_ids=artifact_ids) for item in findings
        ]
        summary = _run_summary(events)
        closure_outcome, closure_reason_codes = _closure_summary(events)
        engagement = (
            await self._engagement_repository.get(target.engagement_id)
            if self._engagement_repository is not None
            else None
        )
        pentest = (
            await self._build_pentest_source(target) if target.kind is RunKind.PENTEST else None
        )
        return ReportSource(
            run_id=target.id,
            engagement=_engagement_for_report(target, engagement),
            objective=_truncate(target.objective.description, _MAX_TEXT_LENGTH),
            scope=target.scope.model_dump(mode="json"),
            success_criteria=[item.model_dump(mode="json") for item in target.success_criteria],
            run_status=target.status.value,
            run_summary=_truncate(summary, _MAX_SUMMARY_LENGTH),
            closure_outcome=closure_outcome,
            closure_reason_codes=closure_reason_codes,
            findings=report_findings,
            artifacts=[
                ReportArtifactSummary(
                    id=item.id,
                    execution_id=item.execution_id,
                    name=item.name,
                    mime_type=item.mime_type,
                    sha256=item.sha256,
                    size=item.size,
                    description=_truncate(item.description, _MAX_SUMMARY_LENGTH),
                    content_url=_artifact_content_url(item.id),
                )
                for item in artifacts
            ],
            key_events=[
                ReportEventSummary(
                    sequence=event.sequence,
                    event_type=event.event_type,
                    payload=_safe_event_payload(event),
                    created_at=event.created_at,
                )
                for event in events
                if event.event_type in _REPORT_EVENT_FIELDS
            ],
            pentest=pentest,
        )

    async def _build_pentest_source(self, run: Run) -> ReportPentestSource:
        if run.pentest_admission is None:
            raise ApplicationConflictError(
                "pentest_report_admission_missing",
                "Pentest report generation requires the durable admission contract",
            )
        if any(
            dependency is None
            for dependency in (
                self._execution_repository,
                self._evidence_repository,
                self._reasoning_graph_repository,
                self._task_graph_repository,
                self._pentest_status_reader,
            )
        ):
            raise ApplicationConflictError(
                "pentest_report_projection_unavailable",
                "Pentest report generation requires the durable professional fact readers",
            )
        assert self._execution_repository is not None
        assert self._evidence_repository is not None
        assert self._reasoning_graph_repository is not None
        assert self._task_graph_repository is not None
        assert self._pentest_status_reader is not None

        snapshot = await self._pentest_status_reader.read(run.id, f"{run.id}:primary")
        executions = await self._all_executions(run.id)
        evidence = await self._all_evidence(run.id)
        reasoning_graph = await self._reasoning_graph_repository.get(run.id)
        task_graph = await self._task_graph_repository.get(run.id)
        working_memory = (
            await self._working_memory_repository.get_for_run(run.id)
            if self._working_memory_repository is not None
            else None
        )
        start = run.started_at or run.created_at
        end = run.finished_at or utc_now()
        usage = snapshot.usage
        stop = snapshot.stop
        return ReportPentestSource(
            admission=ReportPentestAdmission(
                approval_mode=run.approval_mode.value,
                model_profile=run.model_profile,
                entry_points=[_entry_point_for_report(item) for item in run.entry_points],
                prohibited_actions=[
                    item.value for item in run.pentest_admission.prohibited_actions
                ],
                stop_conditions=[item.value for item in run.pentest_admission.stop_conditions],
            ),
            capabilities=[
                ReportCapabilitySelection(
                    kind=item.kind.value,
                    capability_id=item.capability_id,
                    version=item.version,
                    digest=item.capability_digest,
                    source=item.source.value,
                    active=item.active,
                )
                for item in snapshot.selections
            ],
            capability_allowlists={
                item.kind.value: list(item.capability_ids) for item in snapshot.allowlists
            },
            pack_locks=[
                ReportPackLock(
                    lock_id=item.lock_id,
                    capability_id=item.capability_id,
                    capability_version_id=item.capability_version_id,
                    capability_version=item.capability_version,
                    capability_digest=item.capability_digest,
                    active=item.active,
                )
                for item in snapshot.pack_locks
            ],
            budget=ReportPentestBudget(
                limits=run.pentest_admission.budget.model_dump(mode="json"),
                elapsed_seconds=max(0, int((end - start).total_seconds())),
                model_calls=usage.model_calls,
                tool_calls=usage.tool_calls,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                token_usage_complete=usage.token_usage_complete,
                observed_target_interactions=usage.observed_target_interactions,
                active_target_interactions=usage.active_target_interactions,
            ),
            stop=ReportStopStatus(
                latest_event_type=stop.latest_event_type,
                confirmed=stop.confirmed,
                workflow_synced=stop.workflow_synced,
                failed_resource_types=list(stop.failed_resource_types),
            ),
            executions=[_execution_for_report(item) for item in executions],
            evidence=[_ledger_evidence_for_report(item) for item in evidence],
            reasoning_nodes=(
                [_reasoning_node_for_report(item) for item in reasoning_graph.nodes]
                if reasoning_graph is not None
                else []
            ),
            reasoning_edges=(
                [
                    ReportReasoningEdge(
                        source_node_id=item.source_node_id,
                        target_node_id=item.target_node_id,
                        relation_type=item.relation_type.value,
                        evidence_ids=list(item.evidence_ids),
                    )
                    for item in reasoning_graph.edges
                ]
                if reasoning_graph is not None
                else []
            ),
            attempts=(
                [_attempt_for_report(item) for item in working_memory.attempts]
                if working_memory is not None
                else []
            ),
            tasks=(
                [_task_for_report(item) for item in task_graph.tasks]
                if task_graph is not None
                else []
            ),
        )

    async def _require_run(self, run_id: str) -> Run:
        run = await self._run_repository.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        return run

    async def _all_events(self, run_id: str) -> Sequence[RunEvent]:
        events: list[RunEvent] = []
        after_sequence = 0
        while True:
            page = list(
                await self._event_repository.list_after(
                    run_id,
                    after_sequence=after_sequence,
                    limit=1000,
                )
            )
            if not page:
                return events
            events.extend(page)
            after_sequence = page[-1].sequence
            if len(page) < 1000:
                return events

    async def _all_executions(self, run_id: str) -> Sequence[Execution]:
        assert self._execution_repository is not None
        executions: list[Execution] = []
        offset = 0
        while True:
            page = list(
                await self._execution_repository.list(
                    run_id,
                    limit=1000,
                    offset=offset,
                )
            )
            executions.extend(page)
            if len(page) < 1000:
                return executions
            offset += len(page)

    async def _all_evidence(self, run_id: str) -> Sequence[Evidence]:
        assert self._evidence_repository is not None
        evidence: list[Evidence] = []
        offset = 0
        while True:
            page = list(
                await self._evidence_repository.list(
                    run_id,
                    limit=1000,
                    offset=offset,
                )
            )
            evidence.extend(page)
            if len(page) < 1000:
                return evidence
            offset += len(page)

    async def _latest_by_format(self, run_id: str) -> dict[ReportFormat, Report]:
        reports = await self._report_repository.list(run_id, limit=1000)
        latest: dict[ReportFormat, Report] = {}
        for report in reports:
            latest[report.format] = report
        return latest


def _engagement_for_report(run: Run, engagement: Engagement | None) -> ReportEngagement:
    if engagement is None:
        return ReportEngagement(id=run.engagement_id)
    authorization_scheme = (
        urlsplit(engagement.authorization_reference).scheme.lower()
        if engagement.authorization_reference
        else None
    )
    return ReportEngagement(
        id=engagement.id,
        name=_truncate(engagement.name, _MAX_SUMMARY_LENGTH),
        description=_truncate(engagement.description, _MAX_TEXT_LENGTH),
        authorization_reference_present=engagement.authorization_reference is not None,
        authorization_reference_scheme=authorization_scheme or None,
    )


def _entry_point_for_report(entry_point: EntryPoint) -> dict[str, object]:
    if entry_point.kind is EntryPointKind.URL:
        metadata = safe_url_metadata(entry_point.value)
        return {
            "kind": entry_point.kind.value,
            "url": metadata if metadata is not None else {"redacted": True},
        }
    return {
        "kind": entry_point.kind.value,
        "value": _truncate(entry_point.value, _MAX_SUMMARY_LENGTH),
    }


def _execution_for_report(execution: Execution) -> ReportExecutionSummary:
    return ReportExecutionSummary(
        id=execution.id,
        session_id=execution.session_id,
        tool_call_id=execution.tool_call_id,
        tool_id=execution.tool_id,
        tool_version=execution.tool_version,
        node_id=execution.node_id,
        status=execution.status.value,
        exit_code=execution.exit_code,
        physical_stop_confirmed=execution.physical_stop_confirmed_at is not None,
        created_at=execution.created_at,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
    )


def _ledger_evidence_for_report(evidence: Evidence) -> ReportLedgerEvidence:
    locator: dict[str, object]
    if isinstance(evidence.locator, ArtifactSpanLocator):
        locator = {
            "locator_type": evidence.locator.locator_type,
            "artifact_id": evidence.locator.artifact_id,
            "start_offset": evidence.locator.start_offset,
            "end_offset": evidence.locator.end_offset,
            "artifact_sha256": evidence.locator.artifact_sha256,
        }
    elif isinstance(evidence.locator, CodeLocationLocator):
        locator = {
            "locator_type": evidence.locator.locator_type,
            "source": evidence.locator.source.value,
            "path": evidence.locator.path,
            "start_line": evidence.locator.start_line,
            "start_column": evidence.locator.start_column,
            "end_line": evidence.locator.end_line,
            "end_column": evidence.locator.end_column,
            "source_digest": evidence.locator.source_digest,
        }
    else:
        locator = {
            "locator_type": evidence.locator.locator_type,
            "scheme": urlsplit(evidence.source_uri).scheme,
            "source_uri_digest": hashlib.sha256(evidence.source_uri.encode("utf-8")).hexdigest(),
        }
    return ReportLedgerEvidence(
        id=evidence.id,
        kind=evidence.kind.value,
        digest=evidence.digest,
        ledger_digest=evidence.ledger_digest,
        artifact_id=evidence.artifact_id,
        task_id=evidence.task_id,
        trust_class=evidence.trust_class.value,
        redaction_status=evidence.redaction_status.value,
        replay_strategy=evidence.replay.strategy.value,
        replayable=evidence.replay.replayable,
        locator=locator,
    )


def _reasoning_node_for_report(node: ReasoningNode) -> ReportReasoningNode:
    return ReportReasoningNode(
        id=node.id,
        kind=node.kind.value,
        status=node.status.value,
        claim=_truncate(node.claim, _MAX_TEXT_LENGTH),
        evidence_ids=list(node.evidence_ids),
    )


def _task_for_report(task: Task) -> ReportTaskSummary:
    return ReportTaskSummary(
        id=task.id,
        sequence=task.sequence,
        title=_truncate(task.title, _MAX_SUMMARY_LENGTH),
        status=task.status.value,
        completion_summary=(
            _truncate(task.completion_summary, _MAX_TEXT_LENGTH)
            if task.completion_summary
            else None
        ),
        blocked_reason=(
            _truncate(task.blocked_reason, _MAX_TEXT_LENGTH) if task.blocked_reason else None
        ),
        stop_condition=(
            _truncate(task.stop_condition, _MAX_TEXT_LENGTH) if task.stop_condition else None
        ),
    )


def _attempt_for_report(attempt: AttemptRecord) -> ReportAttemptSummary:
    return ReportAttemptSummary(
        id=attempt.id,
        action_signature=_truncate(attempt.action_signature, _MAX_SUMMARY_LENGTH),
        tool_id=_truncate(attempt.tool_id, _MAX_SUMMARY_LENGTH),
        result_status=attempt.result_status.value,
        result_summary=_truncate(attempt.result_summary, _MAX_SUMMARY_LENGTH),
        retryable=attempt.retryable,
    )


def render_report(report: StructuredReport, report_format: ReportFormat) -> tuple[str, str, str]:
    if report_format is ReportFormat.MARKDOWN:
        return _render_markdown(report), "report.md", "text/markdown"
    if report_format is ReportFormat.HTML:
        return _render_html(report), "report.html", "text/html"
    if report_format is ReportFormat.JSON:
        return (
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            "report.json",
            "application/json",
        )
    raise ApplicationConflictError(
        "unsupported_report_format",
        f"Report format {report_format!r} is not supported",
    )


def _render_markdown(report: StructuredReport) -> str:
    source = report.source
    lines = [
        f"# {report.title}",
        "",
        "## Executive Summary",
        "",
        report.executive_summary or "No summary was provided.",
        "",
        "## Run Context",
        "",
        f"- **Run ID:** `{source.run_id}`",
        f"- **Status:** `{source.run_status}`",
        f"- **Closure outcome:** `{source.closure_outcome.value}`",
        f"- **Objective:** {source.objective}",
    ]
    if source.closure_reason_codes:
        lines.append(
            "- **Closure reasons:** "
            + ", ".join(f"`{item}`" for item in source.closure_reason_codes)
        )
    lines.extend(
        [
            "",
            "### Scope",
            "",
            "```json",
            json.dumps(source.scope, ensure_ascii=False, indent=2),
            "```",
            "",
            "## Findings",
            "",
        ]
    )
    if not source.findings:
        lines.extend(["No structured findings were recorded.", ""])
    for index, finding in enumerate(source.findings, start=1):
        lines.extend(
            [
                f"### {index}. {finding.title}",
                "",
                f"- **Severity:** `{finding.severity}`",
                f"- **Status:** `{finding.status}`",
                f"- **Affected assets:** {', '.join(finding.affected_assets) or 'None recorded'}",
                "",
                finding.description or "No description supplied.",
                "",
            ]
        )
        if finding.evidence:
            lines.extend(["#### Evidence", ""])
            for evidence_index, evidence in enumerate(finding.evidence, start=1):
                label = evidence.description or f"Evidence {evidence_index}"
                if evidence.content_url:
                    label = f"[{label}]({evidence.content_url})"
                details = []
                if evidence.location:
                    details.append(f"location: {evidence.location}")
                if evidence.execution_id:
                    details.append(f"execution: `{evidence.execution_id}`")
                suffix = f" — {'; '.join(details)}" if details else ""
                lines.append(f"- {label}{suffix}")
            lines.append("")
        if finding.reproduction_steps:
            lines.extend(["#### Reproduction", ""])
            lines.extend(
                f"{step_index}. {step}"
                for step_index, step in enumerate(finding.reproduction_steps, start=1)
            )
            lines.append("")
        if finding.impact:
            lines.extend(["#### Impact", "", finding.impact, ""])
        if finding.recommendation:
            lines.extend(["#### Recommendation", "", finding.recommendation, ""])

    if source.pentest is not None:
        pentest = source.pentest
        lines.extend(["## Pentest Method Context", "", "### Capability Selections", ""])
        if not pentest.capabilities:
            lines.extend(["No Capability selections were recorded.", ""])
        else:
            for capability in pentest.capabilities:
                lines.append(
                    f"- `{capability.kind}` `{capability.capability_id}` version "
                    f"`{capability.version}` from `{capability.source}` — digest "
                    f"`{capability.digest}`; active: `{str(capability.active).lower()}`"
                )
            lines.append("")

        lines.extend(["### Capability Allowlists", ""])
        if not pentest.capability_allowlists:
            lines.extend(["No Capability allowlists were recorded.", ""])
        else:
            for kind, capability_ids in sorted(pentest.capability_allowlists.items()):
                values = ", ".join(f"`{item}`" for item in capability_ids) or "None"
                lines.append(f"- **{kind}:** {values}")
            lines.append("")

        workflow_synced = (
            str(pentest.stop.workflow_synced).lower()
            if pentest.stop.workflow_synced is not None
            else "unknown"
        )
        lines.extend(
            [
                "### Stop Status",
                "",
                f"- **Latest event:** `{pentest.stop.latest_event_type or 'none'}`",
                f"- **Confirmed:** `{str(pentest.stop.confirmed).lower()}`",
                f"- **Workflow synced:** `{workflow_synced}`",
                "- **Failed resource types:** "
                + (
                    ", ".join(f"`{item}`" for item in pentest.stop.failed_resource_types)
                    or "None"
                ),
                "",
                "### Executions",
                "",
            ]
        )
        if not pentest.executions:
            lines.extend(["No executions were recorded.", ""])
        else:
            for execution in pentest.executions:
                tool = execution.tool_id or "unknown"
                exit_code = (
                    f"; exit: `{execution.exit_code}`"
                    if execution.exit_code is not None
                    else ""
                )
                lines.append(
                    f"- `{execution.status}` `{tool}` on `{execution.node_id}`"
                    f"{exit_code}; physical stop: "
                    f"`{str(execution.physical_stop_confirmed).lower()}`"
                )
            lines.append("")

        lines.extend(["### Evidence Ledger", ""])
        if not pentest.evidence:
            lines.extend(["No ledger evidence was recorded.", ""])
        else:
            for ledger_evidence in pentest.evidence:
                lines.append(
                    f"- `{ledger_evidence.kind}` `{ledger_evidence.id}` — digest "
                    f"`{ledger_evidence.digest}`; trust: "
                    f"`{ledger_evidence.trust_class}`; redaction: "
                    f"`{ledger_evidence.redaction_status}`; replayable: "
                    f"`{str(ledger_evidence.replayable).lower()}`"
                )
            lines.append("")

        lines.extend(["## Pentest Evidence Chain", "", "### Reasoning", ""])
        if not pentest.reasoning_nodes:
            lines.extend(["No reasoning nodes were recorded.", ""])
        else:
            for node in pentest.reasoning_nodes:
                evidence_refs = ", ".join(f"`{item}`" for item in node.evidence_ids)
                suffix = f" — evidence: {evidence_refs}" if evidence_refs else ""
                lines.append(
                    f"- `{node.kind}/{node.status}` {node.claim}{suffix}"
                )
            lines.append("")
        lines.extend(["### Attempts", ""])
        if not pentest.attempts:
            lines.extend(["No structured attempts were recorded.", ""])
        else:
            for attempt in pentest.attempts:
                lines.append(
                    f"- `{attempt.result_status}` {attempt.action_signature} "
                    f"via `{attempt.tool_id}` — {attempt.result_summary}"
                )
            lines.append("")

    lines.extend(["## Artifact Index", ""])
    if not source.artifacts:
        lines.extend(["No non-report artifacts were registered.", ""])
    else:
        for artifact in source.artifacts:
            description = f" — {artifact.description}" if artifact.description else ""
            lines.append(
                f"- [{artifact.name}]({artifact.content_url}) "
                f"(`{artifact.mime_type}`, {artifact.size} bytes){description}"
            )
        lines.append("")

    lines.extend(["## Key Activity", ""])
    if not source.key_events:
        lines.append("No key execution events were recorded.")
    else:
        for event in source.key_events:
            payload = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
            lines.append(
                f"- `{event.sequence}` **{event.event_type}** at "
                f"{event.created_at.isoformat()} — `{payload}`"
            )
    lines.extend(["", f"Generated at {source.generated_at.isoformat()}.", ""])
    return "\n".join(lines)


def _render_html(report: StructuredReport) -> str:
    source = report.source
    findings = "".join(_finding_html(item, index) for index, item in enumerate(source.findings, 1))
    if not findings:
        findings = "<p>No structured findings were recorded.</p>"
    artifacts = (
        "".join(
            '<li><a href="{url}">{name}</a> '
            "<code>{mime}</code> · {size} bytes{description}</li>".format(
                url=html.escape(item.content_url, quote=True),
                name=html.escape(item.name),
                mime=html.escape(item.mime_type),
                size=item.size,
                description=(f" — {html.escape(item.description)}" if item.description else ""),
            )
            for item in source.artifacts
        )
        or "<li>No non-report artifacts were registered.</li>"
    )
    event_items: list[str] = []
    for item in source.key_events:
        event_type = html.escape(item.event_type)
        created_at = html.escape(item.created_at.isoformat())
        payload = html.escape(json.dumps(item.payload, ensure_ascii=False, indent=2))
        event_items.append(
            f"<li><code>{item.sequence}</code> <strong>{event_type}</strong> "
            f"<time>{created_at}</time><pre>{payload}</pre></li>"
        )
    events = "".join(event_items) or "<li>No key execution events were recorded.</li>"
    scope = html.escape(json.dumps(source.scope, ensure_ascii=False, indent=2))
    title = html.escape(report.title)
    run_id = html.escape(source.run_id)
    run_status = html.escape(source.run_status)
    closure_outcome = html.escape(source.closure_outcome.value)
    closure_reasons = html.escape(", ".join(source.closure_reason_codes) or "none")
    executive_summary = html.escape(report.executive_summary)
    objective = html.escape(source.objective)
    generated_at = html.escape(source.generated_at.isoformat())
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
body {{ max-width: 960px; margin: 0 auto; padding: 48px 24px; line-height: 1.6; }}
header, section {{ margin-bottom: 2.5rem; }}
h1, h2, h3 {{ line-height: 1.2; }}
.meta, .chips {{ display: flex; flex-wrap: wrap; gap: .6rem; }}
.chip {{ border: 1px solid currentColor; border-radius: 999px; padding: .1rem .6rem; }}
.finding {{ border-left: 4px solid #ef7d22; padding: .25rem 0 .25rem 1rem; margin: 1.5rem 0; }}
pre {{ overflow: auto; padding: 1rem; border-radius: .5rem; background: rgba(127,127,127,.12); }}
a {{ color: #3b82f6; }}
</style>
</head>
<body>
<header>
<h1>{title}</h1>
<div class="meta">
<span class="chip">Run {run_id}</span><span class="chip">{run_status}</span>
<span class="chip">closure: {closure_outcome}</span>
</div>
</header>
<main>
<section><h2>Executive Summary</h2><p>{executive_summary}</p></section>
<section><h2>Run Context</h2>
<p><strong>Objective:</strong> {objective}</p>
<p><strong>Closure reasons:</strong> {closure_reasons}</p><h3>Scope</h3><pre>{scope}</pre>
</section>
<section><h2>Findings</h2>{findings}</section>
<section><h2>Artifact Index</h2><ul>{artifacts}</ul></section>
<section><h2>Key Activity</h2><ol>{events}</ol></section>
</main>
<footer>Generated at <time>{generated_at}</time>.</footer>
</body>
</html>
"""


def _finding_html(finding: ReportFinding, index: int) -> str:
    assets = (
        "".join(
            f'<span class="chip">{html.escape(item)}</span>' for item in finding.affected_assets
        )
        or '<span class="chip">No affected assets recorded</span>'
    )
    evidence = (
        "".join(
            "<li>{label}{details}</li>".format(
                label=(
                    f'<a href="{html.escape(item.content_url, quote=True)}">'
                    f"{html.escape(item.description or 'Open evidence')}</a>"
                    if item.content_url
                    else html.escape(item.description or "Evidence")
                ),
                details=html.escape(
                    " — "
                    + "; ".join(
                        value
                        for value in (
                            f"location: {item.location}" if item.location else "",
                            f"execution: {item.execution_id}" if item.execution_id else "",
                        )
                        if value
                    )
                )
                if item.location or item.execution_id
                else "",
            )
            for item in finding.evidence
        )
        or "<li>No linked evidence.</li>"
    )
    steps = "".join(f"<li>{html.escape(item)}</li>" for item in finding.reproduction_steps)
    reproduction = f"<h4>Reproduction</h4><ol>{steps}</ol>" if steps else ""
    impact = f"<h4>Impact</h4><p>{html.escape(finding.impact)}</p>" if finding.impact else ""
    recommendation = (
        f"<h4>Recommendation</h4><p>{html.escape(finding.recommendation)}</p>"
        if finding.recommendation
        else ""
    )
    title = html.escape(finding.title)
    severity = html.escape(finding.severity)
    status = html.escape(finding.status)
    description = html.escape(finding.description or "No description supplied.")
    return f"""<article class="finding">
<h3>{index}. {title}</h3>
<p><strong>Severity:</strong> {severity} · <strong>Status:</strong> {status}</p>
<div class="chips">{assets}</div>
<p>{description}</p>
<h4>Evidence</h4><ul>{evidence}</ul>
{reproduction}{impact}{recommendation}
</article>"""


def _finding_for_report(finding: Finding, *, artifact_ids: set[str]) -> ReportFinding:
    return ReportFinding(
        id=finding.id,
        title=_truncate(finding.title, 500),
        severity=finding.severity.value,
        status=finding.status.value,
        affected_assets=[_truncate(item, _MAX_SUMMARY_LENGTH) for item in finding.affected_assets],
        description=_truncate(finding.description, _MAX_TEXT_LENGTH),
        evidence=[
            ReportEvidence(
                artifact_id=item.artifact_id,
                execution_id=item.execution_id,
                description=_truncate(item.description, _MAX_SUMMARY_LENGTH),
                location=_truncate(item.location, _MAX_SUMMARY_LENGTH) if item.location else None,
                content_url=(
                    _artifact_content_url(item.artifact_id)
                    if item.artifact_id and item.artifact_id in artifact_ids
                    else None
                ),
            )
            for item in finding.evidence
        ],
        reproduction_steps=[
            _truncate(item, _MAX_TEXT_LENGTH) for item in finding.reproduction_steps
        ],
        impact=_truncate(finding.impact, _MAX_TEXT_LENGTH),
        recommendation=_truncate(finding.recommendation, _MAX_TEXT_LENGTH),
    )


def _safe_event_payload(event: RunEvent) -> dict[str, object]:
    allowed = _REPORT_EVENT_FIELDS[event.event_type]
    payload: dict[str, object] = {}
    for key, value in event.payload.items():
        if key not in allowed:
            continue
        safe = _safe_scalar(value)
        if safe is not None:
            payload[key] = safe
    return payload


def _safe_scalar(value: object) -> object | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate(value, _MAX_SUMMARY_LENGTH)
    if isinstance(value, list):
        safe = [_safe_scalar(item) for item in value[:100]]
        return [item for item in safe if item is not None]
    return None


def _run_summary(events: Sequence[RunEvent]) -> str:
    for event in reversed(events):
        if event.event_type not in {"agent.completion_requested", "agent.cycle_completed"}:
            continue
        value = event.payload.get("run_summary")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _closure_summary(events: Sequence[RunEvent]) -> tuple[ClosureOutcome, list[str]]:
    event = next(
        (item for item in reversed(events) if item.event_type == CLOSURE_EVALUATED_EVENT_TYPE),
        None,
    )
    if event is None:
        return ClosureOutcome.PARTIAL, ["closure_verification_missing"]
    raw_outcome = event.payload.get("outcome")
    if not isinstance(raw_outcome, str):
        return ClosureOutcome.PARTIAL, ["closure_verification_invalid"]
    try:
        outcome = ClosureOutcome(raw_outcome)
    except (TypeError, ValueError):
        return ClosureOutcome.PARTIAL, ["closure_verification_invalid"]
    raw_reasons = event.payload.get("reason_codes")
    if not isinstance(raw_reasons, list) or any(
        not isinstance(item, str) or not item for item in raw_reasons
    ):
        return ClosureOutcome.PARTIAL, ["closure_verification_invalid"]
    reasons = list(dict.fromkeys(raw_reasons))
    if outcome is ClosureOutcome.COMPLETE and reasons:
        return ClosureOutcome.PARTIAL, ["closure_verification_invalid"]
    if outcome is ClosureOutcome.PARTIAL and not reasons:
        return ClosureOutcome.PARTIAL, ["closure_partial_unspecified"]
    return outcome, reasons


def _normalize_formats(formats: Sequence[ReportFormat]) -> list[ReportFormat]:
    normalized: list[ReportFormat] = []
    for value in formats:
        try:
            report_format = value if isinstance(value, ReportFormat) else ReportFormat(value)
        except ValueError as exc:
            raise ApplicationConflictError(
                "unsupported_report_format",
                f"Report format {value!r} is not supported",
            ) from exc
        if report_format not in normalized:
            normalized.append(report_format)
    if not normalized:
        raise ApplicationConflictError(
            "empty_report_formats",
            "At least one report format must be requested",
        )
    return normalized


def _artifact_content_url(artifact_id: str) -> str:
    return f"/api/v1/artifacts/{artifact_id}/content"


def _truncate(value: str, length: int) -> str:
    text = value.strip()
    if len(text) <= length:
        return text
    return text[: length - 1].rstrip() + "…"
