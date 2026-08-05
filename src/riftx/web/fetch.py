"""SSRF-safe public HTTP fetch, extraction, artifact capture, and Source creation."""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Protocol, cast
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
    ServiceUnavailableError,
    resource_not_accessible,
)
from riftx.application.ports import AuditAggregateReadRepository, AuditAuthorizationBinding
from riftx.application.run_kind_effects import (
    EffectMode,
    EffectOrigin,
    OperationEffect,
    RunEffectOperation,
)
from riftx.application.services.artifacts import (
    ArtifactApplicationService,
    RegisterArtifactContent,
)
from riftx.application.services.runs import require_run_kind_effect_operation
from riftx.context.token_counter import estimate_context_tokens
from riftx.domain import ArtifactContentTrust, Run, RunKind, RunStatus
from riftx.domain.base import utc_now

from .models import (
    CachePolicy,
    ExtractionStatus,
    FetchRequest,
    FetchResult,
    FetchResultStatus,
    RedirectPolicy,
    SourceReference,
    SourceType,
    WebDocument,
    WebDocumentChunk,
)
from .repository import WebSourceRepository

_DEFAULT_HEADERS = {
    "User-Agent": "RiftX-WebResearch/2",
    "Accept": (
        "text/markdown, text/html, application/xhtml+xml, application/json, "
        "application/xml, text/plain, application/pdf"
    ),
}
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_MAX_REDIRECTS = 10
_CHARSET_PATTERN = re.compile(r"charset\s*=\s*['\"]?([^\s;'\"]+)", re.I)
_WEB_EFFECT_BLOCKED_RUN_STATUSES = frozenset(
    {
        RunStatus.PAUSING,
        RunStatus.PAUSED,
        RunStatus.CANCELLING,
        RunStatus.CANCELLED,
        RunStatus.COMPLETING,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    }
)


class WebFetchError(RuntimeError):
    """The public fetch could not produce a canonical document."""


class PublicDestinationError(WebFetchError):
    """A URL resolved outside the public Internet boundary."""


class WebArtifactStore(Protocol):
    async def save(
        self,
        run_id: str,
        *,
        name: str,
        mime_type: str,
        content: bytes,
        description: str,
    ) -> str: ...


class RunRepository(Protocol):
    async def get(self, run_id: str) -> Run | None: ...


class ApplicationWebArtifactStore:
    """Save web payloads through RiftX's immutable Run Artifact service."""

    def __init__(
        self,
        service: ArtifactApplicationService,
        *,
        runs: RunRepository,
        audits: AuditAggregateReadRepository,
    ) -> None:
        self._service = service
        self._runs = runs
        self._audits = audits

    async def save(
        self,
        run_id: str,
        *,
        name: str,
        mime_type: str,
        content: bytes,
        description: str,
    ) -> str:
        command = RegisterArtifactContent(
            content=content,
            name=name,
            mime_type=mime_type,
            description=description,
            content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
        )
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        if run.kind is RunKind.GENERAL:
            artifact = await self._service.register_content(run_id, command)
            return artifact.id
        if run.kind is not RunKind.CODE_AUDIT:
            raise resource_not_accessible()

        audit_id: str | None = None

        def authorize(binding: AuditAuthorizationBinding) -> None:
            nonlocal audit_id
            if (
                binding.requested_audit_id != binding.audit_id
                or binding.scan_run_id != run.id
                or binding.run_id != run.id
                or binding.run_kind != RunKind.CODE_AUDIT.value
            ):
                raise resource_not_accessible()
            audit_id = binding.audit_id

        try:
            aggregate = await self._audits.get_by_run_authorized(
                run.id,
                authorize=authorize,
            )
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise ServiceUnavailableError(
                "web_artifact_owner_unavailable",
                "Code Audit Web Artifact ownership is temporarily unavailable",
            ) from None
        if (
            aggregate is None
            or audit_id is None
            or aggregate.audit.value.id != audit_id
            or aggregate.run.id != run.id
        ):
            raise resource_not_accessible()
        artifact = await self._service.register_audit_content(
            audit_id,
            run.id,
            command,
        )
        return artifact.id


Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


@dataclass(frozen=True, slots=True)
class _Extraction:
    text: str
    title: str | None = None
    author: str | None = None
    site_name: str | None = None
    published_at: datetime | None = None
    canonical_url: str | None = None
    language: str | None = None
    status: ExtractionStatus = ExtractionStatus.COMPLETE
    reason: str | None = None


class PublicWebFetcher:
    """Fetch only anonymous, public destinations and register canonical Sources."""

    def __init__(
        self,
        *,
        runs: RunRepository,
        sources: WebSourceRepository,
        artifacts: WebArtifactStore,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
        cache_ttl_seconds: int = 3600,
    ) -> None:
        if cache_ttl_seconds < 1:
            raise ValueError("cache_ttl_seconds must be positive")
        self._runs = runs
        self._sources = sources
        self._artifacts = artifacts
        self._client = client
        self._resolver = resolver or _resolve_host
        self._cache_ttl = timedelta(seconds=cache_ttl_seconds)

    async def fetch(self, run_id: str, request: FetchRequest) -> FetchResult:
        await self._require_effects_allowed(run_id)
        requested_url = normalize_public_url(str(request.url))
        # Cache lookup must not become an SSRF-policy bypass when DNS changes or
        # durable registry rows are imported from another environment.
        await ensure_public_destination(requested_url, self._resolver)
        now = utc_now()
        if request.cache_policy is CachePolicy.DEFAULT:
            cached = await self._sources.get_cached(run_id, requested_url, now=now)
            if cached is not None:
                document, chunks, source = cached
                return FetchResult(
                    status=FetchResultStatus.FETCHED,
                    requested_url=requested_url,
                    final_url=document.final_url,
                    document=document,
                    chunks=chunks,
                    source=source,
                    cache_hit=True,
                )

        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        try:
            response, final_url, chain, redirect_url = await self._request(
                client, run_id, requested_url, request
            )
            if redirect_url is not None:
                return FetchResult(
                    status=FetchResultStatus.REDIRECT,
                    requested_url=requested_url,
                    final_url=final_url,
                    redirect_url=redirect_url,
                    redirect_chain=chain,
                    reason="redirect requires an explicit cross-origin fetch",
                )
            assert response is not None
            try:
                raw, truncated = await _bounded_body(response, request.max_response_bytes)
            finally:
                await response.aclose()
        finally:
            if owns_client:
                await client.aclose()

        await self._require_effects_allowed(run_id)
        mime_type = _mime_type(response, final_url)
        extraction = _extract(raw, mime_type, response.headers.get("content-type"), final_url)
        if extraction.status is ExtractionStatus.BROWSER_FALLBACK_REQUIRED:
            if request.use_browser_fallback:
                raw_artifact_id = None
                if request.save_raw:
                    raw_artifact_id = await self._artifacts.save(
                        run_id,
                        name=_artifact_name(final_url, mime_type, normalized=False),
                        mime_type=mime_type,
                        content=raw,
                        description=f"Raw JavaScript shell response from {final_url}",
                    )
                return FetchResult(
                    status=FetchResultStatus.BROWSER_FALLBACK_REQUIRED,
                    requested_url=requested_url,
                    final_url=final_url,
                    redirect_chain=chain,
                    reason=extraction.reason,
                    raw_artifact_id=raw_artifact_id,
                )
            extraction = _Extraction(
                text=extraction.text,
                title=extraction.title,
                canonical_url=extraction.canonical_url,
                language=extraction.language,
                status=ExtractionStatus.PARTIAL,
                reason=extraction.reason,
            )

        raw_artifact_id = None
        if request.save_raw:
            raw_artifact_id = await self._artifacts.save(
                run_id,
                name=_artifact_name(final_url, mime_type, normalized=False),
                mime_type=mime_type,
                content=raw,
                description=f"Raw public web response from {final_url}",
            )
        normalized = extraction.text.strip()
        normalized_bytes = normalized.encode("utf-8")
        normalized_artifact_id = await self._artifacts.save(
            run_id,
            name=_artifact_name(final_url, "text/markdown", normalized=True),
            mime_type="text/markdown; charset=utf-8",
            content=normalized_bytes,
            description=f"Normalized public web document from {final_url}",
        )
        status = extraction.status
        if truncated and status is ExtractionStatus.COMPLETE:
            status = ExtractionStatus.PARTIAL
        document = WebDocument(
            run_id=run_id,
            requested_url=requested_url,
            final_url=final_url,
            canonical_url=_safe_canonical(extraction.canonical_url, final_url),
            title=extraction.title,
            author=extraction.author,
            site_name=extraction.site_name,
            published_at=extraction.published_at,
            fetched_at=now,
            mime_type=mime_type,
            language=extraction.language,
            raw_artifact_id=raw_artifact_id,
            normalized_artifact_id=normalized_artifact_id,
            content_hash=hashlib.sha256(normalized_bytes).hexdigest(),
            text_length=len(normalized),
            extraction_status=status,
            truncated=truncated,
        )
        chunks = chunk_document(document.id, normalized)
        source = SourceReference(
            document_id=document.id,
            url=final_url,
            title=document.title,
            domain=(urlsplit(final_url).hostname or "").lower(),
            author=document.author,
            published_at=document.published_at,
            fetched_at=document.fetched_at,
            source_type=SourceType.UNKNOWN,
            content_hash=document.content_hash,
        )
        expires_at = now if request.cache_policy is CachePolicy.NO_STORE else now + self._cache_ttl
        await self._sources.save(
            document,
            chunks,
            source,
            cache_expires_at=expires_at,
        )
        return FetchResult(
            status=FetchResultStatus.FETCHED,
            requested_url=requested_url,
            final_url=final_url,
            redirect_chain=chain,
            document=document,
            chunks=chunks,
            source=source,
        )

    async def _request(
        self,
        client: httpx.AsyncClient,
        run_id: str,
        requested_url: str,
        request: FetchRequest,
    ) -> tuple[httpx.Response | None, str, list[str], str | None]:
        current = requested_url
        chain: list[str] = []
        headers = {**_DEFAULT_HEADERS, **request.headers}
        for _ in range(_MAX_REDIRECTS + 1):
            await self._require_effects_allowed(run_id)
            await ensure_public_destination(current, self._resolver)
            outbound = client.build_request(
                "GET",
                current,
                headers=headers,
                timeout=request.timeout_seconds,
            )
            response = await client.send(outbound, stream=True, follow_redirects=False)
            if response.status_code not in _REDIRECT_CODES:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    await response.aclose()
                    raise WebFetchError(
                        f"public fetch returned HTTP {response.status_code} for {current}"
                    ) from exc
                return response, current, chain, None
            location = response.headers.get("location")
            if not location:
                await response.aclose()
                raise WebFetchError(f"redirect response from {current} has no Location")
            destination = normalize_public_url(urljoin(current, location))
            chain.append(destination)
            same_origin = _origin(current) == _origin(destination)
            await response.aclose()
            if request.redirect_policy is RedirectPolicy.NONE:
                return None, current, chain, destination
            if not same_origin and request.redirect_policy is not RedirectPolicy.ALL_AUTO:
                return None, current, chain, destination
            current = destination
        raise WebFetchError(f"public fetch exceeded {_MAX_REDIRECTS} redirects")

    async def _require_effects_allowed(self, run_id: str) -> Run:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        require_run_kind_effect_operation(
            run,
            operation=RunEffectOperation.SERVICE_WEB_FETCH,
            origin=EffectOrigin.APPLICATION_SERVICE,
            effect=OperationEffect.HOST_EXECUTION,
            mode=EffectMode.NORMAL,
        )
        if run.status in _WEB_EFFECT_BLOCKED_RUN_STATUSES:
            raise ApplicationConflictError(
                "run_web_fetch_blocked",
                f"Run {run.id!r} cannot perform Public Web Fetch while it is "
                f"{run.status.value}",
                details={"run_id": run.id, "status": run.status.value},
            )
        return run


async def ensure_public_destination(url: str, resolver: Resolver) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or host is None:
        raise PublicDestinationError("Public Fetch requires an HTTP(S) URL with a host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = list(await resolver(host, port))
    if not addresses:
        raise PublicDestinationError(f"could not resolve public destination {host!r}")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise PublicDestinationError(f"resolver returned invalid address {value!r}") from exc
        if not address.is_global:
            raise PublicDestinationError(
                f"Public Fetch rejected non-public destination {host!r} ({address})"
            )


async def _resolve_host(host: str, port: int) -> Sequence[str]:
    try:
        records = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PublicDestinationError(f"could not resolve public destination {host!r}") from exc
    return list(dict.fromkeys(cast(str, record[4][0]) for record in records))


def normalize_public_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise PublicDestinationError("Public Fetch accepts only absolute HTTP(S) URLs")
    if parsed.username is not None or parsed.password is not None:
        raise PublicDestinationError("Public Fetch does not accept URL credentials")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PublicDestinationError("Public Fetch URL contains an invalid port") from exc
    default_port = 443 if scheme == "https" else 80
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    path = parsed.path or "/"
    query = urlencode(
        [
            (key, val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
        ],
        doseq=True,
    )
    return urlunsplit((scheme, authority, path, query, ""))


async def _bounded_body(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
    body = bytearray()
    async for part in response.aiter_bytes():
        remaining = limit + 1 - len(body)
        if remaining <= 0:
            break
        body.extend(part[:remaining])
        if len(body) > limit:
            return bytes(body[:limit]), True
    return bytes(body), False


def _mime_type(response: httpx.Response, url: str) -> str:
    header = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if header:
        if header in {"application/xhtml+xml"}:
            return "text/html"
        if header in {"text/xml"}:
            return "application/xml"
        return header
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    return {
        ".html": "text/html",
        ".htm": "text/html",
        ".md": "text/markdown",
        ".json": "application/json",
        ".xml": "application/xml",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
    }.get(suffix, "application/octet-stream")


def _extract(raw: bytes, mime_type: str, content_type: str | None, url: str) -> _Extraction:
    if mime_type == "application/pdf":
        return _extract_pdf(raw)
    if mime_type.startswith("image/") or mime_type in {
        "application/zip",
        "application/octet-stream",
    }:
        return _Extraction(text="", status=ExtractionStatus.BINARY_ONLY)
    text = _decode(raw, content_type)
    if mime_type == "text/html":
        return _extract_html(text, url)
    if mime_type == "application/json" or mime_type.endswith("+json"):
        try:
            return _Extraction(text=json.dumps(json.loads(text), ensure_ascii=False, indent=2))
        except json.JSONDecodeError as exc:
            raise WebFetchError("response declared JSON but could not be parsed") from exc
    if mime_type == "application/xml" or mime_type.endswith("+xml"):
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise WebFetchError("response declared XML but could not be parsed") from exc
        return _Extraction(text="\n".join(_clean(item) for item in root.itertext() if _clean(item)))
    return _Extraction(text=text)


def _decode(raw: bytes, content_type: str | None) -> str:
    candidates: list[str] = []
    match = _CHARSET_PATTERN.search(content_type or "")
    if match:
        candidates.append(match.group(1))
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append("utf-16")
    candidates.extend(["utf-8", "windows-1252"])
    for encoding in dict.fromkeys(candidates):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


class _HTMLExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.author: str | None = None
        self.site_name: str | None = None
        self.published_at: str | None = None
        self.canonical_url: str | None = None
        self.language: str | None = None
        self.parts: list[str] = []
        self._ignored = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self.script_count = 0
        self.root_shell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "html":
            self.language = values.get("lang") or None
        if tag in {"script", "style", "nav", "footer", "noscript", "svg"}:
            self._ignored += 1
            if tag == "script":
                self.script_count += 1
            return
        if self._ignored:
            return
        if tag == "title":
            self._in_title = True
        if tag == "link" and values.get("rel", "").lower() == "canonical":
            self.canonical_url = values.get("href") or None
        if tag == "meta":
            key = (values.get("name") or values.get("property") or "").lower()
            content = values.get("content") or ""
            if key in {"author", "article:author"}:
                self.author = content or self.author
            elif key in {"og:site_name", "application-name"}:
                self.site_name = content or self.site_name
            elif key in {"article:published_time", "date", "datepublished"}:
                self.published_at = content or self.published_at
            elif key in {"og:title", "twitter:title"} and not self.title:
                self.title = content or None
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "section", "article", "main", "li", "tr", "pre"}:
            self.parts.append("\n")
        if tag == "div" and values.get("id", "").lower() in {"root", "app", "__next"}:
            self.root_shell = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "footer", "noscript", "svg"}:
            self._ignored = max(0, self._ignored - 1)
            return
        if self._ignored:
            return
        if tag == "title":
            self._in_title = False
            title = _clean(" ".join(self._title_parts))
            self.title = title or self.title
        if tag in {"p", "div", "section", "article", "main", "li", "tr", "pre"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        cleaned = _clean(data)
        if cleaned:
            self.parts.append(cleaned + " ")


def _extract_html(value: str, url: str) -> _Extraction:
    parser = _HTMLExtractor()
    parser.feed(value)
    text = _normalize_lines("".join(parser.parts))
    lower = value.lower()
    js_message = "enable javascript" in lower or "javascript is required" in lower
    if len(text) < 80 and (js_message or (parser.root_shell and parser.script_count > 0)):
        return _Extraction(
            text=text,
            title=parser.title,
            canonical_url=urljoin(url, parser.canonical_url) if parser.canonical_url else None,
            language=parser.language,
            status=ExtractionStatus.BROWSER_FALLBACK_REQUIRED,
            reason="HTML is a JavaScript application shell without useful static content",
        )
    return _Extraction(
        text=text,
        title=parser.title,
        author=parser.author,
        site_name=parser.site_name,
        published_at=_parse_datetime(parser.published_at),
        canonical_url=urljoin(url, parser.canonical_url) if parser.canonical_url else None,
        language=parser.language,
    )


def _extract_pdf(raw: bytes) -> _Extraction:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - packaging fault, not content behavior
        raise WebFetchError("PDF extraction requires the pypdf runtime dependency") from exc
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise WebFetchError("response declared PDF but could not be parsed") from exc
    metadata = reader.metadata
    title = str(metadata.title) if metadata and metadata.title else None
    return _Extraction(text="\n\n".join(pages), title=title)


def chunk_document(
    document_id: str,
    content: str,
    *,
    target_chars: int = 4_000,
    overlap_chars: int = 480,
) -> list[WebDocumentChunk]:
    if not content:
        return []
    if target_chars < 100 or overlap_chars < 0 or overlap_chars >= target_chars:
        raise ValueError("invalid document chunk sizing")
    chunks: list[WebDocumentChunk] = []
    start = 0
    sequence = 0
    while start < len(content):
        proposed = min(start + target_chars, len(content))
        end = proposed
        if proposed < len(content):
            boundary = content.rfind("\n", start + target_chars // 2, proposed)
            if boundary > start:
                end = boundary
        piece = content[start:end].strip()
        actual_start = start
        while actual_start < end and content[actual_start].isspace():
            actual_start += 1
        actual_end = actual_start + len(piece)
        if piece:
            chunks.append(
                WebDocumentChunk(
                    document_id=document_id,
                    sequence=sequence,
                    heading_path=_heading_path(content, actual_start),
                    content=piece,
                    token_count=estimate_context_tokens(piece),
                    start_offset=actual_start,
                    end_offset=actual_end,
                )
            )
            sequence += 1
        if end >= len(content):
            break
        start = max(end - overlap_chars, start + 1)
    return chunks


def _heading_path(content: str, offset: int) -> list[str]:
    headings: dict[int, str] = {}
    for line in content[:offset].splitlines():
        match = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if not match:
            continue
        level = len(match.group(1))
        headings[level] = match.group(2).strip()
        for deeper in tuple(key for key in headings if key > level):
            headings.pop(deeper)
    return [headings[level] for level in sorted(headings)]


def _artifact_name(url: str, mime_type: str, *, normalized: bool) -> str:
    path_name = PurePosixPath(urlsplit(url).path).name or "index"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", path_name).strip(".-") or "index"
    if normalized:
        stem = PurePosixPath(safe).stem or "document"
        return f"{stem}.normalized.md"
    if "." not in safe:
        extension = {
            "text/html": ".html",
            "text/markdown": ".md",
            "text/plain": ".txt",
            "application/json": ".json",
            "application/xml": ".xml",
            "application/pdf": ".pdf",
        }.get(mime_type, ".bin")
        safe += extension
    return safe


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _safe_canonical(value: str | None, final_url: str) -> str | None:
    if value is None:
        return None
    try:
        return normalize_public_url(value)
    except PublicDestinationError:
        return final_url


def _clean(value: str) -> str:
    return " ".join(value.split())


def _normalize_lines(value: str) -> str:
    lines = [line.strip() for line in value.splitlines()]
    compact: list[str] = []
    for line in lines:
        if line or (compact and compact[-1]):
            compact.append(line)
    return "\n".join(compact).strip()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
