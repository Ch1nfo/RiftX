from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from riftx.api import APISettings
from riftx.config import (
    EnvironmentSecretProvider,
    RiftXConfigError,
    load_riftx_config,
    resolve_secret,
)


def write_yaml(path: Path, payload: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def test_configuration_layers_follow_documented_precedence(tmp_path: Path) -> None:
    system = tmp_path / "system.yaml"
    user = tmp_path / "user.yaml"
    explicit = tmp_path / "explicit.yaml"
    write_yaml(
        system,
        {
            "server": {"host": "system.test", "port": 8001},
            "database": {"url": "sqlite+aiosqlite:///system.db"},
        },
    )
    write_yaml(user, {"server": {"port": 8002}, "runner": {"node_id": "user-node"}})
    write_yaml(explicit, {"server": {"port": 8003}, "workspace": {"root": "/explicit"}})

    config = load_riftx_config(
        system_path=system,
        user_path=user,
        explicit_path=explicit,
        environment={"RIFTX_SERVER_PORT": "8004", "RIFTX_NODE_ID": "env-node"},
        cli_overrides={"server": {"port": 8005}},
        run_overrides={"server": {"port": 8006}},
    )

    assert config.server.host == "system.test"
    assert config.server.port == 8006
    assert config.database.url == "sqlite+aiosqlite:///system.db"
    assert config.runner.node_id == "env-node"
    assert config.workspace.root == Path("/explicit")


def test_environment_compatibility_maps_into_api_settings(tmp_path: Path) -> None:
    missing_system = tmp_path / "missing-system.yaml"
    missing_user = tmp_path / "missing-user.yaml"
    config = load_riftx_config(
        system_path=missing_system,
        user_path=missing_user,
        environment={
            "RIFTX_DATABASE_URL": "sqlite+aiosqlite:///env.db",
            "RIFTX_TOOLS_CONFIG": "custom-tools.yaml",
            "RIFTX_WORKSPACE_ROOT": "runs",
            "RIFTX_RUNNER_STATE": "state",
            "RIFTX_TEMPORAL_ADDRESS": "temporal.test:7233",
            "RIFTX_CORS_ORIGINS": "https://one.test, https://two.test",
            "RIFTX_RUNNER_REGISTRATION_TOKEN": "from-secret-env",
        },
    )

    settings = APISettings.from_config(config)

    assert settings.database_url == "sqlite+aiosqlite:///env.db"
    assert settings.tools_config_path == Path("custom-tools.yaml")
    assert settings.workspace_root == Path("runs")
    assert settings.runner_state_path == Path("state")
    assert settings.temporal_address == "temporal.test:7233"
    assert settings.cors_origins == ("https://one.test", "https://two.test")
    assert settings.runner_registration_token == "from-secret-env"


def test_plaintext_secrets_are_rejected_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "riftx.yaml"
    write_yaml(config_path, {"runner": {"registration_token": "do-not-store-this"}})

    with pytest.raises(RiftXConfigError, match="secret provider"):
        load_riftx_config(
            system_path=tmp_path / "missing-system.yaml",
            user_path=tmp_path / "missing-user.yaml",
            explicit_path=config_path,
            environment={},
        )


def test_secret_provider_chain_uses_first_available_value() -> None:
    value = resolve_secret(
        "RIFTX_MODEL_API_KEY",
        [
            EnvironmentSecretProvider({}),
            EnvironmentSecretProvider({"RIFTX_MODEL_API_KEY": "secret-value"}),
        ],
    )

    assert value == "secret-value"


def test_example_runtime_config_is_valid(tmp_path: Path) -> None:
    config = load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        explicit_path=Path("configs/riftx.example.yaml"),
        environment={},
    )

    assert config.server.port == 8787
    assert config.execution.policy.value == "open"
    assert config.execution_output.max_inline_bytes == 32768
    assert config.execution_output.preview_head_bytes == 8192
    assert config.execution_output.preview_tail_bytes == 8192
    assert config.execution_output.max_context_tokens == 2000
    assert config.approval.default_mode.value == "balanced"
    assert config.mcp.max_concurrent_per_server == 2
    assert config.mcp.max_concurrent_total == 16
    assert config.mcp.circuit_breaker.failure_threshold == 3
    assert config.mcp.circuit_breaker.cooldown_seconds == 60


def test_execution_output_config_rejects_unsafe_limits(tmp_path: Path) -> None:
    config_path = tmp_path / "riftx.yaml"
    write_yaml(
        config_path,
        {
            "execution_output": {
                "max_inline_bytes": 512,
                "max_context_tokens": 99,
            }
        },
    )

    with pytest.raises(RiftXConfigError, match="invalid RiftX configuration"):
        load_riftx_config(
            system_path=tmp_path / "missing-system.yaml",
            user_path=tmp_path / "missing-user.yaml",
            explicit_path=config_path,
            environment={},
        )
