from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services.models import ModelProfileApplicationService
from riftx.models import ModelProfile, ModelProfileRegistry, ModelsConfig


class _UnusedUsageRepository:
    async def has_nonterminal_model_profile(self, profile_name: str) -> bool:
        return False


class _UnreadableCredentialEnvironment(Mapping[str, str]):
    def __getitem__(self, key: str) -> str:
        raise AssertionError(f"credential environment was read: {key}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("credential environment was iterated")

    def __len__(self) -> int:
        raise AssertionError("credential environment length was read")

    def get(self, key: str, default: str | None = None) -> str | None:
        raise AssertionError(f"credential environment was read: {key}")


@pytest.mark.asyncio
async def test_service_never_queries_credentials_for_no_key_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ModelProfile(
        model="local-model",
        base_url="http://127.0.0.1:11434/v1",
        api_key_env="RIFTX_MODEL_REAL_KEY",
        requires_api_key=False,
    )
    registry = ModelProfileRegistry(
        tmp_path / "models.yaml",
        tmp_path / "secrets" / "models.json",
        initial_config=ModelsConfig(default_profile="local", models={"local": profile}),
    )
    registry.refresh()

    def fail(*_: object, **__: object) -> None:
        raise AssertionError("Secret Store credential state was queried")

    monkeypatch.setattr(registry, "has_stored_api_key", fail)
    service = ModelProfileApplicationService(
        registry,
        run_repository=_UnusedUsageRepository(),
        session_repository=_UnusedUsageRepository(),
        environment=_UnreadableCredentialEnvironment(),
    )

    profiles = await service.list_profiles()
    selected = await service.resolve_profile(None)

    assert selected == "local"
    assert profiles.profiles[0].api_key_configured is True
    assert profiles.profiles[0].has_stored_api_key is False


@pytest.mark.asyncio
async def test_effective_default_endpoint_change_requires_resubmitting_stored_key(
    tmp_path: Path,
) -> None:
    profile = ModelProfile(
        model="remote-model",
        base_url="https://models.example/v1",
        api_key_env=None,
    )
    registry = ModelProfileRegistry(
        tmp_path / "models.yaml",
        tmp_path / "secrets" / "models.json",
        initial_config=ModelsConfig(default_profile="primary", models={"primary": profile}),
    )
    registry.refresh()
    registry.upsert("primary", profile, api_key="stored-secret")
    service = ModelProfileApplicationService(
        registry,
        run_repository=_UnusedUsageRepository(),
        session_repository=_UnusedUsageRepository(),
        environment={},
    )

    with pytest.raises(ApplicationConflictError, match="requires an API credential"):
        await service.upsert_profile(
            "primary",
            profile.model_copy(update={"base_url": "https://capture.example/v1"}),
        )

    assert registry.get("primary").base_url == "https://models.example/v1"
    assert registry.api_key("primary", profile) == "stored-secret"
