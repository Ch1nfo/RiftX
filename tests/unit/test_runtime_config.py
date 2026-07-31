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
            "RIFTX_RUNNER_CREDENTIALS": "private/runner-credentials.json",
            "RIFTX_TEMPORAL_ADDRESS": "temporal.test:7233",
            "RIFTX_TEMPORAL_TLS_ENABLED": "true",
            "RIFTX_TEMPORAL_TLS_SERVER_ROOT_CA_PATH": "tls/server-ca.pem",
            "RIFTX_TEMPORAL_TLS_SERVER_NAME": "temporal.service.test",
            "RIFTX_TEMPORAL_TLS_CLIENT_CERT_PATH": "tls/client-cert.pem",
            "RIFTX_TEMPORAL_TLS_CLIENT_PRIVATE_KEY_PATH": "tls/client-key.pem",
            "RIFTX_TEMPORAL_API_KEY": "temporal-secret-from-env",
            "RIFTX_CORS_ORIGINS": "https://one.test, https://two.test",
            "RIFTX_RUNNER_REGISTRATION_TOKEN": "from-secret-env",
            "RIFTX_REQUIRE_CONTAINMENT": "false",
            "RIFTX_PAYLOAD_UID": "65532",
            "RIFTX_PAYLOAD_GID": "65533",
            "RIFTX_MODELS_CONFIG": "custom-models.yaml",
            "RIFTX_MODEL_SECRETS": "private/model-secrets.json",
            "RIFTX_MODEL_PROFILE": "fast",
            "RIFTX_ADMIN_TOKEN": "admin-secret",
            "RIFTX_TRUST_PROXY_AUTH": "true",
        },
    )

    settings = APISettings.from_config(config)

    assert settings.database_url == "sqlite+aiosqlite:///env.db"
    assert settings.tools_config_path == Path("custom-tools.yaml")
    assert settings.workspace_root == Path("runs")
    assert settings.runner_state_path == Path("state")
    assert config.runner.credential_path == Path("private/runner-credentials.json")
    assert settings.temporal_address == "temporal.test:7233"
    assert settings.temporal_tls_enabled is True
    assert settings.temporal_tls_server_root_ca_path == Path("tls/server-ca.pem")
    assert settings.temporal_tls_server_name == "temporal.service.test"
    assert settings.temporal_tls_client_cert_path == Path("tls/client-cert.pem")
    assert settings.temporal_tls_client_private_key_path == Path("tls/client-key.pem")
    assert settings.temporal_api_key is not None
    assert settings.temporal_api_key.get_secret_value() == "temporal-secret-from-env"
    assert "temporal-secret-from-env" not in repr(config)
    assert "temporal-secret-from-env" not in repr(settings)
    assert "temporal-secret-from-env" not in str(config.model_dump(mode="python"))
    assert "tls/client-key.pem" not in repr(config)
    assert "tls/client-key.pem" not in repr(settings)
    assert "tls/client-key.pem" not in str(config.model_dump(mode="python"))
    assert settings.cors_origins == ("https://one.test", "https://two.test")
    assert settings.runner_registration_token == "from-secret-env"
    assert config.execution.require_containment is False
    assert settings.require_containment is False
    assert config.execution.payload_uid == 65532
    assert config.execution.payload_gid == 65533
    assert settings.payload_uid == 65532
    assert settings.payload_gid == 65533
    assert settings.models_config_path == Path("custom-models.yaml")
    assert settings.model_secrets_path == Path("private/model-secrets.json")
    assert settings.model_profile_override == "fast"
    assert settings.admin_token == "admin-secret"
    assert config.security.trust_proxy_auth is True


def test_runtime_model_paths_default_to_local_non_example_files(tmp_path: Path) -> None:
    config = load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        environment={},
    )

    settings = APISettings.from_config(config)

    assert config.models.path == Path("configs/models.yaml")
    assert config.models.secrets_path == Path(".riftx/secrets/models.json")
    assert settings.models_config_path == Path("configs/models.yaml")
    assert settings.model_secrets_path == Path(".riftx/secrets/models.json")
    assert config.runner.credential_path == Path(".riftx/secrets/runner-credentials.json")


def test_runtime_tool_path_defaults_to_local_non_example_file(tmp_path: Path) -> None:
    config = load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        environment={},
    )

    settings = APISettings.from_config(config)

    assert config.tools.path == Path("configs/tools.yaml")
    assert settings.tools_config_path == Path("configs/tools.yaml")


def test_kernel_containment_is_required_by_default(tmp_path: Path) -> None:
    config = load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        environment={},
    )

    settings = APISettings.from_config(config)

    assert config.execution.require_containment is True
    assert settings.require_containment is True


@pytest.mark.parametrize(
    "execution",
    [
        {"payload_uid": 65532},
        {"payload_gid": 65532},
        {"payload_uid": 0, "payload_gid": 65532},
        {"payload_uid": 65532, "payload_gid": 0},
    ],
)
def test_payload_identity_must_be_complete_and_unprivileged(
    tmp_path: Path,
    execution: dict[str, int],
) -> None:
    config_path = tmp_path / "riftx.yaml"
    write_yaml(config_path, {"execution": execution})

    with pytest.raises(RiftXConfigError, match="payload_(?:uid|gid)"):
        load_riftx_config(
            system_path=tmp_path / "missing-system.yaml",
            user_path=tmp_path / "missing-user.yaml",
            explicit_path=config_path,
            environment={},
        )


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


def test_plaintext_admin_token_is_rejected_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "riftx.yaml"
    write_yaml(config_path, {"security": {"admin_token": "do-not-store-this"}})

    with pytest.raises(RiftXConfigError, match="secret field 'admin_token'"):
        load_riftx_config(
            system_path=tmp_path / "missing-system.yaml",
            user_path=tmp_path / "missing-user.yaml",
            explicit_path=config_path,
            environment={},
        )


def test_plaintext_temporal_api_key_is_rejected_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "riftx.yaml"
    write_yaml(
        config_path,
        {"temporal": {"tls_enabled": True, "api_key": "temporal-yaml-secret"}},
    )

    with pytest.raises(RiftXConfigError) as captured:
        load_riftx_config(
            system_path=tmp_path / "missing-system.yaml",
            user_path=tmp_path / "missing-user.yaml",
            explicit_path=config_path,
            environment={},
        )

    assert "secret field 'api_key'" in str(captured.value)
    assert "temporal-yaml-secret" not in str(captured.value)


def test_temporal_api_key_requires_tls_without_disclosing_secret(tmp_path: Path) -> None:
    temporal_api_key = "temporal-validation-secret"

    with pytest.raises(RiftXConfigError) as captured:
        load_riftx_config(
            system_path=tmp_path / "missing-system.yaml",
            user_path=tmp_path / "missing-user.yaml",
            environment={"RIFTX_TEMPORAL_API_KEY": temporal_api_key},
        )

    assert "Temporal API key authentication requires TLS" in str(captured.value)
    assert temporal_api_key not in str(captured.value)
    assert temporal_api_key not in repr(captured.value)


@pytest.mark.parametrize(
    ("temporal", "message"),
    [
        (
            {"tls_server_root_ca_path": "server-ca.pem"},
            "certificate and server-name settings require TLS",
        ),
        (
            {"tls_enabled": True, "tls_client_cert_path": "client-cert.pem"},
            "must be configured together",
        ),
    ],
)
def test_temporal_tls_configuration_rejects_unsafe_combinations(
    tmp_path: Path,
    temporal: dict[str, object],
    message: str,
) -> None:
    config_path = tmp_path / "riftx.yaml"
    write_yaml(config_path, {"temporal": temporal})

    with pytest.raises(RiftXConfigError, match=message):
        load_riftx_config(
            system_path=tmp_path / "missing-system.yaml",
            user_path=tmp_path / "missing-user.yaml",
            explicit_path=config_path,
            environment={},
        )


def test_temporal_api_key_cannot_be_supplied_by_non_secret_override(tmp_path: Path) -> None:
    override_secret = "temporal-cli-override-secret"

    with pytest.raises(RiftXConfigError) as captured:
        load_riftx_config(
            system_path=tmp_path / "missing-system.yaml",
            user_path=tmp_path / "missing-user.yaml",
            environment={},
            cli_overrides={"temporal": {"tls_enabled": True, "api_key": override_secret}},
        )

    assert "must come from RIFTX_TEMPORAL_API_KEY" in str(captured.value)
    assert override_secret not in str(captured.value)


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
    assert config.execution.policy.value == "registered_only"
    assert config.execution.require_containment is True
    assert config.execution_output.max_inline_bytes == 32768
    assert config.execution_output.preview_head_bytes == 8192
    assert config.execution_output.preview_tail_bytes == 8192
    assert config.execution_output.max_context_tokens == 2000
    assert config.approval.default_mode.value == "balanced"
    assert config.tools.path == Path("configs/tools.yaml")
    assert config.models.path == Path("configs/models.yaml")
    assert config.models.secrets_path == Path(".riftx/secrets/models.json")
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
