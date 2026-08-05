"""Provider-neutral web search contracts and initial provider adapters."""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, cast
from urllib.parse import urljoin, urlsplit

import httpx
from openai import APITimeoutError
from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from riftx.domain.base import DomainModel, new_id, utc_now

from .fetch import normalize_public_url


class SearchFreshness(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class SearchType(StrEnum):
    GENERAL = "general"
    DOCUMENTATION = "documentation"
    SECURITY_ADVISORY = "security_advisory"
    CVE = "cve"
    EXPLOIT = "exploit"
    SOURCE_CODE = "source_code"
    ACADEMIC = "academic"
    NEWS = "news"


class SearchRequest(DomainModel):
    query: str = Field(min_length=1, max_length=2_000)
    max_results: int = Field(default=10, ge=1, le=50)
    freshness: SearchFreshness | None = None
    allowed_domains: list[str] = Field(default_factory=list, max_length=50)
    blocked_domains: list[str] = Field(default_factory=list, max_length=50)
    language: str | None = Field(default=None, max_length=32)
    region: str | None = Field(default=None, max_length=32)
    search_type: SearchType = SearchType.GENERAL

    @model_validator(mode="after")
    def normalize_domains(self) -> SearchRequest:
        query = self.query.strip()
        if not query:
            raise ValueError("search query must not be blank")
        allowed = _domains(self.allowed_domains)
        blocked = _domains(self.blocked_domains)
        if set(allowed).intersection(blocked):
            raise ValueError("a search domain cannot be both allowed and blocked")
        object.__setattr__(self, "query", query)
        object.__setattr__(self, "allowed_domains", allowed)
        object.__setattr__(self, "blocked_domains", blocked)
        return self


class SearchResult(DomainModel):
    """A discovery candidate, never a formal SourceReference."""

    id: str = Field(default_factory=new_id)
    title: str = Field(min_length=1, max_length=1_000)
    url: HttpUrl
    normalized_url: str = Field(min_length=1)
    snippet: str | None = Field(default=None, max_length=6_000)
    domain: str = Field(min_length=1, max_length=253)
    published_at: AwareDatetime | None = None
    provider: str = Field(min_length=1)
    provider_rank: int = Field(ge=1)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    search_query_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def bind_url_identity(self) -> SearchResult:
        normalized = normalize_public_url(str(self.url))
        object.__setattr__(self, "normalized_url", normalized)
        object.__setattr__(self, "domain", (urlsplit(normalized).hostname or "").lower())
        return self


class SearchResponse(DomainModel):
    query_id: str = Field(default_factory=new_id)
    provider: str = Field(min_length=1)
    request: SearchRequest
    results: list[SearchResult] = Field(default_factory=list, max_length=50)
    warnings: list[str] = Field(default_factory=list, max_length=16)
    artifact_id: str | None = Field(default=None, min_length=1)
    content_trust: Literal["UNTRUSTED_EXTERNAL_CONTENT"] = "UNTRUSTED_EXTERNAL_CONTENT"
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_result_query_ids(self) -> SearchResponse:
        if any(result.search_query_id != self.query_id for result in self.results):
            raise ValueError("Search Results must belong to the enclosing query")
        return self


class SearchProvider(Protocol):
    id: str

    async def search(self, request: SearchRequest) -> SearchResponse: ...


class SearchProviderError(RuntimeError):
    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
        self.status_code = status_code


class FederatedSearchProvider:
    """Query configured providers concurrently and return one bounded candidate set."""

    id = "federated_search"

    def __init__(
        self,
        providers: list[SearchProvider],
        *,
        warnings: list[str] | None = None,
    ) -> None:
        if not providers:
            raise ValueError("Federated Search requires at least one provider")
        self._providers = list(providers)
        self._warnings = [_bounded_text(item, 500) for item in (warnings or [])][:16]

    async def search(self, request: SearchRequest) -> SearchResponse:
        searches = await asyncio.gather(
            *(provider.search(request) for provider in self._providers),
            return_exceptions=True,
        )
        responses: list[SearchResponse] = []
        warnings = list(self._warnings)
        for provider, outcome in zip(self._providers, searches, strict=True):
            if isinstance(outcome, SearchResponse):
                responses.append(outcome)
                warnings.extend(outcome.warnings)
            else:
                warnings.append(f"search provider {provider.id!r} was unavailable")
        if not responses:
            raise SearchProviderError(
                self.id,
                "all configured Web Search providers failed",
                retryable=any(
                    isinstance(item, SearchProviderError) and item.retryable for item in searches
                ),
            )
        query_id = new_id()
        merged: list[SearchResult] = []
        seen: set[str] = set()
        maximum_rank = max((len(response.results) for response in responses), default=0)
        for provider_rank in range(maximum_rank):
            for response in responses:
                if provider_rank >= len(response.results):
                    continue
                candidate = response.results[provider_rank]
                if candidate.normalized_url in seen:
                    continue
                seen.add(candidate.normalized_url)
                merged.append(
                    candidate.model_copy(
                        update={
                            "search_query_id": query_id,
                            "provider_rank": len(merged) + 1,
                        }
                    )
                )
                if len(merged) >= request.max_results:
                    break
            if len(merged) >= request.max_results:
                break
        return SearchResponse(
            query_id=query_id,
            provider=self.id,
            request=request,
            results=merged,
            warnings=list(dict.fromkeys(warnings))[:16],
        )


class SearXNGSearchProvider:
    """Generic HTTP adapter for the documented SearXNG JSON Search API."""

    def __init__(
        self,
        endpoint: str,
        *,
        provider_id: str = "searxng",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 20,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.id = provider_id
        self._endpoint = urljoin(endpoint.rstrip("/") + "/", "search")
        self._client = client
        self._timeout = timeout_seconds
        self._headers = dict(headers or {})

    async def search(self, request: SearchRequest) -> SearchResponse:
        query_id = new_id()
        params = {
            "q": request.query,
            "format": "json",
            "language": request.language or "all",
            "safesearch": "0",
        }
        if request.freshness is not None:
            params["time_range"] = request.freshness.value
        category = _SEARXNG_CATEGORIES.get(request.search_type)
        if category is not None:
            params["categories"] = category
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(trust_env=False)
        try:
            response = await client.get(
                self._endpoint,
                params=params,
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise SearchProviderError(self.id, "SearXNG search timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                self.id, "SearXNG search transport failed", retryable=True
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code == 429:
            raise SearchProviderError(
                self.id,
                "SearXNG search rate limited",
                retryable=True,
                status_code=429,
            )
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPStatusError, ValueError) as exc:
            raise SearchProviderError(
                self.id,
                f"SearXNG returned an invalid response ({response.status_code})",
                retryable=response.status_code >= 500,
                status_code=response.status_code,
            ) from exc
        raw_results = payload.get("results", []) if isinstance(payload, dict) else []
        candidates: list[SearchResult] = []
        for rank, item in enumerate(raw_results, 1):
            if not isinstance(item, dict):
                continue
            result = _result_from_mapping(
                item,
                query_id=query_id,
                provider=self.id,
                rank=rank,
            )
            if result is not None:
                candidates.append(result)
        return SearchResponse(
            query_id=query_id,
            provider=self.id,
            request=request,
            results=_filter_and_deduplicate(candidates, request),
        )


class OpenAIHostedSearchProvider:
    """Normalize Responses API hosted web-search citations into SearchResult objects."""

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        provider_id: str = "openai_hosted_search",
        timeout_seconds: float = 60,
    ) -> None:
        self.id = provider_id
        self._client = client
        self._model = model
        self._timeout = timeout_seconds

    async def search(self, request: SearchRequest) -> SearchResponse:
        query_id = new_id()
        web_tool: dict[str, Any] = {
            "type": "web_search",
            "search_context_size": "medium",
        }
        if request.allowed_domains:
            web_tool["filters"] = {"allowed_domains": request.allowed_domains}
        prompt = request.query
        qualifiers = [
            value
            for value in (
                f"Language: {request.language}" if request.language else None,
                f"Region: {request.region}" if request.region else None,
                (
                    f"Freshness: only consider the past {request.freshness.value}."
                    if request.freshness
                    else None
                ),
            )
            if value
        ]
        if qualifiers:
            prompt += "\n" + "\n".join(qualifiers)
        try:
            response = await self._client.responses.create(
                model=self._model,
                input=prompt,
                tools=[web_tool],
                tool_choice="required",
                include=["web_search_call.action.sources"],
                max_tool_calls=1,
                store=False,
                timeout=self._timeout,
            )
        except (TimeoutError, APITimeoutError) as exc:
            raise SearchProviderError(
                self.id, "OpenAI hosted search timed out", retryable=True
            ) from exc
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            raise SearchProviderError(
                self.id,
                "OpenAI hosted search failed",
                retryable=status == 429 or (isinstance(status, int) and status >= 500),
                status_code=status if isinstance(status, int) else None,
            ) from exc
        candidates: list[SearchResult] = []
        for rank, item in enumerate(_openai_citations(response), 1):
            raw_url = item.get("url")
            if not isinstance(raw_url, str) or not _valid_http_url(raw_url):
                continue
            title = item.get("title") or (urlsplit(raw_url).hostname or raw_url)
            snippet = item.get("snippet")
            candidates.append(
                SearchResult(
                    title=_bounded_text(title, 1_000),
                    url=cast(HttpUrl, raw_url),
                    normalized_url=normalize_public_url(raw_url),
                    snippet=_bounded_text(snippet, 6_000) if snippet else None,
                    domain=(urlsplit(raw_url).hostname or "").lower(),
                    provider=self.id,
                    provider_rank=rank,
                    provider_metadata={"response_id": getattr(response, "id", None)},
                    search_query_id=query_id,
                )
            )
        return SearchResponse(
            query_id=query_id,
            provider=self.id,
            request=request,
            results=_filter_and_deduplicate(candidates, request),
        )


def _result_from_mapping(
    item: dict[str, Any],
    *,
    query_id: str,
    provider: str,
    rank: int,
) -> SearchResult | None:
    raw_url = item.get("url")
    title = item.get("title")
    if not isinstance(raw_url, str) or not _valid_http_url(raw_url):
        return None
    if not isinstance(title, str) or not title.strip():
        title = urlsplit(raw_url).hostname or raw_url
    normalized = normalize_public_url(raw_url)
    published = _published_at(item)
    snippet = item.get("content", item.get("snippet"))
    normalized_title = html.unescape(_strip_markup(str(title))).strip()
    if not normalized_title:
        normalized_title = urlsplit(normalized).hostname or normalized
    return SearchResult(
        title=_bounded_text(normalized_title, 1_000),
        url=cast(HttpUrl, raw_url),
        normalized_url=normalized,
        snippet=(
            _bounded_text(html.unescape(_strip_markup(str(snippet))).strip(), 6_000)
            if snippet
            else None
        ),
        domain=(urlsplit(normalized).hostname or "").lower(),
        published_at=published,
        provider=provider,
        provider_rank=rank,
        provider_metadata=_provider_metadata(item),
        search_query_id=query_id,
    )


def _filter_and_deduplicate(
    results: list[SearchResult], request: SearchRequest
) -> list[SearchResult]:
    selected: list[SearchResult] = []
    seen: set[str] = set()
    for result in results:
        if result.normalized_url in seen or not _domain_allowed(result.domain, request):
            continue
        seen.add(result.normalized_url)
        selected.append(result.model_copy(update={"provider_rank": len(selected) + 1}))
        if len(selected) >= request.max_results:
            break
    return selected


def _domain_allowed(domain: str, request: SearchRequest) -> bool:
    if any(domain == item or domain.endswith("." + item) for item in request.blocked_domains):
        return False
    if not request.allowed_domains:
        return True
    return any(domain == item or domain.endswith("." + item) for item in request.allowed_domains)


def _domains(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        candidate = value.strip().lower().rstrip(".")
        if "://" in candidate:
            candidate = urlsplit(candidate).hostname or ""
        candidate = candidate.removeprefix("*.")
        if not candidate or len(candidate) > 253 or "/" in candidate or " " in candidate:
            raise ValueError(f"invalid search domain {value!r}")
        normalized.append(candidate)
    return list(dict.fromkeys(normalized))


def _published_at(item: dict[str, Any]) -> datetime | None:
    value = next(
        (item.get(key) for key in ("publishedDate", "published_at", "pubdate") if item.get(key)),
        None,
    )
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _openai_citations(response: Any) -> list[dict[str, str | None]]:
    citations: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for output in getattr(response, "output", []):
        if getattr(output, "type", None) == "message":
            for content in getattr(output, "content", []):
                snippet = getattr(content, "text", None)
                for annotation in getattr(content, "annotations", []):
                    if getattr(annotation, "type", None) != "url_citation":
                        continue
                    url = getattr(annotation, "url", None)
                    if isinstance(url, str) and url not in seen:
                        seen.add(url)
                        citations.append(
                            {
                                "url": url,
                                "title": getattr(annotation, "title", None),
                                "snippet": snippet,
                            }
                        )
        if getattr(output, "type", None) == "web_search_call":
            action = getattr(output, "action", None)
            for source in getattr(action, "sources", None) or []:
                url = getattr(source, "url", None)
                if isinstance(url, str) and url not in seen:
                    seen.add(url)
                    citations.append({"url": url, "title": None, "snippet": None})
    return citations


def _valid_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)


def _strip_markup(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def _bounded_text(value: object, maximum: int) -> str:
    return str(value)[:maximum]


def _provider_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key, value in item.items():
        if key in {"title", "url", "content", "snippet"} or len(metadata) >= 20:
            continue
        bounded_key = str(key)[:128]
        if isinstance(value, str):
            metadata[bounded_key] = value[:2_000]
        elif value is None or isinstance(value, bool | int | float):
            metadata[bounded_key] = value
    return metadata


_SEARXNG_CATEGORIES = {
    SearchType.DOCUMENTATION: "it",
    SearchType.SECURITY_ADVISORY: "it",
    SearchType.CVE: "it",
    SearchType.EXPLOIT: "it",
    SearchType.SOURCE_CODE: "it",
    SearchType.ACADEMIC: "science",
    SearchType.NEWS: "news",
}
