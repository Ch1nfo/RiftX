"""Run-scoped production Web Search and Research application service."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    ServiceUnavailableError,
)
from riftx.application.run_kind_effects import (
    EffectMode,
    EffectOrigin,
    OperationEffect,
    RunEffectOperation,
)
from riftx.application.services.runs import (
    require_run_kind_effect_operation,
)
from riftx.config import WebSearchConfig
from riftx.domain import Run, RunStatus
from riftx.domain.base import DomainModel
from riftx.models import ModelConfigurationError, RiftXModelProvider

from .fetch import WebArtifactStore
from .research import (
    ResearchRequest,
    WebFetcher,
    WebResearchNote,
    WebResearchPacket,
    WebResearchPipeline,
)
from .search import (
    FederatedSearchProvider,
    OpenAIHostedSearchProvider,
    SearchProvider,
    SearchRequest,
    SearchResponse,
    SearXNGSearchProvider,
)

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


class RunRepository(Protocol):
    async def get(self, run_id: str) -> Run | None: ...


class WebResearchRepository(Protocol):
    async def record_search(
        self,
        run_id: str,
        session_id: str,
        response: SearchResponse,
    ) -> None: ...

    async def record_note(self, run_id: str, note: WebResearchNote) -> None: ...

    async def record_packet(self, packet: WebResearchPacket) -> None: ...


class SearchProviderResolver(Protocol):
    def resolve(self, model_profile: str) -> ResolvedSearchProviders: ...


class ResearchRecorder(Protocol):
    async def record_search(
        self,
        run_id: str,
        session_id: str,
        response: SearchResponse,
    ) -> None: ...

    async def record_note(self, run_id: str, note: WebResearchNote) -> None: ...

    async def record_packet(self, packet: WebResearchPacket) -> None: ...


@dataclass(frozen=True, slots=True)
class ResolvedSearchProviders:
    providers: tuple[SearchProvider, ...]
    warnings: tuple[str, ...] = ()


class ConfiguredSearchProviderResolver:
    """Resolve operator-configured providers for the active Run model profile."""

    def __init__(
        self,
        config: WebSearchConfig,
        model_provider: RiftXModelProvider,
    ) -> None:
        self._config = config
        self._model_provider = model_provider

    def resolve(self, model_profile: str) -> ResolvedSearchProviders:
        if not self._config.enabled:
            raise ServiceUnavailableError(
                "web_search_disabled",
                "Web Search is disabled in this Worker configuration",
            )
        providers: list[SearchProvider] = []
        warnings: list[str] = []
        for configured in self._config.providers:
            if configured == "searxng":
                endpoint = self._config.searxng_endpoint
                if endpoint is None:
                    # Config validation normally makes this unreachable. Keep the
                    # runtime boundary fail-closed if a synthetic config bypasses it.
                    warnings.append("configured SearXNG provider has no endpoint")
                    continue
                providers.append(
                    SearXNGSearchProvider(
                        endpoint,
                        timeout_seconds=self._config.timeout_seconds,
                    )
                )
                continue
            try:
                binding = self._model_provider.get_openai_hosted_search(model_profile)
            except ModelConfigurationError as exc:
                warnings.append(
                    f"OpenAI hosted search is unavailable for model profile "
                    f"{model_profile!r}: {exc}"
                )
                continue
            providers.append(
                OpenAIHostedSearchProvider(
                    binding.client,
                    model=binding.model,
                    timeout_seconds=self._config.timeout_seconds,
                )
            )
        if not providers:
            raise ServiceUnavailableError(
                "web_search_unavailable",
                "No configured Web Search provider is available for the active model profile",
                details={
                    "model_profile": model_profile,
                    "configured_providers": list(self._config.providers),
                    "warnings": warnings,
                },
            )
        return ResolvedSearchProviders(tuple(providers), tuple(warnings))


class ArtifactBackedResearchRecorder:
    """Persist normalized records and immutable JSON evidence for web operations."""

    def __init__(
        self,
        repository: WebResearchRepository,
        artifacts: WebArtifactStore,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts

    async def record_search(
        self,
        run_id: str,
        session_id: str,
        response: SearchResponse,
    ) -> None:
        artifact_id = await self._artifacts.save(
            run_id,
            name=f"web-search-{response.query_id}.json",
            mime_type="application/json",
            content=_json_bytes(response),
            description="Normalized untrusted Web Search candidates",
        )
        response.artifact_id = artifact_id
        await self._repository.record_search(run_id, session_id, response)

    async def record_note(self, run_id: str, note: WebResearchNote) -> None:
        await self._repository.record_note(run_id, note)

    async def record_packet(self, packet: WebResearchPacket) -> None:
        artifact_id = await self._artifacts.save(
            packet.run_id,
            name=f"web-research-{packet.id}.json",
            mime_type="application/json",
            content=_json_bytes(packet),
            description="Citation-safe untrusted Web Research packet",
        )
        packet.artifact_ids = list(dict.fromkeys([*packet.artifact_ids, artifact_id]))
        await self._repository.record_packet(packet)


class WebResearchApplicationService:
    """Authorize, execute, record, and bound public Web Search/Research."""

    def __init__(
        self,
        *,
        runs: RunRepository,
        providers: SearchProviderResolver,
        fetcher: WebFetcher,
        recorder: ResearchRecorder,
    ) -> None:
        self._runs = runs
        self._providers = providers
        self._fetcher = fetcher
        self._recorder = recorder

    async def search(
        self,
        run_id: str,
        session_id: str,
        model_profile: str,
        request: SearchRequest,
    ) -> SearchResponse:
        await self._require_effects_allowed(
            run_id,
            operation=RunEffectOperation.SERVICE_WEB_SEARCH,
        )
        provider = self._federated_provider(run_id, model_profile)
        response = await provider.search(request)
        await self._recorder.record_search(run_id, session_id, response)
        return response

    async def research(
        self,
        request: ResearchRequest,
        *,
        model_profile: str,
    ) -> WebResearchPacket:
        await self._require_effects_allowed(
            request.run_id,
            operation=RunEffectOperation.SERVICE_WEB_RESEARCH,
        )
        pipeline = WebResearchPipeline(
            providers=[self._federated_provider(request.run_id, model_profile)],
            fetcher=self._fetcher,
            recorder=self._recorder,
        )
        return await pipeline.research(request)

    def _federated_provider(
        self,
        run_id: str,
        model_profile: str,
    ) -> FederatedSearchProvider:
        resolved = self._providers.resolve(model_profile)
        return FederatedSearchProvider(
            [
                _RunGuardedSearchProvider(run_id, provider, self._require_effects_allowed)
                for provider in resolved.providers
            ],
            warnings=list(resolved.warnings),
        )

    async def _require_effects_allowed(
        self,
        run_id: str,
        *,
        operation: RunEffectOperation = RunEffectOperation.SERVICE_WEB_SEARCH,
    ) -> Run:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        require_run_kind_effect_operation(
            run,
            operation=operation,
            origin=EffectOrigin.APPLICATION_SERVICE,
            effect=OperationEffect.HOST_EXECUTION,
            mode=EffectMode.NORMAL,
        )
        if run.status in _WEB_EFFECT_BLOCKED_RUN_STATUSES:
            raise ApplicationConflictError(
                "run_web_research_blocked",
                f"Run {run.id!r} cannot perform Web Search or Research while it is "
                f"{run.status.value}",
                details={"run_id": run.id, "status": run.status.value},
            )
        return run


class _RunGuardedSearchProvider:
    def __init__(
        self,
        run_id: str,
        provider: SearchProvider,
        guard: Callable[..., Awaitable[Run]],
    ) -> None:
        self.id = provider.id
        self._run_id = run_id
        self._provider = provider
        self._guard = guard

    async def search(self, request: SearchRequest) -> SearchResponse:
        await self._guard(
            self._run_id,
            operation=RunEffectOperation.SERVICE_WEB_SEARCH,
        )
        return await self._provider.search(request)


def _json_bytes(value: DomainModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
