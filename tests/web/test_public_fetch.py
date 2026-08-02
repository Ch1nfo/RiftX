from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import pytest

from riftx.web import (
    FetchRequest,
    FetchResultStatus,
    PublicDestinationError,
    PublicWebFetcher,
    RedirectPolicy,
)
from riftx.web.fetch import normalize_public_url
from riftx.web.models import SourceReference, WebDocument, WebDocumentChunk


class MemorySources:
    def __init__(self) -> None:
        self.rows: dict[
            tuple[str, str],
            tuple[WebDocument, list[WebDocumentChunk], SourceReference, datetime],
        ] = {}

    async def save(
        self,
        document: WebDocument,
        chunks: list[WebDocumentChunk],
        source: SourceReference,
        *,
        cache_expires_at: datetime,
    ) -> None:
        self.rows[(document.run_id, document.requested_url)] = (
            document,
            chunks,
            source,
            cache_expires_at,
        )

    async def get_cached(
        self,
        run_id: str,
        normalized_url: str,
        *,
        now: datetime,
    ) -> tuple[WebDocument, list[WebDocumentChunk], SourceReference] | None:
        row = self.rows.get((run_id, normalized_url))
        if row is None or row[3] <= now:
            return None
        return row[0], row[1], row[2]

    async def get_document(self, document_id: str) -> WebDocument | None:
        return next((row[0] for row in self.rows.values() if row[0].id == document_id), None)

    async def get_source(self, source_id: str) -> SourceReference | None:
        return next((row[2] for row in self.rows.values() if row[2].id == source_id), None)

    async def list_chunks(self, document_id: str) -> list[WebDocumentChunk]:
        return next((row[1] for row in self.rows.values() if row[0].id == document_id), [])


class MemoryArtifacts:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    async def save(self, run_id: str, **item: Any) -> str:
        self.items.append({"run_id": run_id, **item})
        return f"artifact-{len(self.items)}"


async def public_resolver(_: str, __: int) -> list[str]:
    return ["93.184.216.34"]


def fetcher(
    handler: httpx.MockTransport,
) -> tuple[PublicWebFetcher, MemorySources, MemoryArtifacts]:
    sources = MemorySources()
    artifacts = MemoryArtifacts()
    client = httpx.AsyncClient(transport=handler)
    return (
        PublicWebFetcher(
            sources=sources,
            artifacts=artifacts,
            client=client,
            resolver=public_resolver,
        ),
        sources,
        artifacts,
    )


async def test_static_html_becomes_canonical_source_and_cache_hit() -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="""
            <html lang="zh"><head><title>RiftX 文档</title>
            <link rel="canonical" href="/guide" />
            <meta name="author" content="RiftX Team" /></head>
            <body><nav>ignore</nav><main><h1>概览</h1>
            <p>这是可静态提取并作为证据引用的正文内容。</p></main>
            <script>ignored()</script></body></html>
            """,
        )

    service, _, artifacts = fetcher(httpx.MockTransport(handle))
    request = FetchRequest(url="https://example.com/article?utm_source=test")
    first = await service.fetch("run-1", request)
    second = await service.fetch("run-1", request)

    assert first.status is FetchResultStatus.FETCHED
    assert first.document is not None and first.source is not None
    assert first.document.canonical_url == "https://example.com/guide"
    assert first.document.title == "RiftX 文档"
    assert first.document.author == "RiftX Team"
    assert first.source.url == "https://example.com/article"
    assert first.source.document_id == first.document.id
    assert first.chunks[0].content.startswith("# 概览")
    assert [item["mime_type"] for item in artifacts.items] == [
        "text/html",
        "text/markdown; charset=utf-8",
    ]
    assert second.cache_hit is True
    assert calls == 1


async def test_large_html_is_split_into_bounded_overlapping_chunks() -> None:
    body = "<h1>Large</h1>" + "".join(
        f"<p>Paragraph {index} contains public research material.</p>" for index in range(500)
    )
    service, _, _ = fetcher(
        httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "text/html"}, text=body)
        )
    )
    result = await service.fetch("run-1", FetchRequest(url="https://example.com/large"))

    assert result.document is not None
    assert result.document.text_length > 10_000
    assert len(result.chunks) >= 3
    assert [chunk.sequence for chunk in result.chunks] == list(range(len(result.chunks)))
    assert all(chunk.token_count > 0 for chunk in result.chunks)


@pytest.mark.parametrize(
    ("mime_type", "body", "expected"),
    [
        ("text/markdown", b"# Advisory\n\nFixed in 2.0", "# Advisory"),
        ("text/plain", b"plain public note", "plain public note"),
        ("application/json", b'{"fixed":true,"version":2}', '"fixed": true'),
        ("application/xml", b"<root><item>patched</item></root>", "patched"),
    ],
)
async def test_structured_and_text_formats_are_normalized(
    mime_type: str, body: bytes, expected: str
) -> None:
    service, _, artifacts = fetcher(
        httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": mime_type}, content=body)
        )
    )
    result = await service.fetch("run-1", FetchRequest(url="https://example.com/data"))

    assert result.status is FetchResultStatus.FETCHED
    assert result.chunks and expected in result.chunks[0].content
    assert artifacts.items[0]["content"] == body


async def test_pdf_text_layer_is_extracted() -> None:
    pdf = _text_pdf("RiftX PDF evidence")
    service, _, artifacts = fetcher(
        httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf)
        )
    )
    result = await service.fetch("run-1", FetchRequest(url="https://example.com/report.pdf"))

    assert result.document is not None
    assert result.document.mime_type == "application/pdf"
    assert result.chunks and "RiftX PDF evidence" in result.chunks[0].content
    assert artifacts.items[0]["name"] == "report.pdf"


async def test_same_origin_redirect_is_followed() -> None:
    visited: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="final")

    service, _, _ = fetcher(httpx.MockTransport(handle))
    result = await service.fetch("run-1", FetchRequest(url="https://example.com/old"))

    assert result.status is FetchResultStatus.FETCHED
    assert result.final_url == "https://example.com/new"
    assert result.redirect_chain == ["https://example.com/new"]
    assert visited == ["https://example.com/old", "https://example.com/new"]


async def test_cross_origin_redirect_is_returned_without_fetching_destination() -> None:
    visited: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example/item"})

    service, sources, artifacts = fetcher(httpx.MockTransport(handle))
    result = await service.fetch("run-1", FetchRequest(url="https://example.com/short"))

    assert result.status is FetchResultStatus.REDIRECT
    assert result.redirect_url == "https://other.example/item"
    assert visited == ["https://example.com/short"]
    assert sources.rows == {}
    assert artifacts.items == []


async def test_all_auto_redirect_revalidates_cross_origin_destination() -> None:
    calls = 0

    async def resolver(host: str, _: int) -> list[str]:
        return ["93.184.216.34"] if host == "example.com" else ["127.0.0.1"]

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "http://internal.example/admin"})

    sources = MemorySources()
    artifacts = MemoryArtifacts()
    service = PublicWebFetcher(
        sources=sources,
        artifacts=artifacts,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        resolver=resolver,
    )
    with pytest.raises(PublicDestinationError, match="non-public"):
        await service.fetch(
            "run-1",
            FetchRequest(
                url="https://example.com/start",
                redirect_policy=RedirectPolicy.ALL_AUTO,
            ),
        )
    assert calls == 1


async def test_javascript_shell_requires_browser_without_creating_source() -> None:
    service, sources, artifacts = fetcher(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<div id="root"></div><script src="app.js"></script>',
            )
        )
    )
    result = await service.fetch("run-1", FetchRequest(url="https://example.com/app"))

    assert result.status is FetchResultStatus.BROWSER_FALLBACK_REQUIRED
    assert result.source is None and result.document is None
    assert result.raw_artifact_id == "artifact-1"
    assert sources.rows == {}
    assert len(artifacts.items) == 1


async def test_encoding_error_falls_back_without_losing_text() -> None:
    body = "café — security note".encode("windows-1252")
    service, _, _ = fetcher(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "text/plain; charset=not-a-codec"},
                content=body,
            )
        )
    )
    result = await service.fetch("run-1", FetchRequest(url="https://example.com/note"))

    assert result.chunks[0].content == "café — security note"


async def test_response_limit_marks_document_truncated() -> None:
    service, _, artifacts = fetcher(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b"a" * 4096,
            )
        )
    )
    result = await service.fetch(
        "run-1",
        FetchRequest(url="https://example.com/large.txt", max_response_bytes=1024),
    )

    assert result.document is not None
    assert result.document.truncated is True
    assert result.document.text_length == 1024
    assert len(artifacts.items[0]["content"]) == 1024


async def test_literal_private_address_is_rejected_before_transport() -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="should not run")

    service, _, _ = fetcher(httpx.MockTransport(handle))
    with pytest.raises(PublicDestinationError, match="non-public"):
        await service.fetch("run-1", FetchRequest(url="http://127.0.0.1/admin"))
    assert calls == 0


def test_public_fetch_rejects_embedded_credentials() -> None:
    with pytest.raises(PublicDestinationError, match="credentials"):
        normalize_public_url("https://user:secret@example.com/")


def _text_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(payload)
