"""Durable idempotency records for authorized Target HTTP exchanges."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from riftx.application.errors import RepositoryConflictError
from riftx.domain.base import utc_now
from riftx.target_http.models import TargetHttpResult, TargetHttpSubmission

from .orm import TargetHttpRequestRecord
from .repositories import SessionFactory


class SQLAlchemyTargetHttpRequestRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get_by_execution_key(self, execution_key: str) -> TargetHttpResult | None:
        statement = select(TargetHttpRequestRecord).where(
            TargetHttpRequestRecord.execution_key == execution_key
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
        return TargetHttpResult.model_validate(row.result_json) if row is not None else None

    async def create(
        self,
        submission: TargetHttpSubmission,
        result: TargetHttpResult,
    ) -> TargetHttpResult:
        if submission.request.execution_key != result.execution_key:
            raise ValueError("Target HTTP result execution key does not match its request")
        row = TargetHttpRequestRecord(
            id=result.request_id,
            execution_key=result.execution_key,
            run_id=submission.run_id,
            session_id=submission.session_id,
            tool_call_id=submission.tool_call_id,
            node_id=submission.node_id,
            method=submission.request.method,
            url=submission.request.url,
            request_json=submission.request.runner_payload(),
            result_json=result.model_dump(mode="json"),
            request_artifact_id=result.request_artifact_id,
            response_artifact_id=result.response_artifact_id,
            created_at=utc_now(),
        )
        try:
            async with self._session_factory() as session, session.begin():
                session.add(row)
                await session.flush()
        except IntegrityError as exc:
            existing = await self.get_by_execution_key(result.execution_key)
            if existing is not None:
                return existing
            raise RepositoryConflictError(
                f"could not record Target HTTP request {result.request_id!r}"
            ) from exc
        return result
