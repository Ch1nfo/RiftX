"""Canonical public-web acquisition and source registry."""

from .fetch import (
    ApplicationWebArtifactStore,
    PublicDestinationError,
    PublicWebFetcher,
    WebFetchError,
)
from .models import (
    CachePolicy,
    EvidenceSpan,
    ExtractionStatus,
    FetchRequest,
    FetchResult,
    FetchResultStatus,
    RedirectPolicy,
    SourceReference,
    SourceType,
    WebDestinationClass,
    WebDocument,
    WebDocumentChunk,
    WebSourceClass,
)
from .repository import WebSourceRepository

__all__ = [
    "ApplicationWebArtifactStore",
    "CachePolicy",
    "EvidenceSpan",
    "ExtractionStatus",
    "FetchRequest",
    "FetchResult",
    "FetchResultStatus",
    "PublicDestinationError",
    "PublicWebFetcher",
    "RedirectPolicy",
    "SourceReference",
    "SourceType",
    "WebDestinationClass",
    "WebDocument",
    "WebDocumentChunk",
    "WebFetchError",
    "WebSourceClass",
    "WebSourceRepository",
]
