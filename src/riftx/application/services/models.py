"""Application service for model-profile configuration and credential metadata."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    ServiceUnavailableError,
)
from riftx.models import (
    ModelAPI,
    ModelProfile,
    ModelProfileNotFoundError,
    ModelProfileRegistry,
    ModelProviderKind,
    ModelRegistryConflictError,
    same_credential_destination,
)


@dataclass(frozen=True, slots=True)
class ModelProfileView:
    name: str
    provider: ModelProviderKind
    model: str
    request_mode: ModelAPI
    base_url: str | None
    api_key_env: str | None
    requires_api_key: bool
    timeout_seconds: float
    max_retries: int
    has_stored_api_key: bool
    api_key_configured: bool
    is_default: bool
    is_effective_default: bool


@dataclass(frozen=True, slots=True)
class ModelProfilesView:
    generation: int
    source_digest: str
    default_profile: str
    effective_default_profile: str
    profile_override: str | None
    profiles: list[ModelProfileView]


class ModelProfileRunUsageRepository(Protocol):
    async def has_nonterminal_model_profile(self, profile_name: str) -> bool: ...


class ModelProfileSessionUsageRepository(Protocol):
    async def has_nonterminal_model_profile(self, profile_name: str) -> bool: ...


class ModelProfileApplicationService:
    def __init__(
        self,
        registry: ModelProfileRegistry,
        *,
        run_repository: ModelProfileRunUsageRepository,
        session_repository: ModelProfileSessionUsageRepository,
        profile_override: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._runs = run_repository
        self._sessions = session_repository
        self._profile_override = profile_override.strip() if profile_override else None
        self._environment = os.environ if environment is None else environment

    async def list_profiles(self) -> ModelProfilesView:
        snapshot = await asyncio.to_thread(self._registry.reload_if_changed)
        effective = self._resolve_from_snapshot(
            snapshot.config.models,
            snapshot.config.default_profile,
        )
        return ModelProfilesView(
            generation=snapshot.generation,
            source_digest=snapshot.source_digest,
            default_profile=snapshot.config.default_profile,
            effective_default_profile=effective,
            profile_override=self._profile_override,
            profiles=[
                self._profile_view(
                    name,
                    profile,
                    default_profile=snapshot.config.default_profile,
                    effective_default=effective,
                )
                for name, profile in sorted(snapshot.config.models.items())
            ],
        )

    async def get_profile(self, profile_name: str) -> ModelProfileView:
        normalized = profile_name.strip()
        view = await self.list_profiles()
        for profile in view.profiles:
            if profile.name == normalized:
                return profile
        raise EntityNotFoundError("Model profile", normalized)

    async def upsert_profile(
        self,
        profile_name: str,
        profile: ModelProfile,
        *,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> ModelProfileView:
        normalized = profile_name.strip()
        if not profile.requires_api_key and api_key is not None:
            raise ApplicationConflictError(
                "invalid_model_profile",
                "api_key must not be supplied when requires_api_key is false",
            )
        snapshot = await asyncio.to_thread(self._registry.reload_if_changed)
        effective_default = self._profile_override or snapshot.config.default_profile
        if normalized == effective_default:
            if profile.requires_api_key:
                has_stored = await asyncio.to_thread(
                    self._registry.has_stored_api_key,
                    normalized,
                )
                current_profile = snapshot.config.models.get(normalized)
                stored_key_reusable = bool(
                    current_profile is not None
                    and same_credential_destination(current_profile, profile)
                )
                has_stored_after = bool(api_key and api_key.strip()) or (
                    has_stored and not clear_api_key and stored_key_reusable
                )
            else:
                has_stored_after = False
            configured, _, environment_configured = self._credential_state(
                normalized,
                profile,
                has_stored=has_stored_after,
            )
            if not configured:
                self._raise_credentials_missing(
                    normalized,
                    has_stored=has_stored_after,
                    environment_configured=environment_configured,
                    mutation=True,
                )
        try:
            await asyncio.to_thread(
                self._registry.upsert,
                normalized,
                profile,
                api_key=api_key,
                clear_api_key=clear_api_key or not profile.requires_api_key,
            )
        except ValueError as exc:
            raise ApplicationConflictError("invalid_model_profile", str(exc)) from exc
        return await self.get_profile(profile_name)

    async def set_default(self, profile_name: str) -> ModelProfilesView:
        try:
            normalized = await asyncio.to_thread(self._registry.resolve, profile_name)
            profile = await asyncio.to_thread(self._registry.get, normalized)
        except ModelProfileNotFoundError as exc:
            raise EntityNotFoundError("Model profile", exc.profile_name) from exc
        except ValueError as exc:
            raise ApplicationConflictError("invalid_model_profile", str(exc)) from exc
        configured, has_stored, environment_configured = self._credential_state(
            normalized,
            profile,
        )
        if not configured:
            self._raise_credentials_missing(
                normalized,
                has_stored=has_stored,
                environment_configured=environment_configured,
                mutation=True,
            )
        await asyncio.to_thread(self._registry.set_default, normalized)
        return await self.list_profiles()

    async def delete_profile(self, profile_name: str) -> ModelProfilesView:
        normalized = profile_name.strip()
        if self._profile_override is not None and normalized == self._profile_override:
            raise ApplicationConflictError(
                "model_profile_in_use",
                "the effective default model profile cannot be removed while "
                "RIFTX_MODEL_PROFILE selects it",
            )
        run_in_use, session_in_use = await asyncio.gather(
            self._runs.has_nonterminal_model_profile(normalized),
            self._sessions.has_nonterminal_model_profile(normalized),
        )
        if run_in_use or session_in_use:
            raise ApplicationConflictError(
                "model_profile_in_use",
                f"model profile {normalized!r} is used by active runtime state",
                details={
                    "profile": normalized,
                    "used_by_nonterminal_run": run_in_use,
                    "used_by_nonterminal_session": session_in_use,
                },
            )
        try:
            await asyncio.to_thread(self._registry.delete, normalized)
        except ModelProfileNotFoundError as exc:
            raise EntityNotFoundError("Model profile", exc.profile_name) from exc
        except ModelRegistryConflictError as exc:
            raise ApplicationConflictError("model_profile_in_use", str(exc)) from exc
        except ValueError as exc:
            raise ApplicationConflictError("invalid_model_profile", str(exc)) from exc
        return await self.list_profiles()

    async def resolve_profile(self, profile_name: str | None) -> str:
        try:
            selected = await asyncio.to_thread(
                self._registry.resolve,
                profile_name,
                override=self._profile_override,
            )
        except ModelProfileNotFoundError as exc:
            if profile_name is None:
                raise ServiceUnavailableError(
                    "model_configuration_unavailable",
                    f"The effective default model profile {exc.profile_name!r} is not configured",
                ) from exc
            raise EntityNotFoundError("Model profile", exc.profile_name) from exc
        except ValueError as exc:
            raise ApplicationConflictError("invalid_model_profile", str(exc)) from exc
        profile = await asyncio.to_thread(self._registry.get, selected)
        configured, has_stored, environment_configured = self._credential_state(
            selected,
            profile,
        )
        if not configured:
            self._raise_credentials_missing(
                selected,
                has_stored=has_stored,
                environment_configured=environment_configured,
                mutation=False,
            )
        return selected

    def _resolve_from_snapshot(
        self,
        models: Mapping[str, ModelProfile],
        configured_default: str,
    ) -> str:
        selected = self._profile_override or configured_default
        if selected not in models:
            raise ServiceUnavailableError(
                "model_configuration_unavailable",
                f"The effective default model profile {selected!r} is not configured",
            )
        return selected

    def _profile_view(
        self,
        name: str,
        profile: ModelProfile,
        *,
        default_profile: str,
        effective_default: str,
    ) -> ModelProfileView:
        has_stored = (
            self._registry.has_stored_api_key(name, profile) if profile.requires_api_key else False
        )
        return ModelProfileView(
            name=name,
            provider=profile.provider,
            model=profile.model,
            request_mode=profile.api,
            base_url=profile.base_url,
            api_key_env=profile.api_key_env,
            requires_api_key=profile.requires_api_key,
            timeout_seconds=profile.timeout_seconds,
            max_retries=profile.max_retries,
            has_stored_api_key=has_stored,
            api_key_configured=self._credential_state(
                name,
                profile,
                has_stored=has_stored,
            )[0],
            is_default=name == default_profile,
            is_effective_default=name == effective_default,
        )

    def _credential_state(
        self,
        name: str,
        profile: ModelProfile,
        *,
        has_stored: bool | None = None,
    ) -> tuple[bool, bool, bool]:
        if not profile.requires_api_key:
            # Do not even query credential sources for credential-free profiles.
            return True, False, False
        environment_key = (
            self._environment.get(profile.api_key_env) if profile.api_key_env else None
        )
        stored = (
            self._registry.has_stored_api_key(name, profile) if has_stored is None else has_stored
        )
        environment_configured = bool(environment_key)
        return (
            bool(environment_configured or stored or not profile.requires_api_key),
            stored,
            environment_configured,
        )

    @staticmethod
    def _raise_credentials_missing(
        profile_name: str,
        *,
        has_stored: bool,
        environment_configured: bool,
        mutation: bool,
    ) -> None:
        error_type = ApplicationConflictError if mutation else ServiceUnavailableError
        raise error_type(
            "model_credentials_missing",
            f"Model profile {profile_name!r} requires an API credential, but none is configured",
            details={
                "profile": profile_name,
                "has_stored_api_key": has_stored,
                "environment_configured": environment_configured,
            },
        )
