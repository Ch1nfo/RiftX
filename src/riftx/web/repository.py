"""Persistence boundary for canonical public web sources."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import SourceReference, WebDocument, WebDocumentChunk


class WebSourceRepository(Protocol):
    async def save(
        self,
        document: WebDocument,
        chunks: list[WebDocumentChunk],
        source: SourceReference,
        *,
        cache_expires_at: datetime,
    ) -> None: ...

    async def get_cached(
        self,
        run_id: str,
        normalized_url: str,
        *,
        now: datetime,
    ) -> tuple[WebDocument, list[WebDocumentChunk], SourceReference] | None: ...

    async def get_document(self, document_id: str) -> WebDocument | None: ...

    async def get_source(self, source_id: str) -> SourceReference | None: ...

    async def list_chunks(self, document_id: str) -> list[WebDocumentChunk]: ...
