from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest

from riftx.application.errors import ApplicationConflictError, ServiceUnavailableError
from riftx.config import WebSearchConfig
from riftx.domain import Objective, Run, RunKind, RunStatus
from riftx.domain.base import utc_now
from riftx.models import ModelProfile, ModelProviderKind, ModelsConfig, RiftXModelProvider
from riftx.web import (
    ArtifactBackedResearchRecorder,
    ConfiguredSearchProviderResolver,
    FetchRequest,
    FetchResult,
    FetchResultStatus,
    ResearchRequest,
    ResolvedSearchProviders,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SourceReference,
    WebDocument,
    WebDocumentChunk,
    WebResearchApplicationService,
    WebResearchPacket,
)


class FakeRuns:
    def __init__(self, run: Run) -> None:
        self.run = run
        self.calls = 0

    async def get(self, run_id: str) -> Run | None:
        self.calls += 1
        return self.run if self.run.id == run_id else None


class StaticProvider:
    id = "static"

    def __init__(self) -> None:
        self.calls: list[SearchRequest] = []

    async def search(self, request: SearchRequest) -> SearchResponse:
        self.calls.append(request)
        query_id = f"query-{len(self.calls)}"
        return SearchResponse(
            query_id=query_id,
            provider=self.id,
            request=request,
            results=[
                SearchResult(
                    title="Vendor advisory",
                    url="https://vendor.example/advisory",
                    normalized_url="https://vendor.example/advisory",
                    domain="vendor.example",
                    snippet="Patched release information",
                    provider=self.id,
                    provider_rank=1,
                    search_query_id=query_id,
                )
            ],
        )


@dataclass
class StaticResolver:
    provider: StaticProvider

    def resolve(self, model_profile: str) -> ResolvedSearchProviders:
        assert model_profile == "profile-1"
        return ResolvedSearchProviders((self.provider,))


class FakeRecorder:
    def __init__(self) -> None:
        self.searches: list[tuple[str, str, SearchResponse]] = []
        self.notes: list[object] = []
        self.packets: list[WebResearchPacket] = []

    async def record_search(
        self,
        run_id: str,
        session_id: str,
        response: SearchResponse,
    ) -> None:
        response.artifact_id = f"search-artifact-{len(self.searches) + 1}"
        self.searches.append((run_id, session_id, response))

    async def record_note(self, run_id: str, note: object) -> None:
        self.notes.append((run_id, note))

    async def record_packet(self, packet: WebResearchPacket) -> None:
        packet.artifact_ids = [*packet.artifact_ids, "packet-artifact-1"]
        self.packets.append(packet)


class FakeFetcher:
    async def fetch(self, run_id: str, request: FetchRequest) -> FetchResult:
        content = "The vendor fixed the issue in version 2.0."
        digest = hashlib.sha256(content.encode()).hexdigest()
        document = WebDocument(
            id="document-1",
            run_id=run_id,
            requested_url=str(request.url),
            final_url=str(request.url),
            fetched_at=utc_now(),
            mime_type="text/plain",
            raw_artifact_id="raw-1",
            normalized_artifact_id="normalized-1",
            content_hash=digest,
            text_length=len(content),
            extraction_status="complete",
        )
        source = SourceReference(
            id="source-1",
            document_id=document.id,
            url=document.final_url,
            domain="vendor.example",
            fetched_at=document.fetched_at,
            content_hash=digest,
        )
        return FetchResult(
            status=FetchResultStatus.FETCHED,
            requested_url=document.requested_url,
            final_url=document.final_url,
            document=document,
            source=source,
            chunks=[
                WebDocumentChunk(
                    id="chunk-1",
                    document_id=document.id,
                    sequence=0,
                    content=content,
                    token_count=12,
                    start_offset=0,
                    end_offset=len(content),
                )
            ],
        )


def run(
    *,
    status: RunStatus = RunStatus.RUNNING,
    kind: RunKind = RunKind.GENERAL,
) -> Run:
    return Run(
        id="run-1",
        engagement_id="engagement-1",
        node_id="local",
        objective=Objective(description="Research public evidence"),
        kind=kind,
        status=status,
        model_profile="profile-1",
        workspace_path="/workspace",
    )


@pytest.mark.parametrize("kind", [RunKind.GENERAL, RunKind.CODE_AUDIT])
async def test_application_search_is_run_guarded_recorded_and_untrusted(
    kind: RunKind,
) -> None:
    provider = StaticProvider()
    recorder = FakeRecorder()
    runs = FakeRuns(run(kind=kind))
    service = WebResearchApplicationService(
        runs=runs,
        providers=StaticResolver(provider),  # type: ignore[arg-type]
        fetcher=FakeFetcher(),  # type: ignore[arg-type]
        recorder=recorder,  # type: ignore[arg-type]
    )

    response = await service.search(
        "run-1",
        "session-1",
        "profile-1",
        SearchRequest(query="vendor fix"),
    )

    assert runs.calls == 2
    assert len(provider.calls) == 1
    assert response.artifact_id == "search-artifact-1"
    assert response.content_trust == "UNTRUSTED_EXTERNAL_CONTENT"
    assert recorder.searches[0][:2] == ("run-1", "session-1")


async def test_application_search_rechecks_run_before_each_provider_effect() -> None:
    provider = StaticProvider()
    runs = FakeRuns(run(status=RunStatus.PAUSED))
    service = WebResearchApplicationService(
        runs=runs,
        providers=StaticResolver(provider),  # type: ignore[arg-type]
        fetcher=FakeFetcher(),  # type: ignore[arg-type]
        recorder=FakeRecorder(),  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.search(
            "run-1",
            "session-1",
            "profile-1",
            SearchRequest(query="must not leave the worker"),
        )

    assert captured.value.code == "run_web_research_blocked"
    assert provider.calls == []


@pytest.mark.parametrize("kind", [RunKind.GENERAL, RunKind.CODE_AUDIT])
async def test_application_research_promotes_only_fetched_canonical_sources(
    kind: RunKind,
) -> None:
    provider = StaticProvider()
    recorder = FakeRecorder()
    service = WebResearchApplicationService(
        runs=FakeRuns(run(kind=kind)),
        providers=StaticResolver(provider),  # type: ignore[arg-type]
        fetcher=FakeFetcher(),  # type: ignore[arg-type]
        recorder=recorder,  # type: ignore[arg-type]
    )

    packet = await service.research(
        ResearchRequest(
            run_id="run-1",
            session_id="session-1",
            question="Which release fixed the issue?",
            max_queries=1,
            max_sources=1,
        ),
        model_profile="profile-1",
    )

    assert [source.id for source in packet.sources] == ["source-1"]
    assert packet.document_ids == ["document-1"]
    assert {"raw-1", "normalized-1", "packet-artifact-1"} <= set(packet.artifact_ids)
    assert packet.key_claims[0].evidence[0].source_id == "source-1"
    assert recorder.packets == [packet]


class FakeArtifactStore:
    def __init__(self) -> None:
        self.saved: list[tuple[str, dict[str, object]]] = []

    async def save(self, run_id: str, **kwargs: object) -> str:
        self.saved.append((run_id, kwargs))
        return f"artifact-{len(self.saved)}"


class FakeRepository:
    def __init__(self) -> None:
        self.search: SearchResponse | None = None
        self.packet: WebResearchPacket | None = None

    async def record_search(
        self,
        run_id: str,
        session_id: str,
        response: SearchResponse,
    ) -> None:
        assert (run_id, session_id) == ("run-1", "session-1")
        self.search = response

    async def record_note(self, run_id: str, note: object) -> None:
        del run_id, note

    async def record_packet(self, packet: WebResearchPacket) -> None:
        self.packet = packet


async def test_artifact_backed_recorder_links_search_and_packet_artifacts() -> None:
    repository = FakeRepository()
    artifacts = FakeArtifactStore()
    recorder = ArtifactBackedResearchRecorder(
        repository,  # type: ignore[arg-type]
        artifacts,  # type: ignore[arg-type]
    )
    search = await StaticProvider().search(SearchRequest(query="artifact evidence"))
    packet = WebResearchPacket(
        run_id="run-1",
        session_id="session-1",
        question="artifact evidence",
        summary="No canonical claim in this fixture.",
    )

    await recorder.record_search("run-1", "session-1", search)
    await recorder.record_packet(packet)

    assert search.artifact_id == "artifact-1"
    assert packet.artifact_ids == ["artifact-2"]
    assert repository.search is search
    assert repository.packet is packet
    assert [item[1]["name"] for item in artifacts.saved] == [
        f"web-search-{search.query_id}.json",
        f"web-research-{packet.id}.json",
    ]


def _model_provider(profile: ModelProfile, environment: dict[str, str]) -> RiftXModelProvider:
    return RiftXModelProvider(
        ModelsConfig(default_profile="profile-1", models={"profile-1": profile}),
        environment=environment,
    )


def test_configured_provider_resolver_combines_searxng_and_official_openai() -> None:
    model_provider = _model_provider(
        ModelProfile(
            provider=ModelProviderKind.OPENAI,
            model="gpt-search",
            api_key_env="OPENAI_KEY",
        ),
        {"OPENAI_KEY": "secret"},
    )
    resolver = ConfiguredSearchProviderResolver(
        WebSearchConfig(
            enabled=True,
            providers=("searxng", "openai_hosted"),
            searxng_endpoint="https://search.example.test",
        ),
        model_provider,
    )

    resolved = resolver.resolve("profile-1")

    assert [provider.id for provider in resolved.providers] == [
        "searxng",
        "openai_hosted_search",
    ]
    assert resolved.warnings == ()


def test_configured_provider_resolver_falls_back_without_granting_hosted_capability() -> None:
    model_provider = _model_provider(
        ModelProfile(
            provider=ModelProviderKind.OPENAI_COMPATIBLE,
            model="local",
            base_url="http://127.0.0.1:8000/v1",
            requires_api_key=False,
            api_key_env=None,
        ),
        {},
    )
    resolver = ConfiguredSearchProviderResolver(
        WebSearchConfig(
            enabled=True,
            providers=("openai_hosted", "searxng"),
            searxng_endpoint="https://search.example.test",
        ),
        model_provider,
    )

    resolved = resolver.resolve("profile-1")

    assert [provider.id for provider in resolved.providers] == ["searxng"]
    assert "not eligible" in resolved.warnings[0]
    assert model_provider._clients == {}


def test_configured_provider_resolver_reports_no_available_provider() -> None:
    resolver = ConfiguredSearchProviderResolver(
        WebSearchConfig(enabled=True, providers=("openai_hosted",)),
        _model_provider(
            ModelProfile(
                provider=ModelProviderKind.OPENAI_COMPATIBLE,
                model="local",
                base_url="http://127.0.0.1:8000/v1",
                requires_api_key=False,
                api_key_env=None,
            ),
            {},
        ),
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        resolver.resolve("profile-1")

    assert captured.value.code == "web_search_unavailable"
