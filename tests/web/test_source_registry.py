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
from riftx.web import EvidenceSpan, SourceReference, WebDocument, WebDocumentChunk


async def test_source_registry_round_trip_and_cache_scope(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'sources.db'}")
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
            objective=Objective(description="Research public documentation"),
            workspace_path=str(tmp_path),
        )
    )
    repository = SQLAlchemyWebSourceRepository(database.session_factory)
    now = utc_now()
    content = "# Heading\n\nCanonical evidence"
    digest = hashlib.sha256(content.encode()).hexdigest()
    document = WebDocument(
        id="document-1",
        run_id="run-1",
        requested_url="https://example.com/doc",
        final_url="https://example.com/doc",
        fetched_at=now,
        mime_type="text/html",
        content_hash=digest,
        text_length=len(content),
        extraction_status="complete",
    )
    chunk = WebDocumentChunk(
        id="chunk-1",
        document_id=document.id,
        sequence=0,
        heading_path=["Heading"],
        content=content,
        token_count=8,
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
    try:
        await repository.save(
            document,
            [chunk],
            source,
            cache_expires_at=now + timedelta(hours=1),
        )
        cached = await repository.get_cached("run-1", "https://example.com/doc", now=now)
        assert cached == (document, [chunk], source)
        assert await repository.get_document(document.id) == document
        assert await repository.get_source(source.id) == source
        assert await repository.list_chunks(document.id) == [chunk]

        span = EvidenceSpan(
            source_id=source.id,
            chunk_id=chunk.id,
            start_offset=2,
            end_offset=9,
            quote="Heading",
            section_title="Heading",
        )
        assert span.source_id == source.id
    finally:
        await database.dispose()
