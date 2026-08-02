from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

from riftx.domain import Engagement, Objective, Run
from riftx.domain.base import utc_now
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
)
from riftx.persistence.web_repositories import SQLAlchemyWebSourceRepository
from riftx.persistence.web_research_repositories import (
    SQLAlchemyWebResearchRepository,
)
from riftx.web import (
    EvidenceSpan,
    ResearchClaim,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SourceReference,
    WebDocument,
    WebDocumentChunk,
    WebResearchNote,
    WebResearchPacket,
)


async def test_search_note_and_packet_round_trip(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'research.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Research public sources"),
            workspace_path=str(tmp_path),
        )
    )
    now = utc_now()
    content = "Canonical public evidence"
    digest = hashlib.sha256(content.encode()).hexdigest()
    document = WebDocument(
        id="document-1",
        run_id="run-1",
        requested_url="https://example.com/doc",
        final_url="https://example.com/doc",
        fetched_at=now,
        mime_type="text/plain",
        content_hash=digest,
        text_length=len(content),
        extraction_status="complete",
    )
    chunk = WebDocumentChunk(
        id="chunk-1",
        document_id=document.id,
        sequence=0,
        content=content,
        token_count=6,
        start_offset=0,
        end_offset=len(content),
    )
    source = SourceReference(
        id="source-1",
        document_id=document.id,
        url=document.final_url,
        domain="example.com",
        fetched_at=now,
        content_hash=digest,
    )
    await SQLAlchemyWebSourceRepository(database.session_factory).save(
        document,
        [chunk],
        source,
        cache_expires_at=now + timedelta(hours=1),
    )
    repository = SQLAlchemyWebResearchRepository(database.session_factory)
    search_request = SearchRequest(query="canonical evidence")
    search = SearchResponse(
        query_id="query-1",
        provider="test",
        request=search_request,
        results=[
            SearchResult(
                id="result-1",
                title="Canonical",
                url=document.final_url,
                normalized_url=document.final_url,
                domain="example.com",
                provider="test",
                provider_rank=1,
                provider_metadata={"engine": "fixture"},
                search_query_id="query-1",
            )
        ],
        created_at=now,
    )
    span = EvidenceSpan(
        source_id=source.id,
        chunk_id=chunk.id,
        start_offset=0,
        end_offset=len(content),
        quote=content,
    )
    note = WebResearchNote(
        id="note-1",
        document_id=document.id,
        source_id=source.id,
        question="What is the evidence?",
        answer=content,
        key_points=[content],
        evidence_spans=[span],
        confidence=0.9,
        created_at=now,
    )
    packet = WebResearchPacket(
        id="packet-1",
        run_id="run-1",
        session_id="session-1",
        question=note.question,
        summary=note.answer,
        key_claims=[
            ResearchClaim(
                id="claim-1",
                statement=note.answer,
                evidence=[span],
                confidence=0.9,
            )
        ],
        sources=[source],
        search_query_ids=[search.query_id],
        document_ids=[document.id],
        created_at=now,
    )
    try:
        await repository.record_search("run-1", "session-1", search)
        await repository.record_note("run-1", note)
        await repository.record_packet(packet)

        assert await repository.get_search(search.query_id) == search
        assert await repository.get_note(note.id) == note
        assert await repository.get_packet(packet.id) == packet
    finally:
        await database.dispose()
