"""Restricted report composition and immutable report generation."""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

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
    FindingRepository,
    ReportRepository,
    RunEventRepository,
    RunRepository,
)
from riftx.domain import (
    ArtifactContentTrust,
    Finding,
    Report,
    ReportFormat,
    Run,
    RunEvent,
    RunStatus,
)
from riftx.domain.base import utc_now

from .artifacts import ArtifactApplicationService, RegisterArtifactContent
from .runs import require_general_run_operation

_MAX_SUMMARY_LENGTH = 2_000
_MAX_TEXT_LENGTH = 10_000
_REPORT_EVENT_FIELDS: Mapping[str, frozenset[str]] = {
    "run.created": frozenset({"status"}),
    "run.status_changed": frozenset({"from", "to", "status"}),
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


class ReportSource(BaseModel):
    run_id: str
    objective: str
    scope: dict[str, object]
    success_criteria: list[dict[str, object]] = Field(default_factory=list)
    run_status: str
    run_summary: str
    findings: list[ReportFinding] = Field(default_factory=list)
    artifacts: list[ReportArtifactSummary] = Field(default_factory=list)
    key_events: list[ReportEventSummary] = Field(default_factory=list)
    generated_at: AwareDatetime = Field(default_factory=utc_now)


class StructuredReport(BaseModel):
    schema_version: str = "riftx.report.v1"
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
        composer: ReportComposer | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._finding_repository = finding_repository
        self._artifact_repository = artifact_repository
        self._report_repository = report_repository
        self._event_repository = event_repository
        self._artifact_service = artifact_service
        self._composer = composer or DeterministicReportComposer()

    async def generate(self, run_id: str, command: GenerateReports | None = None) -> list[Report]:
        request = command or GenerateReports()
        formats = _normalize_formats(request.formats)
        run = await self._require_run(run_id)
        require_general_run_operation(run)
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
        events = [
            redact_sensitive_event(
                event,
                sensitive_artifact_ids=sensitive_artifact_ids,
                restricted_artifact_ids=restricted_artifact_ids,
            )
            for event in raw_events
        ]
        artifact_ids = {item.id for item in all_artifacts}
        report_findings = [
            _finding_for_report(item, artifact_ids=artifact_ids) for item in findings
        ]
        summary = _run_summary(events)
        return ReportSource(
            run_id=target.id,
            objective=_truncate(target.objective.description, _MAX_TEXT_LENGTH),
            scope=target.scope.model_dump(mode="json"),
            success_criteria=[item.model_dump(mode="json") for item in target.success_criteria],
            run_status=target.status.value,
            run_summary=_truncate(summary, _MAX_SUMMARY_LENGTH),
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
        )

    async def _require_run(self, run_id: str) -> Run:
        run = await self._run_repository.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        return run

    async def _all_events(self, run_id: str) -> list[RunEvent]:
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

    async def _latest_by_format(self, run_id: str) -> dict[ReportFormat, Report]:
        reports = await self._report_repository.list(run_id, limit=1000)
        latest: dict[ReportFormat, Report] = {}
        for report in reports:
            latest[report.format] = report
        return latest


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
        f"- **Objective:** {source.objective}",
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
</div>
</header>
<main>
<section><h2>Executive Summary</h2><p>{executive_summary}</p></section>
<section><h2>Run Context</h2>
<p><strong>Objective:</strong> {objective}</p><h3>Scope</h3><pre>{scope}</pre>
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
