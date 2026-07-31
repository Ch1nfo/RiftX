from __future__ import annotations

from collections.abc import Iterator, Mapping
from unittest.mock import AsyncMock

import httpx
import pytest
from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel
from openai import APITimeoutError

from riftx.models import (
    ModelAPI,
    ModelConfigurationError,
    ModelFailureCategory,
    ModelProfile,
    ModelsConfig,
    RiftXModelProvider,
    classify_model_failure,
)


def _config() -> ModelsConfig:
    return ModelsConfig(
        default_profile="primary",
        models={
            "primary": ModelProfile(
                model="primary-model",
                api=ModelAPI.RESPONSES,
                base_url="${LOCAL_MODEL_URL}/v1",
                api_key_env="PRIMARY_KEY",
            ),
            "fast": ModelProfile(
                model="fast-model",
                api=ModelAPI.CHAT_COMPLETIONS,
                base_url="http://localhost:9000/v1",
                requires_api_key=False,
                api_key_env=None,
            ),
        },
    )


def test_provider_resolves_profiles_and_caches_models() -> None:
    provider = RiftXModelProvider(
        _config(),
        environment={
            "LOCAL_MODEL_URL": "http://localhost:8000",
            "PRIMARY_KEY": "secret",
        },
    )

    primary = provider.get_model(None)
    fast = provider.get_model("fast")

    assert isinstance(primary, OpenAIResponsesModel)
    assert isinstance(fast, OpenAIChatCompletionsModel)
    assert provider.get_model("primary") is primary
    assert provider.get_model("fast") is fast
    assert provider._clients["primary"].base_url == "http://localhost:8000/v1/"
    assert provider._clients["primary"].api_key == "secret"
    assert provider._clients["fast"].api_key == "not-required"


@pytest.mark.parametrize("profile_name", ["missing", "report"])
def test_provider_rejects_unknown_profile(profile_name: str) -> None:
    provider = RiftXModelProvider(_config(), environment={})

    with pytest.raises(ModelConfigurationError, match=profile_name):
        provider.get_model(profile_name)


def test_provider_requires_configured_api_key() -> None:
    provider = RiftXModelProvider(
        _config(),
        environment={"LOCAL_MODEL_URL": "http://localhost:8000"},
    )

    with pytest.raises(ModelConfigurationError, match="PRIMARY_KEY"):
        provider.get_model("primary")


class _UnreadableCredentialEnvironment(Mapping[str, str]):
    def __getitem__(self, key: str) -> str:
        raise AssertionError(f"credential environment was read: {key}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("credential environment was iterated")

    def __len__(self) -> int:
        raise AssertionError("credential environment length was read")

    def get(self, key: str, default: str | None = None) -> str | None:
        raise AssertionError(f"credential environment was read: {key}")


def test_provider_never_reads_credentials_for_no_key_profile() -> None:
    config = ModelsConfig(
        default_profile="local",
        models={
            "local": ModelProfile(
                model="local-model",
                base_url="http://localhost:11434/v1",
                api_key_env="RIFTX_MODEL_REAL_KEY",
                requires_api_key=False,
            )
        },
    )
    provider = RiftXModelProvider(config, environment=_UnreadableCredentialEnvironment())

    provider.get_model("local")

    assert provider._clients["local"].api_key == "not-required"


def test_provider_rejects_missing_environment_reference() -> None:
    provider = RiftXModelProvider(
        _config(),
        environment={"PRIMARY_KEY": "secret"},
    )

    with pytest.raises(ModelConfigurationError, match="LOCAL_MODEL_URL"):
        provider.get_model("primary")


async def test_provider_closes_cached_clients() -> None:
    provider = RiftXModelProvider(
        _config(),
        environment={
            "LOCAL_MODEL_URL": "http://localhost:8000",
            "PRIMARY_KEY": "secret",
        },
    )
    provider.get_model("primary")
    client = provider._clients["primary"]
    close = AsyncMock()
    client.close = close

    await provider.aclose()

    close.assert_awaited_once_with()
    assert provider._clients == {}
    assert provider._models == {}


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize(
    ("error", "category", "retryable"),
    [
        (
            APITimeoutError(request=httpx.Request("GET", "http://localhost")),
            ModelFailureCategory.TIMEOUT,
            True,
        ),
        (_StatusError("rate limited", 429), ModelFailureCategory.RATE_LIMIT, True),
        (_StatusError("upstream failed", 503), ModelFailureCategory.TEMPORARY_SERVER, True),
        (_StatusError("unauthorized", 401), ModelFailureCategory.AUTHENTICATION, False),
        (
            _StatusError("maximum context length exceeded", 400),
            ModelFailureCategory.CONTEXT_EXCEEDED,
            False,
        ),
        (
            _StatusError("unsupported tool schema", 400),
            ModelFailureCategory.UNSUPPORTED_TOOL_SCHEMA,
            False,
        ),
        (
            _StatusError("model does not exist", 404),
            ModelFailureCategory.INVALID_MODEL,
            False,
        ),
        (_StatusError("bad input", 400), ModelFailureCategory.INVALID_REQUEST, False),
        (RuntimeError("other"), ModelFailureCategory.UNKNOWN, False),
    ],
)
def test_classify_model_failure(
    error: BaseException,
    category: ModelFailureCategory,
    retryable: bool,
) -> None:
    failure = classify_model_failure(error)

    assert failure.category is category
    assert failure.retryable is retryable
