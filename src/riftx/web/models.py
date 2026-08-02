"""Provider-neutral contracts for public web documents and citations."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from riftx.domain.base import DomainModel, new_id, utc_now


class RedirectPolicy(StrEnum):
    NONE = "none"
    SAME_ORIGIN_AUTO = "same_origin_auto"
    ALL_AUTO = "all_auto"
    RETURN_CROSS_ORIGIN = "return_cross_origin"


class CachePolicy(StrEnum):
    DEFAULT = "default"
    BYPASS = "bypass"
    REFRESH = "refresh"
    NO_STORE = "no_store"


class ExtractionStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    BINARY_ONLY = "binary_only"
    BROWSER_FALLBACK_REQUIRED = "browser_fallback_required"


class WebSourceClass(StrEnum):
    PUBLIC_WEB = "public_web"


class WebDestinationClass(StrEnum):
    PUBLIC_RESEARCH = "public_research"
    TARGET_SCOPE = "target_scope"
    LOCAL_OR_PRIVATE = "local_or_private"
    AUTHENTICATED_EXTERNAL = "authenticated_external"
    UNKNOWN = "unknown"


class SourceType(StrEnum):
    VENDOR_OFFICIAL = "vendor_official"
    STANDARD_OR_DATABASE = "standard_or_database"
    PROJECT_OFFICIAL = "project_official"
    RESEARCH_ORGANIZATION = "research_organization"
    INDIVIDUAL_RESEARCHER = "individual_researcher"
    NEWS = "news"
    FORUM = "forum"
    AGGREGATOR = "aggregator"
    UNKNOWN = "unknown"


class FetchResultStatus(StrEnum):
    FETCHED = "fetched"
    REDIRECT = "redirect"
    BROWSER_FALLBACK_REQUIRED = "browser_fallback_required"


class FetchRequest(DomainModel):
    url: HttpUrl
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    extraction_prompt: str | None = None
    cache_policy: CachePolicy = CachePolicy.DEFAULT
    redirect_policy: RedirectPolicy = RedirectPolicy.SAME_ORIGIN_AUTO
    max_response_bytes: int = Field(default=10_000_000, ge=1, le=100_000_000)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    save_raw: bool = True
    use_browser_fallback: bool = True

    @model_validator(mode="after")
    def validate_public_fetch(self) -> FetchRequest:
        if self.method.upper() != "GET":
            raise ValueError("Public Fetch only supports GET")
        object.__setattr__(self, "method", "GET")
        blocked = {"authorization", "cookie", "host", "proxy-authorization"}
        if blocked.intersection(name.lower() for name in self.headers):
            raise ValueError("Public Fetch does not accept credentials or routing headers")
        return self


class WebDocument(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    requested_url: str = Field(min_length=1)
    final_url: str = Field(min_length=1)
    canonical_url: str | None = None
    title: str | None = None
    author: str | None = None
    site_name: str | None = None
    published_at: AwareDatetime | None = None
    fetched_at: AwareDatetime = Field(default_factory=utc_now)
    mime_type: str = Field(min_length=1)
    language: str | None = None
    raw_artifact_id: str | None = None
    normalized_artifact_id: str | None = None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_length: int = Field(ge=0)
    extraction_status: ExtractionStatus
    truncated: bool = False
    source_class: WebSourceClass = WebSourceClass.PUBLIC_WEB
    destination_class: WebDestinationClass = WebDestinationClass.PUBLIC_RESEARCH


class WebDocumentChunk(DomainModel):
    id: str = Field(default_factory=new_id)
    document_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    heading_path: list[str] = Field(default_factory=list)
    content: str = Field(min_length=1)
    token_count: int = Field(ge=1)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)
    embedding: list[float] | None = None

    @model_validator(mode="after")
    def validate_offsets(self) -> WebDocumentChunk:
        if self.end_offset <= self.start_offset:
            raise ValueError("Document Chunk end offset must follow its start")
        return self


class SourceReference(DomainModel):
    id: str = Field(default_factory=new_id)
    document_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    title: str | None = None
    domain: str = Field(min_length=1)
    author: str | None = None
    published_at: AwareDatetime | None = None
    fetched_at: AwareDatetime
    source_type: SourceType = SourceType.UNKNOWN
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvidenceSpan(DomainModel):
    source_id: str = Field(min_length=1)
    chunk_id: str | None = None
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=1)
    quote: str | None = None
    paraphrase: str | None = None
    section_title: str | None = None

    @model_validator(mode="after")
    def validate_span(self) -> EvidenceSpan:
        if self.quote is None and self.paraphrase is None:
            raise ValueError("Evidence Span requires a quote or paraphrase")
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("Evidence Span offsets must be supplied together")
        if self.start_offset is not None and self.end_offset <= self.start_offset:
            raise ValueError("Evidence Span end offset must follow its start")
        return self


class FetchResult(DomainModel):
    status: FetchResultStatus
    requested_url: str
    final_url: str | None = None
    redirect_url: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    reason: str | None = None
    raw_artifact_id: str | None = None
    document: WebDocument | None = None
    chunks: list[WebDocumentChunk] = Field(default_factory=list)
    source: SourceReference | None = None
    cache_hit: bool = False

    @model_validator(mode="after")
    def validate_result(self) -> FetchResult:
        canonical = self.document is not None and self.source is not None
        if self.status is FetchResultStatus.FETCHED and not canonical:
            raise ValueError("Fetched result requires a canonical Document and Source")
        if self.status is not FetchResultStatus.FETCHED and canonical:
            raise ValueError("Only fetched documents can become formal Sources")
        if self.status is FetchResultStatus.REDIRECT and self.redirect_url is None:
            raise ValueError("Redirect result requires a destination URL")
        return self
