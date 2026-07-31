"""Model profile configuration."""

from __future__ import annotations

import ipaddress
import re
import socket
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REMOTE_API_KEY_ENV = re.compile(r"^RIFTX_MODEL_[A-Z0-9_]+$")
_ENV_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")

MAX_MODEL_TIMEOUT_SECONDS = 600.0


class ModelConfigError(ValueError):
    pass


class ModelProviderKind(StrEnum):
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai_compatible"


class ModelAPI(StrEnum):
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"


class ModelProfile(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    provider: ModelProviderKind = ModelProviderKind.OPENAI_COMPATIBLE
    model: str = Field(min_length=1)
    api: ModelAPI = ModelAPI.CHAT_COMPLETIONS
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

    @model_validator(mode="after")
    def validate_provider_endpoint(self) -> ModelProfile:
        validate_provider_base_url(self.provider, self.base_url)
        if not self.requires_api_key:
            validate_no_key_base_url_environment(self.base_url, self.api_key_env)
        return self


class ModelsConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    default_profile: str = Field(default="primary", min_length=1)
    models: dict[str, ModelProfile]

    @field_validator("default_profile")
    @classmethod
    def validate_default_profile_name(cls, value: str) -> str:
        return normalize_profile_name(value)

    @field_validator("models")
    @classmethod
    def validate_model_profile_names(
        cls,
        value: dict[str, ModelProfile],
    ) -> dict[str, ModelProfile]:
        for name in value:
            if normalize_profile_name(name) != name:
                raise ValueError("model profile names in configuration must not contain padding")
        return value

    def model_post_init(self, __context: object) -> None:
        if self.default_profile not in self.models:
            raise ValueError(f"default model profile {self.default_profile!r} is not configured")


def load_models_config(path: Path) -> ModelsConfig:
    try:
        content = path.read_text()
    except OSError as exc:
        raise ModelConfigError(f"could not read model config {path}: {exc}") from exc
    return parse_models_config(content, source=str(path))


def parse_models_config(content: str | bytes, *, source: str = "model config") -> ModelsConfig:
    try:
        raw: Any = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        location = _yaml_error_location(exc)
        raise ModelConfigError(f"invalid YAML in {source}{location}") from exc
    if not isinstance(raw, dict):
        raise ModelConfigError(f"model config {source} must contain a mapping")
    try:
        return ModelsConfig.model_validate(raw)
    except ValidationError as exc:
        raise ModelConfigError(
            f"invalid model config {source}: {_validation_error_summary(exc)}"
        ) from exc


def default_models_config() -> ModelsConfig:
    """Return the safe metadata-only configuration used for first-run initialization."""

    return ModelsConfig(
        default_profile="primary",
        models={
            "primary": ModelProfile(
                provider=ModelProviderKind.OPENAI,
                model="gpt-5.6",
                api=ModelAPI.CHAT_COMPLETIONS,
                api_key_env="RIFTX_MODEL_API_KEY",
            )
        },
    )


def normalize_profile_name(profile_name: str) -> str:
    normalized = profile_name.strip()
    if not _PROFILE_NAME.fullmatch(normalized):
        raise ValueError(
            "model profile names must start with an alphanumeric character and contain only "
            "letters, numbers, '.', '_' or '-' (maximum 64 characters)"
        )
    return normalized


def validate_remote_api_key_env(value: str | None) -> str | None:
    """Validate the narrower environment boundary exposed to remote administrators.

    Local YAML is an operator-owned trust boundary and may still name another
    environment variable. WebUI/CLI/API mutations must not be able to select an
    arbitrary Worker secret and send it to a configured model endpoint.
    """

    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if not _REMOTE_API_KEY_ENV.fullmatch(normalized):
        raise ValueError(
            "api_key_env managed through the API must start with 'RIFTX_MODEL_' "
            "and contain only uppercase letters, numbers, or underscores"
        )
    return normalized


def validate_remote_base_url(value: str | None) -> str | None:
    """Validate model endpoints accepted through WebUI/CLI/API management.

    DNS and private-network policy remains a deployment concern, but obviously
    unsafe literal destinations and environment interpolation are rejected at
    this boundary. Loopback is intentionally supported for local model servers.
    """

    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if _ENV_REFERENCE.search(normalized):
        raise ValueError("base_url managed through the API must not contain environment references")
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain user information")
    if parsed.query:
        raise ValueError("base_url must not contain a query string")
    if parsed.fragment:
        raise ValueError("base_url must not contain a fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("base_url contains an invalid port") from exc

    host = parsed.hostname.split("%", 1)[0]
    address = _parse_literal_ip_address(host)
    if address is None:
        return normalized
    effective_address = address
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        effective_address = address.ipv4_mapped
    if (
        effective_address.is_link_local
        or effective_address.is_unspecified
        or effective_address.is_multicast
    ):
        raise ValueError("base_url must not target a link-local, unspecified, or multicast address")
    return normalized


def validate_provider_base_url(
    provider: ModelProviderKind,
    base_url: str | None,
) -> None:
    """Require compatible providers to name their destination explicitly."""

    if provider is ModelProviderKind.OPENAI_COMPATIBLE and not (base_url or "").strip():
        raise ValueError("openai_compatible model profiles require an explicit base_url")


def validate_no_key_base_url_environment(
    base_url: str | None,
    api_key_env: str | None,
) -> None:
    """Prevent credential-looking environment values from becoming request URLs."""

    for name in _ENV_REFERENCE.findall(base_url or ""):
        parts = {part for part in re.split(r"[^A-Za-z0-9]+", name.upper()) if part}
        if name == api_key_env or parts.intersection(
            {"APIKEY", "KEY", "PASSWORD", "SECRET", "TOKEN"}
        ):
            raise ValueError(
                "credential-free model base_url must not reference a credential "
                "environment variable"
            )


def same_credential_destination(current: ModelProfile, updated: ModelProfile) -> bool:
    """Return whether a stored key remains bound to the same provider destination."""

    return (current.provider, current.base_url) == (updated.provider, updated.base_url)


def _parse_literal_ip_address(
    host: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # OS resolvers commonly accept legacy decimal, octal, hexadecimal, and
    # shortened IPv4 literals. Normalize those without performing DNS I/O so
    # they cannot bypass link-local checks (for example 2852039166).
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return None
    return ipaddress.ip_address(packed)


def _yaml_error_location(error: yaml.YAMLError) -> str:
    mark = getattr(error, "problem_mark", None)
    line = getattr(mark, "line", None)
    column = getattr(mark, "column", None)
    if isinstance(line, int) and isinstance(column, int):
        return f" at line {line + 1}, column {column + 1}"
    return ""


def _validation_error_summary(error: ValidationError) -> str:
    summaries: list[str] = []
    for item in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        location = ".".join(str(part) for part in item.get("loc", ())) or "configuration"
        message = str(item.get("msg", "invalid value"))
        summaries.append(f"{location}: {message}")
    return "; ".join(summaries) or "validation failed"
