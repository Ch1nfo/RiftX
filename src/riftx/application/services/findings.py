"""Editable structured findings with Run-scoped evidence validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
)
from riftx.application.ports import (
    ArtifactRepository,
    ExecutionRepository,
    FindingRepository,
    RunEventRepository,
    RunRepository,
)
from riftx.domain import (
    Finding,
    FindingEvidence,
    FindingSeverity,
    FindingStatus,
)

if TYPE_CHECKING:
    from riftx.memory import MemoryCandidateFactory, MemoryWriter


@dataclass(frozen=True, slots=True)
class CreateFinding:
    title: str
    severity: FindingSeverity
    status: FindingStatus = FindingStatus.DRAFT
    affected_assets: list[str] | None = None
    description: str = ""
    evidence: list[FindingEvidence] | None = None
    reproduction_steps: list[str] | None = None
    impact: str = ""
    recommendation: str = ""
    agent_step_id: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateFinding:
    title: str | None = None
    severity: FindingSeverity | None = None
    status: FindingStatus | None = None
    affected_assets: list[str] | None = None
    description: str | None = None
    evidence: list[FindingEvidence] | None = None
    reproduction_steps: list[str] | None = None
    impact: str | None = None
    recommendation: str | None = None


class FindingApplicationService:
    def __init__(
        self,
        *,
        run_repository: RunRepository,
        finding_repository: FindingRepository,
        artifact_repository: ArtifactRepository | None = None,
        execution_repository: ExecutionRepository | None = None,
        event_repository: RunEventRepository | None = None,
        memory_writer: MemoryWriter | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._finding_repository = finding_repository
        self._artifact_repository = artifact_repository
        self._execution_repository = execution_repository
        self._event_repository = event_repository
        self._memory_writer = memory_writer
        if memory_writer is not None:
            from riftx.memory import MemoryCandidateFactory

            self._memory_candidates: MemoryCandidateFactory | None = MemoryCandidateFactory()
        else:
            self._memory_candidates = None

    async def create_finding(self, run_id: str, command: CreateFinding) -> Finding:
        await self._require_run(run_id)
        finding = Finding(
            run_id=run_id,
            title=_required_text(command.title, "title"),
            severity=command.severity,
            status=command.status,
            affected_assets=_normalize_list(command.affected_assets or []),
            description=command.description.strip(),
            evidence=command.evidence or [],
            reproduction_steps=_normalize_list(command.reproduction_steps or []),
            impact=command.impact.strip(),
            recommendation=command.recommendation.strip(),
        )
        await self._validate_evidence(run_id, finding.evidence)
        finding = await self._finding_repository.create(finding)
        event_payload: dict[str, object] = {
            "finding_id": finding.id,
            "title": finding.title,
            "severity": finding.severity.value,
            "status": finding.status.value,
        }
        if command.agent_step_id is not None:
            event_payload["agent_step_id"] = command.agent_step_id
        await self._append_event(
            run_id,
            "finding.created",
            event_payload,
        )
        await self._promote_confirmed(finding)
        return finding

    async def get_finding(self, finding_id: str) -> Finding:
        finding = await self._finding_repository.get(finding_id)
        if finding is None:
            raise EntityNotFoundError("Finding", finding_id)
        return finding

    async def update_finding(
        self,
        finding_id: str,
        command: UpdateFinding,
    ) -> Finding:
        current = await self.get_finding(finding_id)
        updates: dict[str, object] = {}
        if command.title is not None:
            updates["title"] = _required_text(command.title, "title")
        if command.severity is not None:
            updates["severity"] = command.severity
        if command.status is not None:
            updates["status"] = command.status
        if command.affected_assets is not None:
            updates["affected_assets"] = _normalize_list(command.affected_assets)
        if command.description is not None:
            updates["description"] = command.description.strip()
        if command.evidence is not None:
            updates["evidence"] = command.evidence
        if command.reproduction_steps is not None:
            updates["reproduction_steps"] = _normalize_list(command.reproduction_steps)
        if command.impact is not None:
            updates["impact"] = command.impact.strip()
        if command.recommendation is not None:
            updates["recommendation"] = command.recommendation.strip()

        if not updates:
            raise ApplicationConflictError(
                "empty_finding_update",
                "At least one Finding field must be supplied for update",
            )

        finding = current.model_copy(update=updates)
        await self._validate_evidence(finding.run_id, finding.evidence)
        try:
            finding, changed = await self._finding_repository.save(
                finding,
                expected_updated_at=current.updated_at,
            )
        except RepositoryConflictError as exc:
            raise ApplicationConflictError(
                "finding_update_conflict",
                "Finding was updated by another writer",
                details={"finding_id": finding_id},
            ) from exc
        if not changed:
            return finding
        updated_fields = sorted(
            field_name
            for field_name in updates
            if getattr(current, field_name) != getattr(finding, field_name)
        )
        await self._append_event(
            finding.run_id,
            "finding.updated",
            {
                "finding_id": finding.id,
                "title": finding.title,
                "severity": finding.severity.value,
                "status": finding.status.value,
                "updated_fields": updated_fields,
            },
        )
        if (
            finding.status is FindingStatus.CONFIRMED
            and current.status is not FindingStatus.CONFIRMED
        ):
            await self._promote_confirmed(finding)
        return finding

    async def _promote_confirmed(self, finding: Finding) -> None:
        if (
            self._memory_writer is None
            or self._memory_candidates is None
            or finding.status is not FindingStatus.CONFIRMED
        ):
            return
        run = await self._run_repository.get(finding.run_id)
        if run is None:
            return
        try:
            result = await self._memory_writer.write(
                self._memory_candidates.from_finding(
                    finding,
                    engagement_id=run.engagement_id,
                ),
                run_id=finding.run_id,
            )
            await self._append_event(
                finding.run_id,
                "memory.promotion_evaluated",
                {
                    "finding_id": finding.id,
                    "candidate_id": result.candidate_id,
                    "decision": result.assessment.decision.value,
                    "memory_id": result.memory.id if result.memory is not None else None,
                },
            )
        except Exception as exc:
            await self._append_event(
                finding.run_id,
                "memory.promotion_failed",
                {"finding_id": finding.id, "reason": str(exc)},
            )

    async def list_findings(
        self,
        run_id: str,
        *,
        severity: FindingSeverity | None = None,
        status: FindingStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Finding]:
        await self._require_run(run_id)
        return list(
            await self._finding_repository.list(
                run_id,
                severity=severity,
                status=status,
                limit=limit,
                offset=offset,
            )
        )

    async def _require_run(self, run_id: str) -> None:
        if await self._run_repository.get(run_id) is None:
            raise EntityNotFoundError("Run", run_id)

    async def _validate_evidence(
        self,
        run_id: str,
        evidence_items: list[FindingEvidence],
    ) -> None:
        for index, evidence in enumerate(evidence_items):
            if not evidence.artifact_id and not evidence.execution_id:
                if not evidence.description.strip():
                    raise ApplicationConflictError(
                        "empty_finding_evidence",
                        (
                            "Finding evidence must reference an artifact or execution, "
                            "or describe the evidence"
                        ),
                        details={"evidence_index": index},
                    )
                continue
            if evidence.artifact_id:
                if self._artifact_repository is None:
                    raise ApplicationConflictError(
                        "artifact_evidence_unavailable",
                        "Artifact evidence validation is unavailable",
                    )
                artifact = await self._artifact_repository.get(evidence.artifact_id)
                if artifact is None:
                    raise EntityNotFoundError("Artifact", evidence.artifact_id)
                if artifact.run_id != run_id:
                    raise ApplicationConflictError(
                        "finding_artifact_run_mismatch",
                        "Finding evidence cannot reference an Artifact from another Run",
                        details={
                            "run_id": run_id,
                            "artifact_id": artifact.id,
                            "artifact_run_id": artifact.run_id,
                        },
                    )
            if evidence.execution_id:
                if self._execution_repository is None:
                    raise ApplicationConflictError(
                        "execution_evidence_unavailable",
                        "Execution evidence validation is unavailable",
                    )
                execution = await self._execution_repository.get(evidence.execution_id)
                if execution is None:
                    raise EntityNotFoundError("Execution", evidence.execution_id)
                if execution.run_id != run_id:
                    raise ApplicationConflictError(
                        "finding_execution_run_mismatch",
                        "Finding evidence cannot reference an Execution from another Run",
                        details={
                            "run_id": run_id,
                            "execution_id": execution.id,
                            "execution_run_id": execution.run_id,
                        },
                    )

    async def _append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        if self._event_repository is not None:
            await self._event_repository.append(run_id, event_type, payload)


def _required_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ApplicationConflictError(
            f"empty_finding_{field}",
            f"Finding {field} must not be empty",
        )
    if field == "title" and len(normalized) > 500:
        raise ApplicationConflictError(
            "finding_title_too_long",
            "Finding title must contain at most 500 characters",
        )
    return normalized


def _normalize_list(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if item and item not in seen:
            normalized.append(item)
            seen.add(item)
    return normalized
