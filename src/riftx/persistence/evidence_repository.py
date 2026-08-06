"""SQLAlchemy persistence for immutable Evidence Ledger records."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from riftx.application.errors import RepositoryConflictError, RepositoryIntegrityError
from riftx.evidence import Evidence, EvidenceKind

from .orm import EvidenceRecord
from .transactions import SessionFactory, consistent_read, serialized_write

_MAX_EVIDENCE_BATCH = 1_000


class SQLAlchemyEvidenceLedgerRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, evidence: Evidence) -> Evidence:
        try:
            async with serialized_write(self._session_factory) as session:
                session.add(_record(evidence))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not persist Evidence {evidence.id!r}"
            ) from exc
        return evidence

    async def get(self, evidence_id: str) -> Evidence | None:
        async with consistent_read(self._session_factory) as session:
            record = await session.get(EvidenceRecord, evidence_id)
            return _evidence(record) if record is not None else None

    async def list_by_ids(
        self,
        run_id: str,
        evidence_ids: Sequence[str],
    ) -> tuple[Evidence, ...]:
        ordered_ids = tuple(dict.fromkeys(evidence_ids))
        if len(ordered_ids) > _MAX_EVIDENCE_BATCH:
            raise ValueError(f"Evidence lookup exceeds {_MAX_EVIDENCE_BATCH} IDs")
        if not ordered_ids:
            return ()
        async with consistent_read(self._session_factory) as session:
            evidence = tuple(
                _evidence(record)
                for record in await session.scalars(
                    select(EvidenceRecord).where(
                        EvidenceRecord.run_id == run_id,
                        EvidenceRecord.id.in_(ordered_ids),
                    )
                )
            )
        by_id = {item.id: item for item in evidence}
        return tuple(by_id[evidence_id] for evidence_id in ordered_ids if evidence_id in by_id)

    async def list(
        self,
        run_id: str,
        *,
        task_id: str | None = None,
        kind: EvidenceKind | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Evidence, ...]:
        if limit < 1 or limit > _MAX_EVIDENCE_BATCH:
            raise ValueError(f"Evidence list limit must be between 1 and {_MAX_EVIDENCE_BATCH}")
        if offset < 0:
            raise ValueError("Evidence list offset must not be negative")
        statement = select(EvidenceRecord).where(EvidenceRecord.run_id == run_id)
        if task_id is not None:
            statement = statement.where(EvidenceRecord.task_id == task_id)
        if kind is not None:
            statement = statement.where(EvidenceRecord.kind == kind.value)
        statement = statement.order_by(EvidenceRecord.created_at, EvidenceRecord.id).offset(
            offset
        ).limit(limit)
        async with consistent_read(self._session_factory) as session:
            return tuple(_evidence(record) for record in await session.scalars(statement))


def _record(evidence: Evidence) -> EvidenceRecord:
    return EvidenceRecord(
        id=evidence.id,
        schema_version=evidence.schema_version,
        run_id=evidence.run_id,
        session_id=evidence.session_id,
        task_id=evidence.task_id,
        kind=evidence.kind.value,
        source_uri=evidence.source_uri,
        digest=evidence.digest,
        ledger_digest=evidence.ledger_digest,
        creator_type=evidence.creator_type.value,
        created_by=evidence.created_by,
        trust_class=evidence.trust_class.value,
        scope_json=evidence.scope.model_dump(mode="json"),
        redaction_status=evidence.redaction_status.value,
        redaction_policy_ref=evidence.redaction_policy_ref,
        replay_json=evidence.replay.model_dump(mode="json"),
        locator_json=evidence.locator.model_dump(mode="json"),
        artifact_id=evidence.artifact_id,
        created_at=evidence.created_at,
    )


def _evidence(record: EvidenceRecord) -> Evidence:
    try:
        return Evidence.model_validate(
            {
                "id": record.id,
                "schema_version": record.schema_version,
                "run_id": record.run_id,
                "session_id": record.session_id,
                "task_id": record.task_id,
                "kind": record.kind,
                "source_uri": record.source_uri,
                "digest": record.digest,
                "ledger_digest": record.ledger_digest,
                "creator_type": record.creator_type,
                "created_by": record.created_by,
                "trust_class": record.trust_class,
                "scope": record.scope_json,
                "redaction_status": record.redaction_status,
                "redaction_policy_ref": record.redaction_policy_ref,
                "replay": record.replay_json,
                "locator": record.locator_json,
                "artifact_id": record.artifact_id,
                "created_at": record.created_at,
            }
        )
    except (TypeError, ValidationError, ValueError):
        raise RepositoryIntegrityError("Evidence", record.id) from None
