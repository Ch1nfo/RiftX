from pathlib import Path

import pytest

from riftx.models import ModelAPI, ModelConfigError, ModelsConfig, load_models_config


def test_example_models_config_is_valid() -> None:
    path = Path(__file__).parents[3] / "configs" / "models.example.yaml"

    config = load_models_config(path)

    assert config.default_profile == "primary"
    assert set(config.models) == {"primary", "fast", "report"}
    assert all(
        profile.api is ModelAPI.CHAT_COMPLETIONS for profile in config.models.values()
    )


def test_model_profile_defaults_to_chat_completions() -> None:
    config = ModelsConfig.model_validate(
        {
            "default_profile": "primary",
            "models": {"primary": {"model": "test-model"}},
        }
    )

    assert config.models["primary"].api is ModelAPI.CHAT_COMPLETIONS


def test_models_config_requires_default_profile() -> None:
    with pytest.raises(ValueError, match="default model profile"):
        ModelsConfig(default_profile="primary", models={})


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
