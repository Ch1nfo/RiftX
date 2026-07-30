"""Durable connector submission idempotency records."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from riftx.application.errors import RepositoryConflictError
from riftx.connectors import ConnectorSource, ConnectorSubmission

from .orm import ConnectorSubmissionRecord
from .repositories import SessionFactory


class SQLAlchemyConnectorSubmissionRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get(
        self, source: ConnectorSource, capture_id: str
    ) -> ConnectorSubmission | None:
        statement = select(ConnectorSubmissionRecord).where(
            ConnectorSubmissionRecord.source == source.value,
            ConnectorSubmissionRecord.capture_id == capture_id,
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
        return _from_record(row) if row is not None else None

    async def create(self, item: ConnectorSubmission) -> ConnectorSubmission:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(
                    ConnectorSubmissionRecord(
                        id=item.id,
                        run_id=item.run_id,
                        source=item.source.value,
                        capture_id=item.capture_id,
                        fingerprint=item.fingerprint,
                        request_artifact_id=item.request_artifact_id,
                        response_artifact_id=item.response_artifact_id,
                        manifest_artifact_id=item.manifest_artifact_id,
                        summary_json=item.summary,
                        created_at=item.created_at,
                    )
                )
                await session.flush()
        except IntegrityError as exc:
            existing = await self.get(item.source, item.capture_id)
            if existing is not None:
                return existing
            raise RepositoryConflictError(
                f"could not create connector submission {item.id!r}"
            ) from exc
        return item


def _from_record(row: ConnectorSubmissionRecord) -> ConnectorSubmission:
    return ConnectorSubmission(
        id=row.id,
        run_id=row.run_id,
        source=ConnectorSource(row.source),
        capture_id=row.capture_id,
        fingerprint=row.fingerprint,
        request_artifact_id=row.request_artifact_id,
        response_artifact_id=row.response_artifact_id,
        manifest_artifact_id=row.manifest_artifact_id,
        summary=row.summary_json,
        created_at=row.created_at,
    )
