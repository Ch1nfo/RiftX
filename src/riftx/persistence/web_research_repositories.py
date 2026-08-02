"""Durable Search audit records and bounded Research outputs."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from riftx.application.errors import RepositoryConflictError
from riftx.domain.base import utc_now
from riftx.web.research import WebResearchNote, WebResearchPacket
from riftx.web.search import SearchResponse, SearchResult

from .orm import (
    SourceReferenceRecord,
    WebDocumentRecord,
    WebResearchNoteRecord,
    WebResearchPacketRecord,
    WebSearchQueryRecord,
    WebSearchResultRecord,
)
from .repositories import SessionFactory


class SQLAlchemyWebResearchRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def record_search(self, run_id: str, session_id: str, response: SearchResponse) -> None:
        request = response.request
        query = WebSearchQueryRecord(
            id=response.query_id,
            run_id=run_id,
            session_id=session_id,
            query=request.query,
            search_type=request.search_type.value,
            provider=response.provider,
            options_json={
                "max_results": request.max_results,
                "freshness": request.freshness.value if request.freshness else None,
                "allowed_domains": request.allowed_domains,
                "blocked_domains": request.blocked_domains,
                "language": request.language,
                "region": request.region,
            },
            status="completed",
            created_at=response.created_at,
            completed_at=utc_now(),
        )
        try:
            async with self._session_factory() as session, session.begin():
                session.add(query)
                await session.flush()
                session.add_all(_search_result_record(result) for result in response.results)
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not record Web Search Query {response.query_id!r}"
            ) from exc

    async def record_note(self, run_id: str, note: WebResearchNote) -> None:
        row = WebResearchNoteRecord(
            id=note.id,
            document_id=note.document_id,
            source_id=note.source_id,
            question=note.question,
            answer=note.answer,
            key_points_json=note.key_points,
            evidence_spans_json=[span.model_dump(mode="json") for span in note.evidence_spans],
            missing_information_json=note.missing_information,
            confidence=note.confidence,
            model_profile=note.created_by_model,
            content_trust=note.content_trust,
            created_at=note.created_at,
        )
        try:
            async with self._session_factory() as session, session.begin():
                document = await session.get(WebDocumentRecord, note.document_id)
                source = await session.get(SourceReferenceRecord, note.source_id)
                if (
                    document is None
                    or document.run_id != run_id
                    or source is None
                    or source.document_id != note.document_id
                ):
                    raise RepositoryConflictError(
                        "Web Research Note crosses its canonical Run/Source boundary"
                    )
                session.add(row)
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not record Web Research Note {note.id!r}"
            ) from exc

    async def record_packet(self, packet: WebResearchPacket) -> None:
        row = WebResearchPacketRecord(
            id=packet.id,
            run_id=packet.run_id,
            session_id=packet.session_id,
            question=packet.question,
            summary=packet.summary,
            claims_json=[claim.model_dump(mode="json") for claim in packet.key_claims],
            source_ids_json=[source.id for source in packet.sources],
            disagreements_json=[item.model_dump(mode="json") for item in packet.disagreements],
            unresolved_questions_json=packet.unresolved_questions,
            search_query_ids_json=packet.search_query_ids,
            document_ids_json=packet.document_ids,
            artifact_ids_json=packet.artifact_ids,
            content_trust=packet.content_trust,
            created_at=packet.created_at,
        )
        try:
            async with self._session_factory() as session, session.begin():
                session.add(row)
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not record Web Research Packet {packet.id!r}"
            ) from exc

    async def get_search(self, query_id: str) -> SearchResponse | None:
        async with self._session_factory() as session:
            query = await session.get(WebSearchQueryRecord, query_id)
            if query is None:
                return None
            rows = (
                await session.scalars(
                    select(WebSearchResultRecord)
                    .where(WebSearchResultRecord.query_id == query_id)
                    .order_by(WebSearchResultRecord.provider_rank)
                )
            ).all()
        return _search_response(query, list(rows))

    async def get_note(self, note_id: str) -> WebResearchNote | None:
        async with self._session_factory() as session:
            row = await session.get(WebResearchNoteRecord, note_id)
        if row is None:
            return None
        return WebResearchNote(
            id=row.id,
            document_id=row.document_id,
            source_id=row.source_id,
            question=row.question,
            answer=row.answer,
            key_points=row.key_points_json,
            evidence_spans=row.evidence_spans_json,
            missing_information=row.missing_information_json,
            confidence=row.confidence,
            created_by_model=row.model_profile,
            content_trust=row.content_trust,
            created_at=row.created_at,
        )

    async def get_packet(self, packet_id: str) -> WebResearchPacket | None:
        async with self._session_factory() as session:
            row = await session.get(WebResearchPacketRecord, packet_id)
        if row is None:
            return None
        from riftx.web.models import SourceReference

        source_rows = []
        if row.source_ids_json:
            async with self._session_factory() as session:
                source_rows = list(
                    (
                        await session.scalars(
                            select(SourceReferenceRecord).where(
                                SourceReferenceRecord.id.in_(row.source_ids_json)
                            )
                        )
                    ).all()
                )
        sources_by_id = {item.id: SourceReference.model_validate(item) for item in source_rows}
        return WebResearchPacket(
            id=row.id,
            run_id=row.run_id,
            session_id=row.session_id,
            question=row.question,
            summary=row.summary,
            key_claims=row.claims_json,
            sources=[
                sources_by_id[source_id]
                for source_id in row.source_ids_json
                if source_id in sources_by_id
            ],
            disagreements=row.disagreements_json,
            unresolved_questions=row.unresolved_questions_json,
            search_query_ids=row.search_query_ids_json,
            document_ids=row.document_ids_json,
            artifact_ids=row.artifact_ids_json,
            content_trust=row.content_trust,
            created_at=row.created_at,
        )


def _search_result_record(result: SearchResult) -> WebSearchResultRecord:
    return WebSearchResultRecord(
        id=result.id,
        query_id=result.search_query_id,
        title=result.title,
        url=str(result.url),
        normalized_url=result.normalized_url,
        snippet=result.snippet,
        domain=result.domain,
        published_at=result.published_at,
        provider=result.provider,
        provider_rank=result.provider_rank,
        metadata_json=result.provider_metadata,
    )


def _search_response(
    query: WebSearchQueryRecord, results: list[WebSearchResultRecord]
) -> SearchResponse:
    from riftx.web.search import SearchRequest

    options = query.options_json
    request = SearchRequest(
        query=query.query,
        max_results=options["max_results"],
        freshness=options.get("freshness"),
        allowed_domains=options.get("allowed_domains", []),
        blocked_domains=options.get("blocked_domains", []),
        language=options.get("language"),
        region=options.get("region"),
        search_type=query.search_type,
    )
    return SearchResponse(
        query_id=query.id,
        provider=query.provider,
        request=request,
        results=[
            SearchResult(
                id=row.id,
                title=row.title,
                url=row.url,
                normalized_url=row.normalized_url,
                snippet=row.snippet,
                domain=row.domain,
                published_at=row.published_at,
                provider=row.provider,
                provider_rank=row.provider_rank,
                provider_metadata=row.metadata_json,
                search_query_id=row.query_id,
            )
            for row in results
        ],
        created_at=query.created_at,
    )
