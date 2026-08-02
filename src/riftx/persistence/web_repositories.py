"""SQLAlchemy Source Registry for canonical public web documents."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from riftx.application.errors import RepositoryConflictError
from riftx.web.models import SourceReference, WebDocument, WebDocumentChunk

from .orm import SourceReferenceRecord, WebDocumentChunkRecord, WebDocumentRecord
from .repositories import SessionFactory


class SQLAlchemyWebSourceRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def save(
        self,
        document: WebDocument,
        chunks: list[WebDocumentChunk],
        source: SourceReference,
        *,
        cache_expires_at: datetime,
    ) -> None:
        if source.document_id != document.id:
            raise ValueError("Source must reference the saved WebDocument")
        if any(chunk.document_id != document.id for chunk in chunks):
            raise ValueError("all Chunks must belong to the saved WebDocument")
        try:
            async with self._session_factory() as session, session.begin():
                session.add(_document_record(document, cache_expires_at))
                await session.flush()
                session.add_all(_chunk_record(chunk) for chunk in chunks)
                session.add(_source_record(source))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not save WebDocument {document.id!r}") from exc

    async def get_cached(
        self,
        run_id: str,
        normalized_url: str,
        *,
        now: datetime,
    ) -> tuple[WebDocument, list[WebDocumentChunk], SourceReference] | None:
        statement = (
            select(WebDocumentRecord)
            .where(
                WebDocumentRecord.run_id == run_id,
                WebDocumentRecord.cache_expires_at > now,
                or_(
                    WebDocumentRecord.requested_url == normalized_url,
                    WebDocumentRecord.final_url == normalized_url,
                ),
            )
            .order_by(WebDocumentRecord.fetched_at.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
            if row is None:
                return None
            source_row = await session.scalar(
                select(SourceReferenceRecord).where(SourceReferenceRecord.document_id == row.id)
            )
            chunk_rows = (
                await session.scalars(
                    select(WebDocumentChunkRecord)
                    .where(WebDocumentChunkRecord.document_id == row.id)
                    .order_by(WebDocumentChunkRecord.sequence)
                )
            ).all()
        if source_row is None:
            return None
        return _document(row), [_chunk(item) for item in chunk_rows], _source(source_row)

    async def get_document(self, document_id: str) -> WebDocument | None:
        async with self._session_factory() as session:
            row = await session.get(WebDocumentRecord, document_id)
        return _document(row) if row is not None else None

    async def get_source(self, source_id: str) -> SourceReference | None:
        async with self._session_factory() as session:
            row = await session.get(SourceReferenceRecord, source_id)
        return _source(row) if row is not None else None

    async def list_chunks(self, document_id: str) -> list[WebDocumentChunk]:
        statement = (
            select(WebDocumentChunkRecord)
            .where(WebDocumentChunkRecord.document_id == document_id)
            .order_by(WebDocumentChunkRecord.sequence)
        )
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
        return [_chunk(row) for row in rows]


def _document_record(document: WebDocument, cache_expires_at: datetime) -> WebDocumentRecord:
    return WebDocumentRecord(
        id=document.id,
        run_id=document.run_id,
        requested_url=document.requested_url,
        final_url=document.final_url,
        canonical_url=document.canonical_url,
        title=document.title,
        author=document.author,
        site_name=document.site_name,
        published_at=document.published_at,
        fetched_at=document.fetched_at,
        mime_type=document.mime_type,
        language=document.language,
        raw_artifact_id=document.raw_artifact_id,
        normalized_artifact_id=document.normalized_artifact_id,
        content_hash=document.content_hash,
        text_length=document.text_length,
        extraction_status=document.extraction_status.value,
        truncated=document.truncated,
        source_class=document.source_class.value,
        destination_class=document.destination_class.value,
        cache_expires_at=cache_expires_at,
    )


def _document(row: WebDocumentRecord) -> WebDocument:
    return WebDocument(
        id=row.id,
        run_id=row.run_id,
        requested_url=row.requested_url,
        final_url=row.final_url,
        canonical_url=row.canonical_url,
        title=row.title,
        author=row.author,
        site_name=row.site_name,
        published_at=row.published_at,
        fetched_at=row.fetched_at,
        mime_type=row.mime_type,
        language=row.language,
        raw_artifact_id=row.raw_artifact_id,
        normalized_artifact_id=row.normalized_artifact_id,
        content_hash=row.content_hash,
        text_length=row.text_length,
        extraction_status=row.extraction_status,
        truncated=row.truncated,
        source_class=row.source_class,
        destination_class=row.destination_class,
    )


def _chunk_record(chunk: WebDocumentChunk) -> WebDocumentChunkRecord:
    return WebDocumentChunkRecord(
        id=chunk.id,
        document_id=chunk.document_id,
        sequence=chunk.sequence,
        heading_path_json=chunk.heading_path,
        content=chunk.content,
        token_count=chunk.token_count,
        start_offset=chunk.start_offset,
        end_offset=chunk.end_offset,
        embedding_json=chunk.embedding,
    )


def _chunk(row: WebDocumentChunkRecord) -> WebDocumentChunk:
    return WebDocumentChunk(
        id=row.id,
        document_id=row.document_id,
        sequence=row.sequence,
        heading_path=row.heading_path_json,
        content=row.content,
        token_count=row.token_count,
        start_offset=row.start_offset,
        end_offset=row.end_offset,
        embedding=row.embedding_json,
    )


def _source_record(source: SourceReference) -> SourceReferenceRecord:
    return SourceReferenceRecord(
        id=source.id,
        document_id=source.document_id,
        url=source.url,
        title=source.title,
        domain=source.domain,
        author=source.author,
        published_at=source.published_at,
        fetched_at=source.fetched_at,
        source_type=source.source_type.value,
        content_hash=source.content_hash,
    )


def _source(row: SourceReferenceRecord) -> SourceReference:
    return SourceReference(
        id=row.id,
        document_id=row.document_id,
        url=row.url,
        title=row.title,
        domain=row.domain,
        author=row.author,
        published_at=row.published_at,
        fetched_at=row.fetched_at,
        source_type=row.source_type,
        content_hash=row.content_hash,
    )
