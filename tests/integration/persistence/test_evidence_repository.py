from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import update

from riftx.application.errors import RepositoryIntegrityError
from riftx.domain import Engagement, Objective, Run
from riftx.evidence import (
    Evidence,
    EvidenceCreatorType,
    EvidenceKind,
    EvidenceRedactionStatus,
    EvidenceReplayMetadata,
    EvidenceReplayStrategy,
    EvidenceScope,
    EvidenceTrustClass,
    SourceLocator,
)
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyEvidenceLedgerRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTaskPlanner,
)
from riftx.persistence.orm import EvidenceRecord
from riftx.tasks import AddTaskCommand


def source_evidence(evidence_id: str, *, task_id: str | None = "task-1") -> Evidence:
    locator = SourceLocator(uri=f"execution://{evidence_id}/stdout")
    return Evidence(
        id=evidence_id,
        kind=EvidenceKind.EXECUTION_OUTPUT,
        source_uri=locator.source_uri,
        digest="a" * 64,
        run_id="run-1",
        task_id=task_id,
        creator_type=EvidenceCreatorType.TOOL,
        created_by="run_shell",
        trust_class=EvidenceTrustClass.UNTRUSTED_TOOL_OUTPUT,
        scope=EvidenceScope(engagement_id="engagement-1", run_id="run-1"),
        redaction_status=EvidenceRedactionStatus.METADATA_ONLY,
        replay=EvidenceReplayMetadata(
            strategy=EvidenceReplayStrategy.SOURCE_LOOKUP,
            replayable=True,
            expected_digest="a" * 64,
            source_digest="a" * 64,
            parameters_digest="b" * 64,
        ),
        locator=locator,
    )


async def build_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'evidence.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Evidence")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Persist Evidence"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    await SQLAlchemyTaskPlanner(database.session_factory).add_task(
        AddTaskCommand(
            run_id="run-1",
            expected_graph_version=0,
            task_id="task-1",
            title="Collect Evidence",
        )
    )
    return database


async def test_evidence_round_trip_order_filter_and_restart(tmp_path: Path) -> None:
    database = await build_database(tmp_path)
    try:
        repository = SQLAlchemyEvidenceLedgerRepository(database.session_factory)
        first = source_evidence("evidence-1")
        second = source_evidence("evidence-2", task_id=None)
        await repository.create(first)
        await repository.create(second)

        restarted = SQLAlchemyEvidenceLedgerRepository(database.session_factory)
        assert await restarted.get(first.id) == first
        assert await restarted.list_by_ids(
            "run-1", [second.id, first.id, second.id, "missing"]
        ) == (second, first)
        assert await restarted.list("run-1", task_id="task-1") == (first,)
        assert await restarted.list(
            "run-1", kind=EvidenceKind.EXECUTION_OUTPUT, limit=1, offset=1
        ) == (second,)
    finally:
        await database.dispose()


async def test_corrupt_ledger_digest_fails_closed_on_reconstruction(tmp_path: Path) -> None:
    database = await build_database(tmp_path)
    try:
        repository = SQLAlchemyEvidenceLedgerRepository(database.session_factory)
        evidence = source_evidence("evidence-corrupt")
        await repository.create(evidence)
        async with database.session_factory() as session, session.begin():
            await session.execute(
                update(EvidenceRecord)
                .where(EvidenceRecord.id == evidence.id)
                .values(ledger_digest="f" * 64)
            )

        with pytest.raises(RepositoryIntegrityError):
            await repository.get(evidence.id)
    finally:
        await database.dispose()
