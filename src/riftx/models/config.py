"""Model profile configuration."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ModelConfigError(ValueError):
    pass


class ModelProviderKind(StrEnum):
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai_compatible"


class ModelAPI(StrEnum):
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ModelProviderKind = ModelProviderKind.OPENAI_COMPATIBLE
    model: str = Field(min_length=1)
    api: ModelAPI = ModelAPI.RESPONSES
    base_url: str | None = None
    api_key_env: str | None = "RIFTX_MODEL_API_KEY"
    requires_api_key: bool = True
    timeout_seconds: float = Field(default=120, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_profile: str = "primary"
    models: dict[str, ModelProfile]

    def model_post_init(self, __context: object) -> None:
        if self.default_profile not in self.models:
            raise ValueError(f"default model profile {self.default_profile!r} is not configured")


def load_models_config(path: Path) -> ModelsConfig:
    try:
        content = path.read_text()
    except OSError as exc:
        raise ModelConfigError(f"could not read model config {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ModelConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ModelConfigError(f"model config {path} must contain a mapping")
    try:
        return ModelsConfig.model_validate(raw)
    except ValidationError as exc:
        raise ModelConfigError(f"invalid model config {path}: {exc}") from exc
