"""Schemas for model-profile configuration without credential disclosure."""

from __future__ import annotations

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from riftx.application.services import ModelProfilesView, ModelProfileView
from riftx.models import (
    MAX_MODEL_TIMEOUT_SECONDS,
    ModelAPI,
    ModelProfile,
    ModelProviderKind,
    validate_provider_base_url,
    validate_remote_api_key_env,
    validate_remote_base_url,
)


class ModelProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    provider: ModelProviderKind = ModelProviderKind.OPENAI_COMPATIBLE
    model: str = Field(min_length=1)
    request_mode: ModelAPI = Field(
        default=ModelAPI.CHAT_COMPLETIONS,
        validation_alias=AliasChoices("request_mode", "api"),
    )
    base_url: str | None = None
    api_key_env: str | None = "RIFTX_MODEL_API_KEY"
    requires_api_key: bool = True
    timeout_seconds: float = Field(
        default=120,
        gt=0,
        le=MAX_MODEL_TIMEOUT_SECONDS,
        allow_inf_nan=False,
    )
    max_retries: int = Field(default=2, ge=0, le=10)
    api_key: SecretStr | None = Field(default=None, repr=False)
    clear_stored_api_key: bool = False

    @field_validator("model")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model must not be empty")
        return normalized

    @field_validator("api_key_env")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return validate_remote_api_key_env(value)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return validate_remote_base_url(value)

    @field_validator("clear_stored_api_key")
    @classmethod
    def validate_credential_change(cls, value: bool, info: ValidationInfo) -> bool:
        if value and info.data.get("api_key") is not None:
            raise ValueError("api_key and clear_stored_api_key cannot be used together")
        return value

    @model_validator(mode="after")
    def validate_provider_endpoint(self) -> ModelProfileUpdateRequest:
        validate_provider_base_url(self.provider, self.base_url)
        if not self.requires_api_key and self.api_key is not None:
            raise ValueError("api_key must not be supplied when requires_api_key is false")
        return self

    def to_profile(self) -> ModelProfile:
        return ModelProfile(
            provider=self.provider,
            model=self.model,
            api=self.request_mode,
            base_url=self.base_url,
            api_key_env=self.api_key_env,
            requires_api_key=self.requires_api_key,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
        )


class SetDefaultModelProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = Field(min_length=1)


class ModelProfileResponse(BaseModel):
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

    @classmethod
    def from_view(cls, view: ModelProfileView) -> ModelProfileResponse:
        return cls(
            name=view.name,
            provider=view.provider,
            model=view.model,
            request_mode=view.request_mode,
            base_url=view.base_url,
            api_key_env=view.api_key_env,
            requires_api_key=view.requires_api_key,
            timeout_seconds=view.timeout_seconds,
            max_retries=view.max_retries,
            has_stored_api_key=view.has_stored_api_key,
            api_key_configured=view.api_key_configured,
            is_default=view.is_default,
            is_effective_default=view.is_effective_default,
        )


class ModelProfileSummaryResponse(BaseModel):
    name: str
    model: str
    request_mode: ModelAPI
    api_key_configured: bool
    is_default: bool
    is_effective_default: bool

    @classmethod
    def from_view(cls, view: ModelProfileView) -> ModelProfileSummaryResponse:
        return cls(
            name=view.name,
            model=view.model,
            request_mode=view.request_mode,
            api_key_configured=view.api_key_configured,
            is_default=view.is_default,
            is_effective_default=view.is_effective_default,
        )


class ModelProfileSummaryListResponse(BaseModel):
    default_profile: str
    effective_default_profile: str
    profiles: list[ModelProfileSummaryResponse]

    @classmethod
    def from_view(cls, view: ModelProfilesView) -> ModelProfileSummaryListResponse:
        return cls(
            default_profile=view.default_profile,
            effective_default_profile=view.effective_default_profile,
            profiles=[ModelProfileSummaryResponse.from_view(profile) for profile in view.profiles],
        )


class ModelProfileListResponse(BaseModel):
    generation: int
    source_digest: str
    default_profile: str
    effective_default_profile: str
    profile_override: str | None
    profiles: list[ModelProfileResponse]

    @classmethod
    def from_view(cls, view: ModelProfilesView) -> ModelProfileListResponse:
        return cls(
            generation=view.generation,
            source_digest=view.source_digest,
            default_profile=view.default_profile,
            effective_default_profile=view.effective_default_profile,
            profile_override=view.profile_override,
            profiles=[ModelProfileResponse.from_view(profile) for profile in view.profiles],
        )
