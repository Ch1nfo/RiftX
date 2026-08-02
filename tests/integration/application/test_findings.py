import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from riftx.application.errors import ApplicationConflictError
from riftx.application.services import FindingApplicationService, UpdateFinding
from riftx.domain import (
    Engagement,
    Finding,
    FindingSeverity,
    FindingStatus,
    Objective,
    Run,
)
from riftx.memory import (
    MemoryCandidate,
    MemoryWriteResult,
    PromotionAssessment,
    PromotionDecision,
)
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyRunRepository,
)


class RecordingClock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.value


class RecordingEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, object]]] = []

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        event_id: str | None = None,
    ) -> None:
        del event_id
        self.items.append((run_id, event_type, payload or {}))


class RecordingMemoryWriter:
    def __init__(self) -> None:
        self.calls = 0

    async def write(
        self,
        candidate: MemoryCandidate,
        *,
        run_id: str | None = None,
    ) -> MemoryWriteResult:
        del run_id
        self.calls += 1
        return MemoryWriteResult(
            candidate.id,
            PromotionAssessment(PromotionDecision.PROMOTE, "test"),
        )


class ReadBarrierFindingRepository:
    def __init__(self, delegate: SQLAlchemyFindingRepository) -> None:
        self._delegate = delegate
        self._read_count = 0
        self._both_read = asyncio.Event()

    async def create(self, finding: Finding) -> Finding:
        return await self._delegate.create(finding)

    async def get(self, finding_id: str) -> Finding | None:
        finding = await self._delegate.get(finding_id)
        self._read_count += 1
        if self._read_count == 2:
            self._both_read.set()
        await self._both_read.wait()
        return finding

    async def save(
        self,
        finding: Finding,
        *,
        expected_updated_at: datetime,
    ) -> tuple[Finding, bool]:
        return await self._delegate.save(
            finding,
            expected_updated_at=expected_updated_at,
        )

    async def list(
        self,
        run_id: str,
        *,
        severity: FindingSeverity | None = None,
        status: FindingStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Finding]:
        return await self._delegate.list(
            run_id,
            severity=severity,
            status=status,
            limit=limit,
            offset=offset,
        )


async def _repositories(
    path: Path,
) -> tuple[
    Database,
    SQLAlchemyRunRepository,
    SQLAlchemyFindingRepository,
    RecordingClock,
    Finding,
]:
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Test")
    )
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Test"),
            workspace_path=str(path.parent),
        )
    )
    clock = RecordingClock(datetime(2026, 1, 1, tzinfo=UTC))
    findings = SQLAlchemyFindingRepository(database.session_factory, clock=clock)
    finding = await findings.create(
        Finding(
            id="finding-1",
            run_id="run-1",
            title="Finding",
            severity=FindingSeverity.INFO,
        )
    )
    return database, runs, findings, clock, finding


async def test_same_value_update_has_no_event_or_clock_tick(tmp_path: Path) -> None:
    database, runs, findings, clock, finding = await _repositories(tmp_path / "riftx.db")
    events = RecordingEvents()
    service = FindingApplicationService(
        run_repository=runs,
        finding_repository=findings,
        event_repository=events,
    )

    returned = await service.update_finding(
        finding.id,
        UpdateFinding(title=finding.title),
    )

    assert returned == finding
    assert events.items == []
    assert clock.calls == 1
    await database.dispose()


async def test_concurrent_confirmation_emits_and_promotes_exactly_once(
    tmp_path: Path,
) -> None:
    database, runs, findings, clock, finding = await _repositories(tmp_path / "riftx.db")
    events = RecordingEvents()
    memory = RecordingMemoryWriter()
    service = FindingApplicationService(
        run_repository=runs,
        finding_repository=ReadBarrierFindingRepository(findings),
        event_repository=events,
        memory_writer=memory,  # type: ignore[arg-type]
    )

    results = await asyncio.gather(
        service.update_finding(finding.id, UpdateFinding(status=FindingStatus.CONFIRMED)),
        service.update_finding(finding.id, UpdateFinding(status=FindingStatus.CONFIRMED)),
    )

    assert results[0] == results[1]
    assert results[0].status is FindingStatus.CONFIRMED
    assert clock.calls == 2
    assert memory.calls == 1
    assert [item[1] for item in events.items] == [
        "finding.updated",
        "memory.promotion_evaluated",
    ]
    assert events.items[0][2]["updated_fields"] == ["status"]
    await database.dispose()


async def test_stale_divergent_service_writer_uses_stable_conflict_code(
    tmp_path: Path,
) -> None:
    database, runs, findings, clock, finding = await _repositories(tmp_path / "riftx.db")
    events = RecordingEvents()
    service = FindingApplicationService(
        run_repository=runs,
        finding_repository=ReadBarrierFindingRepository(findings),
        event_repository=events,
    )

    results = await asyncio.gather(
        service.update_finding(finding.id, UpdateFinding(title="First writer")),
        service.update_finding(finding.id, UpdateFinding(title="Second writer")),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, Finding)]
    conflicts = [result for result in results if isinstance(result, ApplicationConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "finding_update_conflict"
    assert clock.calls == 2
    assert [item[1] for item in events.items] == ["finding.updated"]
    assert events.items[0][2]["updated_fields"] == ["title"]
    await database.dispose()
