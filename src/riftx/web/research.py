"""Bounded multi-query research that returns citation-safe Context packets."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from riftx.context.token_counter import estimate_context_tokens
from riftx.domain.base import DomainModel, new_id, utc_now

from .models import (
    EvidenceSpan,
    FetchRequest,
    FetchResultStatus,
    SourceReference,
    WebDocument,
    WebDocumentChunk,
)
from .search import SearchProvider, SearchRequest, SearchResponse, SearchResult, SearchType

UNTRUSTED_EXTERNAL_CONTENT = "UNTRUSTED_EXTERNAL_CONTENT"
_MAX_PACKET_TOKENS = 6_000


class PlannedSearchQuery(DomainModel):
    id: str = Field(default_factory=new_id)
    query: str = Field(min_length=1)
    search_type: SearchType = SearchType.GENERAL


class SearchPlan(DomainModel):
    original_question: str = Field(min_length=1)
    queries: list[PlannedSearchQuery] = Field(min_length=1, max_length=4)
    stop_condition: str = Field(min_length=1)
    max_total_results: int = Field(default=30, ge=1, le=100)


class ResearchRequest(DomainModel):
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=4_000)
    search_type: SearchType = SearchType.GENERAL
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    max_queries: int = Field(default=4, ge=1, le=4)
    max_results_per_query: int = Field(default=10, ge=1, le=20)
    max_total_results: int = Field(default=30, ge=1, le=50)
    max_sources: int = Field(default=6, ge=1, le=6)


class WebExtractionRequest(DomainModel):
    document_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    required_fields: list[str] = Field(default_factory=list)
    max_output_tokens: int = Field(default=2_000, ge=100, le=4_000)
    include_quotes: bool = True
    include_section_refs: bool = True


class WebResearchNote(DomainModel):
    id: str = Field(default_factory=new_id)
    document_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    key_points: list[str] = Field(default_factory=list)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    created_by_model: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    content_trust: Literal["UNTRUSTED_EXTERNAL_CONTENT"] = UNTRUSTED_EXTERNAL_CONTENT


class ResearchClaim(DomainModel):
    id: str = Field(default_factory=new_id)
    statement: str = Field(min_length=1)
    evidence: list[EvidenceSpan] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    time_sensitive: bool = False
    valid_as_of: AwareDatetime | None = None


class SourceDisagreement(DomainModel):
    topic: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=2)
    summary: str = Field(min_length=1)


class WebResearchPacket(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    key_claims: list[ResearchClaim] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    disagreements: list[SourceDisagreement] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    search_query_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    content_trust: Literal["UNTRUSTED_EXTERNAL_CONTENT"] = UNTRUSTED_EXTERNAL_CONTENT

    @model_validator(mode="after")
    def validate_citations(self) -> WebResearchPacket:
        source_ids = {source.id for source in self.sources}
        cited = {evidence.source_id for claim in self.key_claims for evidence in claim.evidence}
        if not cited.issubset(source_ids):
            raise ValueError("Research Claim evidence must reference a Packet Source")
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("Research Packet document IDs must be unique")
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("Research Packet Artifact IDs must be unique")
        return self


class WebContextPack(DomainModel):
    purpose: str
    summary: str
    claims: list[ResearchClaim]
    source_refs: list[str]
    document_refs: list[str]
    artifact_refs: list[str]
    token_estimate: int = Field(ge=0, le=_MAX_PACKET_TOKENS)
    untrusted_external_content: bool = True
    content_trust: Literal["UNTRUSTED_EXTERNAL_CONTENT"] = UNTRUSTED_EXTERNAL_CONTENT


class WebFetcher(Protocol):
    async def fetch(self, run_id: str, request: FetchRequest): ...


class FocusedExtractor(Protocol):
    async def extract(
        self,
        request: WebExtractionRequest,
        document: WebDocument,
        chunks: Sequence[WebDocumentChunk],
        source: SourceReference,
    ) -> WebResearchNote: ...


class ResearchRecorder(Protocol):
    async def record_search(
        self, run_id: str, session_id: str, response: SearchResponse
    ) -> None: ...

    async def record_note(self, run_id: str, note: WebResearchNote) -> None: ...

    async def record_packet(self, packet: WebResearchPacket) -> None: ...


class DeterministicQueryPlanner:
    """Generate a small auditable plan without provider-specific prompt coupling."""

    def plan(self, request: ResearchRequest) -> SearchPlan:
        question = " ".join(request.question.split())
        suffixes = _query_suffixes(request.search_type)
        values = [question]
        for suffix in suffixes:
            if len(values) >= request.max_queries:
                break
            values.append(f"{question} {suffix}")
        queries = [
            PlannedSearchQuery(query=value, search_type=request.search_type)
            for value in dict.fromkeys(values)
        ]
        return SearchPlan(
            original_question=question,
            queries=queries,
            stop_condition="enough independent canonical sources answer the question",
            max_total_results=request.max_total_results,
        )


class DeterministicFocusedExtractor:
    """Select relevant chunks and preserve exact offsets; no model inference is added."""

    async def extract(
        self,
        request: WebExtractionRequest,
        document: WebDocument,
        chunks: Sequence[WebDocumentChunk],
        source: SourceReference,
    ) -> WebResearchNote:
        terms = _terms(request.question)
        ranked = sorted(
            chunks,
            key=lambda chunk: (-_relevance(chunk.content, terms), chunk.sequence),
        )
        selected = [chunk for chunk in ranked if chunk.content][:3]
        evidence: list[EvidenceSpan] = []
        points: list[str] = []
        remaining_chars = request.max_output_tokens * 4
        for chunk in selected:
            if remaining_chars <= 0:
                break
            quote = chunk.content[: min(800, remaining_chars)].strip()
            if not quote:
                continue
            points.append(quote)
            evidence.append(
                EvidenceSpan(
                    source_id=source.id,
                    chunk_id=chunk.id,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.start_offset + len(quote),
                    quote=quote if request.include_quotes else None,
                    paraphrase=None if request.include_quotes else quote,
                    section_title=(chunk.heading_path[-1] if chunk.heading_path else None),
                )
            )
            remaining_chars -= len(quote)
        if not points:
            points = ["The fetched document contains no extractable text for this question."]
        return WebResearchNote(
            document_id=document.id,
            source_id=source.id,
            question=request.question,
            answer="\n\n".join(points),
            key_points=points,
            evidence_spans=evidence,
            missing_information=([] if evidence else [request.question]),
            confidence=min(1.0, 0.45 + 0.15 * len(evidence)),
        )


class WebResearchPipeline:
    """Search, rank, canonicalize, extract, and return only a bounded packet."""

    def __init__(
        self,
        *,
        providers: Sequence[SearchProvider],
        fetcher: WebFetcher,
        extractor: FocusedExtractor | None = None,
        planner: DeterministicQueryPlanner | None = None,
        recorder: ResearchRecorder | None = None,
    ) -> None:
        if not providers:
            raise ValueError("Research Pipeline requires at least one Search Provider")
        self._providers = list(providers)
        self._fetcher = fetcher
        self._extractor = extractor or DeterministicFocusedExtractor()
        self._planner = planner or DeterministicQueryPlanner()
        self._recorder = recorder

    async def research(self, request: ResearchRequest) -> WebResearchPacket:
        plan = self._planner.plan(request)
        searches = await asyncio.gather(
            *(
                self._search(request, query, self._providers[index % len(self._providers)])
                for index, query in enumerate(plan.queries)
            ),
            return_exceptions=True,
        )
        responses = [item for item in searches if isinstance(item, SearchResponse)]
        query_ids = [response.query_id for response in responses]
        candidates = rank_search_results(
            [result for response in responses for result in response.results],
            request.question,
            max_results=plan.max_total_results,
        )
        selected = _diverse_selection(candidates, request.max_sources)
        fetched = await asyncio.gather(
            *(self._fetch_and_extract(request, result) for result in selected),
            return_exceptions=True,
        )
        notes: list[WebResearchNote] = []
        sources: list[SourceReference] = []
        documents: list[WebDocument] = []
        for item in fetched:
            if isinstance(item, tuple):
                note, source, document = item
                notes.append(note)
                sources.append(source)
                documents.append(document)
        unresolved: list[str] = []
        failures = len([item for item in searches if isinstance(item, BaseException)])
        fetch_failures = len([item for item in fetched if isinstance(item, BaseException)])
        if failures:
            unresolved.append(f"{failures} planned search queries failed")
        if fetch_failures:
            unresolved.append(f"{fetch_failures} selected pages could not be canonicalized")
        if not notes:
            unresolved.append(request.question)
        packet = _synthesize(request, notes, sources, documents, query_ids, unresolved)
        if self._recorder is not None:
            await self._recorder.record_packet(packet)
        return packet

    async def _search(
        self,
        request: ResearchRequest,
        planned: PlannedSearchQuery,
        provider: SearchProvider,
    ) -> SearchResponse:
        response = await provider.search(
            SearchRequest(
                query=planned.query,
                max_results=request.max_results_per_query,
                allowed_domains=request.allowed_domains,
                blocked_domains=request.blocked_domains,
                search_type=planned.search_type,
            )
        )
        if self._recorder is not None:
            await self._recorder.record_search(request.run_id, request.session_id, response)
        return response

    async def _fetch_and_extract(
        self,
        request: ResearchRequest,
        result: SearchResult,
    ) -> tuple[WebResearchNote, SourceReference, WebDocument]:
        fetched = await self._fetcher.fetch(
            request.run_id,
            FetchRequest(url=result.normalized_url),
        )
        if (
            fetched.status is not FetchResultStatus.FETCHED
            or fetched.document is None
            or fetched.source is None
        ):
            raise RuntimeError("search candidate did not produce a canonical Source")
        note = await self._extractor.extract(
            WebExtractionRequest(
                document_id=fetched.document.id,
                question=request.question,
            ),
            fetched.document,
            fetched.chunks,
            fetched.source,
        )
        if self._recorder is not None:
            await self._recorder.record_note(request.run_id, note)
        return note, fetched.source, fetched.document


def rank_search_results(
    results: Sequence[SearchResult],
    question: str,
    *,
    max_results: int,
) -> list[SearchResult]:
    terms = _terms(question)
    deduplicated: dict[str, SearchResult] = {}
    for result in results:
        current = deduplicated.get(result.normalized_url)
        if current is None or result.provider_rank < current.provider_rank:
            deduplicated[result.normalized_url] = result
    now = utc_now()

    def score(result: SearchResult) -> tuple[float, str]:
        provider = 1.0 / result.provider_rank
        relevance = _relevance(f"{result.title} {result.snippet or ''}", terms)
        authority = _authority_score(result.domain)
        freshness = _freshness_score(result.published_at, now)
        final = 0.35 * provider + 0.20 * relevance + 0.15 * authority + 0.15 * freshness
        return -final, result.normalized_url

    return sorted(deduplicated.values(), key=score)[:max_results]


def packet_to_context(packet: WebResearchPacket) -> WebContextPack:
    summary = packet.summary
    claims = packet.key_claims
    while (
        estimate_context_tokens(
            {"summary": summary, "claims": [claim.model_dump(mode="json") for claim in claims]}
        )
        > _MAX_PACKET_TOKENS
    ):
        if claims:
            claims = claims[:-1]
        elif len(summary) > 200:
            summary = summary[: len(summary) // 2]
        else:
            break
    estimate = estimate_context_tokens(
        {"summary": summary, "claims": [claim.model_dump(mode="json") for claim in claims]}
    )
    return WebContextPack(
        purpose=packet.question,
        summary=summary,
        claims=claims,
        source_refs=[source.id for source in packet.sources],
        document_refs=packet.document_ids,
        artifact_refs=packet.artifact_ids,
        token_estimate=estimate,
    )


def _synthesize(
    request: ResearchRequest,
    notes: Sequence[WebResearchNote],
    sources: Sequence[SourceReference],
    documents: Sequence[WebDocument],
    query_ids: list[str],
    unresolved: list[str],
) -> WebResearchPacket:
    claims = [
        ResearchClaim(
            statement=note.answer[:1_200],
            evidence=note.evidence_spans,
            confidence=note.confidence,
            time_sensitive=request.search_type in {SearchType.NEWS, SearchType.CVE},
            valid_as_of=utc_now(),
        )
        for note in notes
        if note.evidence_spans
    ]
    summary = (
        "\n\n".join(note.answer[:800] for note in notes)[:4_000]
        if notes
        else "No canonical public sources answered the research question."
    )
    return WebResearchPacket(
        run_id=request.run_id,
        session_id=request.session_id,
        question=request.question,
        summary=summary,
        key_claims=claims,
        sources=list(sources),
        disagreements=[],
        unresolved_questions=unresolved,
        search_query_ids=list(dict.fromkeys(query_ids)),
        document_ids=list(dict.fromkeys(document.id for document in documents)),
        artifact_ids=list(
            dict.fromkeys(
                artifact_id
                for document in documents
                for artifact_id in (
                    document.raw_artifact_id,
                    document.normalized_artifact_id,
                )
                if artifact_id is not None
            )
        ),
    )


def _diverse_selection(results: Sequence[SearchResult], limit: int) -> list[SearchResult]:
    selected: list[SearchResult] = []
    deferred: list[SearchResult] = []
    domains: set[str] = set()
    for result in results:
        if result.domain in domains:
            deferred.append(result)
        else:
            domains.add(result.domain)
            selected.append(result)
        if len(selected) >= limit:
            return selected
    return (selected + deferred)[:limit]


def _terms(value: str) -> set[str]:
    return {item.lower() for item in re.findall(r"[\w.-]{2,}", value)}


def _relevance(value: str, terms: set[str]) -> float:
    if not terms:
        return 0.0
    haystack = value.lower()
    return sum(term in haystack for term in terms) / len(terms)


def _authority_score(domain: str) -> float:
    if domain.endswith(".gov") or domain.endswith(".edu"):
        return 1.0
    if any(token in domain for token in ("nist", "cve.org", "github.com", "docs.")):
        return 0.9
    return 0.5


def _freshness_score(value: datetime | None, now: datetime) -> float:
    if value is None:
        return 0.4
    days = max(0, (now - value).days)
    if days <= 30:
        return 1.0
    if days <= 365:
        return 0.7
    return 0.3


def _query_suffixes(search_type: SearchType) -> list[str]:
    if search_type in {SearchType.CVE, SearchType.SECURITY_ADVISORY, SearchType.EXPLOIT}:
        return ["vendor advisory", "NVD", "independent security analysis"]
    if search_type is SearchType.DOCUMENTATION:
        return ["official documentation", "reference", "release notes"]
    if search_type is SearchType.ACADEMIC:
        return ["paper", "study", "independent replication"]
    if search_type is SearchType.NEWS:
        return ["latest", "official statement", "independent coverage"]
    return ["official source", "documentation", "independent analysis"]
