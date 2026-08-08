"""OpenAI and OpenAI-compatible model provider for the Agents SDK."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agents import (
    Model,
    ModelProvider,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
)
from openai import APITimeoutError, AsyncOpenAI

from .config import ModelAPI, ModelProfile, ModelProviderKind, ModelsConfig
from .registry import ModelProfileRegistry

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ModelFailureCategory(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    TEMPORARY_SERVER = "temporary_server"
    INVALID_MODEL = "invalid_model"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_EXCEEDED = "context_exceeded"
    UNSUPPORTED_TOOL_SCHEMA = "unsupported_tool_schema"
    AUTHENTICATION = "authentication"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ModelFailure:
    category: ModelFailureCategory
    retryable: bool
    status_code: int | None = None


@dataclass(frozen=True, slots=True)
class OpenAIHostedSearchBinding:
    client: AsyncOpenAI
    model: str
    profile_name: str


class ModelConfigurationError(ValueError):
    pass


class RiftXModelProvider(ModelProvider):
    """Resolve logical model profiles without exposing credentials to business code."""

    def __init__(
        self,
        config: ModelsConfig | ModelProfileRegistry,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._registry: ModelProfileRegistry | None
        self.config: ModelsConfig
        if isinstance(config, ModelProfileRegistry):
            self._registry = config
            self.config = config.snapshot.config
        else:
            self._registry = None
            self.config = config
        # Keep the environment lazy. In particular, a profile that explicitly
        # disables credentials must never cause an eager copy/read of real keys.
        self._environment = os.environ if environment is None else environment
        self._models: dict[str, Model] = {}
        self._clients: dict[str, AsyncOpenAI] = {}
        self._stale_clients: list[AsyncOpenAI] = []
        self._registry_generation = (
            self._registry.snapshot.generation if self._registry is not None else None
        )

    def get_model(self, model_name: str | None) -> Model:
        self._reload_if_changed()
        profile_name = model_name or self.config.default_profile
        if profile_name in self._models:
            return self._models[profile_name]
        try:
            profile = self.config.models[profile_name]
        except KeyError as exc:
            raise ModelConfigurationError(
                f"model profile {profile_name!r} is not configured"
            ) from exc

        client = self._clients.get(profile_name)
        if client is None:
            client = self._build_client(profile_name, profile)
        if profile.api is ModelAPI.RESPONSES:
            model: Model = OpenAIResponsesModel(
                model=profile.model,
                openai_client=client,
            )
        else:
            model = OpenAIChatCompletionsModel(
                model=profile.model,
                openai_client=client,
            )
        self._clients[profile_name] = client
        self._models[profile_name] = model
        return model

    def get_openai_hosted_search(
        self,
        model_name: str | None,
    ) -> OpenAIHostedSearchBinding:
        """Resolve one official OpenAI profile for the hosted web-search tool.

        Provider labels and destinations are checked independently. An
        OpenAI-compatible endpoint, or even an ``openai`` profile with a custom
        base URL, must never inherit capabilities that only the official API
        contract provides.
        """

        self._reload_if_changed()
        profile_name = model_name or self.config.default_profile
        try:
            profile = self.config.models[profile_name]
        except KeyError as exc:
            raise ModelConfigurationError(
                f"model profile {profile_name!r} is not configured"
            ) from exc
        if profile.provider is not ModelProviderKind.OPENAI or profile.base_url is not None:
            raise ModelConfigurationError(
                f"model profile {profile_name!r} is not eligible for OpenAI hosted search"
            )
        client = self._clients.get(profile_name)
        if client is None:
            client = self._build_client(profile_name, profile)
            self._clients[profile_name] = client
        return OpenAIHostedSearchBinding(
            client=client,
            model=profile.model,
            profile_name=profile_name,
        )

    async def aclose(self) -> None:
        clients = [*self._clients.values(), *self._stale_clients]
        self._clients.clear()
        self._stale_clients.clear()
        self._models.clear()
        for client in clients:
            await client.close()

    def _build_client(self, profile_name: str, profile: ModelProfile) -> AsyncOpenAI:
        api_key: str
        if not profile.requires_api_key:
            # AsyncOpenAI requires a non-empty client value even for local endpoints,
            # but this sentinel is never sourced from the environment or Secret Store.
            api_key = "not-required"
        elif profile.api_key_env:
            configured = self._environment.get(profile.api_key_env)
        else:
            configured = None
        if profile.requires_api_key:
            # Bind the credential lookup to the exact metadata snapshot already
            # selected above. A concurrent update therefore fails closed instead of
            # combining an old endpoint with a newly written credential.
            if configured:
                api_key = configured
            else:
                stored = (
                    self._registry.api_key(profile_name, profile)
                    if self._registry is not None
                    else None
                )
                if stored:
                    api_key = stored
                else:
                    raise ModelConfigurationError(
                        f"model profile {profile_name!r} requires environment variable "
                        f"{profile.api_key_env or '<unspecified>'} or a stored local API key"
                    )

        base_url = _expand_environment(profile.base_url, self._environment)
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=profile.timeout_seconds,
            max_retries=profile.max_retries,
        )

    def _reload_if_changed(self) -> None:
        if self._registry is None:
            return
        snapshot = self._registry.reload_if_changed()
        if snapshot.generation == self._registry_generation:
            return
        self._stale_clients.extend(self._clients.values())
        self._clients.clear()
        self._models.clear()
        self.config = snapshot.config
        self._registry_generation = snapshot.generation


def classify_model_failure(error: BaseException) -> ModelFailure:
    status_code = _status_code(error)
    message = str(error).lower()
    if isinstance(error, (TimeoutError, APITimeoutError)):
        return ModelFailure(ModelFailureCategory.TIMEOUT, retryable=True, status_code=status_code)
    if status_code == 429:
        return ModelFailure(
            ModelFailureCategory.RATE_LIMIT, retryable=True, status_code=status_code
        )
    if status_code is not None and status_code >= 500:
        return ModelFailure(
            ModelFailureCategory.TEMPORARY_SERVER,
            retryable=True,
            status_code=status_code,
        )
    if status_code in {401, 403}:
        return ModelFailure(
            ModelFailureCategory.AUTHENTICATION,
            retryable=False,
            status_code=status_code,
        )
    if "context" in message and any(
        word in message for word in ("length", "window", "token", "exceed")
    ):
        return ModelFailure(
            ModelFailureCategory.CONTEXT_EXCEEDED,
            retryable=False,
            status_code=status_code,
        )
    if "tool" in message and "schema" in message:
        return ModelFailure(
            ModelFailureCategory.UNSUPPORTED_TOOL_SCHEMA,
            retryable=False,
            status_code=status_code,
        )
    if "model" in message and any(
        word in message for word in ("invalid", "not found", "does not exist")
    ):
        return ModelFailure(
            ModelFailureCategory.INVALID_MODEL,
            retryable=False,
            status_code=status_code,
        )
    if status_code is not None and 400 <= status_code < 500:
        return ModelFailure(
            ModelFailureCategory.INVALID_REQUEST,
            retryable=False,
            status_code=status_code,
        )
    return ModelFailure(
        ModelFailureCategory.UNKNOWN,
        retryable=False,
        status_code=status_code,
    )


def _expand_environment(value: str | None, environment: Mapping[str, str]) -> str | None:
    if value is None:
        return None

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return environment[name]
        except KeyError as exc:
            raise ModelConfigurationError(
                f"environment variable {name!r} referenced by model config is not set"
            ) from exc

    return _ENV_PATTERN.sub(replace, value)


def _status_code(error: BaseException) -> int | None:
    value: Any = getattr(error, "status_code", None)
    return value if isinstance(value, int) else None
