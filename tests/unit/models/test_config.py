from pathlib import Path

import pytest

from riftx.models import (
    MAX_MODEL_TIMEOUT_SECONDS,
    ModelAPI,
    ModelConfigError,
    ModelProfile,
    ModelProviderKind,
    ModelsConfig,
    load_models_config,
    parse_models_config,
)


def test_example_models_config_is_valid() -> None:
    path = Path(__file__).parents[3] / "configs" / "models.example.yaml"

    config = load_models_config(path)

    assert config.default_profile == "primary"
    assert set(config.models) == {"primary", "fast", "report"}
    assert all(profile.api is ModelAPI.CHAT_COMPLETIONS for profile in config.models.values())


def test_model_profile_defaults_to_chat_completions() -> None:
    config = ModelsConfig.model_validate(
        {
            "default_profile": "primary",
            "models": {
                "primary": {
                    "model": "test-model",
                    "base_url": "http://localhost:11434/v1",
                }
            },
        }
    )

    assert config.models["primary"].api is ModelAPI.CHAT_COMPLETIONS


def test_openai_profile_keeps_chat_completions_default_without_base_url() -> None:
    profile = ModelProfile(provider=ModelProviderKind.OPENAI, model="test-model")

    assert profile.api is ModelAPI.CHAT_COMPLETIONS
    assert profile.base_url is None


@pytest.mark.parametrize("base_url", [None, "", "   "])
def test_openai_compatible_profile_requires_nonempty_base_url(base_url: str | None) -> None:
    with pytest.raises(ValueError, match="explicit base_url"):
        ModelProfile(
            provider=ModelProviderKind.OPENAI_COMPATIBLE,
            model="test-model",
            base_url=base_url,
        )


@pytest.mark.parametrize(
    "timeout_seconds",
    [float("nan"), float("inf"), float("-inf"), 0, -1, MAX_MODEL_TIMEOUT_SECONDS + 0.01],
)
def test_model_timeout_must_be_finite_positive_and_bounded(timeout_seconds: float) -> None:
    with pytest.raises(ValueError):
        ModelProfile(
            provider=ModelProviderKind.OPENAI,
            model="test-model",
            timeout_seconds=timeout_seconds,
        )


def test_model_timeout_accepts_documented_upper_bound() -> None:
    profile = ModelProfile(
        provider=ModelProviderKind.OPENAI,
        model="test-model",
        timeout_seconds=MAX_MODEL_TIMEOUT_SECONDS,
    )

    assert profile.timeout_seconds == MAX_MODEL_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "environment_name",
    ["RIFTX_MODEL_REAL_KEY", "DATABASE_PASSWORD", "SERVICE_TOKEN", "CLIENT_SECRET"],
)
def test_no_key_profile_rejects_credential_environment_in_base_url(
    environment_name: str,
) -> None:
    with pytest.raises(ValueError, match="must not reference a credential"):
        ModelProfile(
            model="local-model",
            base_url=f"https://capture.example/${{{environment_name}}}/v1",
            api_key_env="RIFTX_MODEL_REAL_KEY",
            requires_api_key=False,
        )


def test_no_key_profile_allows_dedicated_base_url_environment() -> None:
    profile = ModelProfile(
        model="local-model",
        base_url="${RIFTX_MODEL_BASE_URL}",
        api_key_env="RIFTX_MODEL_REAL_KEY",
        requires_api_key=False,
    )

    assert profile.base_url == "${RIFTX_MODEL_BASE_URL}"


def test_models_config_requires_default_profile() -> None:
    with pytest.raises(ValueError, match="default model profile"):
        ModelsConfig(default_profile="primary", models={})


def test_models_config_rejects_invalid_profile_names() -> None:
    with pytest.raises(ValueError, match="profile names"):
        ModelsConfig.model_validate(
            {
                "default_profile": "invalid profile",
                "models": {"invalid profile": {"model": "test-model"}},
            }
        )


def test_model_profile_rejects_whitespace_only_model_name() -> None:
    with pytest.raises(ValueError, match="at least 1 character"):
        ModelsConfig.model_validate(
            {
                "default_profile": "primary",
                "models": {"primary": {"model": "   "}},
            }
        )


def test_load_models_config_wraps_validation_errors(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("default_profile: primary\nmodels: {}\n")

    with pytest.raises(ModelConfigError, match="invalid model config"):
        load_models_config(path)


def test_load_models_config_rejects_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("- primary\n")

    with pytest.raises(ModelConfigError, match="must contain a mapping"):
        load_models_config(path)


def test_yaml_validation_errors_never_echo_sensitive_inputs() -> None:
    sensitive_values = (
        "yaml-api-key-value",
        "yaml-token-value",
        "yaml-password-value",
        "yaml-secret-value",
    )
    content = f"""\
default_profile: primary
models:
  primary:
    provider: openai
    model: test-model
    api_key: {sensitive_values[0]}
    access_token: {sensitive_values[1]}
    password: {sensitive_values[2]}
    client_secret: {sensitive_values[3]}
"""

    with pytest.raises(ModelConfigError) as captured:
        parse_models_config(content)

    message = str(captured.value)
    assert all(value not in message for value in sensitive_values)
