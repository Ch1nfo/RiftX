from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from riftx.api import APISettings
from riftx.application.errors import AuthenticationError
from riftx.application.services import RunnerControlService
from riftx.config import (
    EnvironmentSecretProvider,
    RiftXConfigError,
    load_riftx_config,
    resolve_secret,
)
from riftx.runner import RunnerPaths
from riftx.runner.daemon import RunnerDaemonConfig
from riftx.security import DeploymentProfileError

_RUNNER_BOOTSTRAP_CANARY = "synthetic-runner-bootstrap-canary-never-log"


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
            "RIFTX_CORS_ORIGINS": "http://127.0.0.1:3000, http://localhost:4000",
            "RIFTX_RUNNER_REGISTRATION_TOKEN": _RUNNER_BOOTSTRAP_CANARY,
            "RIFTX_REQUIRE_CONTAINMENT": "false",
            "RIFTX_PAYLOAD_UID": "65532",
            "RIFTX_PAYLOAD_GID": "65533",
            "RIFTX_MODELS_CONFIG": "custom-models.yaml",
            "RIFTX_MODEL_SECRETS": "private/model-secrets.json",
            "RIFTX_MODEL_PROFILE": "fast",
            "RIFTX_CODE_LSP_ENABLED": "true",
            "RIFTX_CODE_LSP_SOCKET_PATH": "/tmp/riftx-lsp.sock",
            "RIFTX_CODE_LSP_BACKEND_ID": "trusted-lsp",
            "RIFTX_CODE_LSP_BACKEND_VERSION": "1.0.0",
            "RIFTX_CODE_LSP_TOKEN_ENV": "RIFTX_LSP_TOKEN",
            "RIFTX_CODE_LSP_TIMEOUT_SECONDS": "20",
            "RIFTX_WEB_SEARCH_ENABLED": "true",
            "RIFTX_WEB_SEARCH_PROVIDERS": "searxng,openai_hosted",
            "RIFTX_SEARXNG_ENDPOINT": "https://search.example.test/base",
            "RIFTX_WEB_SEARCH_TIMEOUT_SECONDS": "45",
            "RIFTX_CONNECTORS_ENABLED": "true",
            "RIFTX_ADMIN_TOKEN": "test-only-admin-operator-token-0002",
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_TRUST_PROXY_AUTH": "false",
        },
    )

    settings = APISettings.from_config(config)

    assert settings.database_url == "sqlite+aiosqlite:///env.db"
    assert settings.tools_config_path == Path("custom-tools.yaml")
    assert settings.workspace_root == Path("runs")
    assert settings.runner_state_path == Path("state")
    assert config.runner.credential_path == Path("private/runner-credentials.json")
    assert settings.runner_credential_path == Path("private/runner-credentials.json")
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
    assert settings.cors_origins == (
        "http://127.0.0.1:3000",
        "http://localhost:4000",
    )
    assert config.runner.registration_token == _RUNNER_BOOTSTRAP_CANARY
    assert settings.runner_registration_token == _RUNNER_BOOTSTRAP_CANARY
    runner_dump = config.model_dump(mode="python")["runner"]
    assert isinstance(runner_dump, dict)
    assert "registration_token" not in runner_dump
    assert _RUNNER_BOOTSTRAP_CANARY not in config.model_dump_json()
    assert _RUNNER_BOOTSTRAP_CANARY not in repr(config.runner)
    assert _RUNNER_BOOTSTRAP_CANARY not in repr(config)
    assert _RUNNER_BOOTSTRAP_CANARY not in repr(settings)
    assert config.execution.require_containment is False
    assert settings.require_containment is False
    assert config.execution.payload_uid == 65532
    assert config.execution.payload_gid == 65533
    assert settings.payload_uid == 65532
    assert settings.payload_gid == 65533
    assert settings.models_config_path == Path("custom-models.yaml")
    assert settings.model_secrets_path == Path("private/model-secrets.json")
    assert settings.model_profile_override == "fast"
    assert config.code.lsp.enabled is True
    assert config.code.lsp.socket_path == Path("/tmp/riftx-lsp.sock")
    assert config.code.lsp.backend_id == "trusted-lsp"
    assert config.code.lsp.backend_version == "1.0.0"
    assert config.code.lsp.token_env == "RIFTX_LSP_TOKEN"
    assert config.code.lsp.timeout_seconds == 20
    assert config.web.search.providers == ("searxng", "openai_hosted")
    assert config.web.search.searxng_endpoint == "https://search.example.test/base"
    assert config.web.search.timeout_seconds == 45
    assert config.connectors.enabled is True
    assert settings.connectors_enabled is True
    assert settings.admin_token == "test-only-admin-operator-token-0002"
    assert config.security.trust_proxy_auth is False
    assert settings.trust_profile.value == "local_single_operator"


def test_runner_bootstrap_canary_reaches_auth_boundary_without_repr_or_error_leak(
    tmp_path: Path,
) -> None:
    config = load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        environment={
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_ADMIN_TOKEN": "test-only-admin-operator-token-0002",
            "RIFTX_RUNNER_REGISTRATION_TOKEN": _RUNNER_BOOTSTRAP_CANARY,
        },
    )
    settings = APISettings.from_config(config)
    assert settings.runner_registration_token == _RUNNER_BOOTSTRAP_CANARY

    service = RunnerControlService(
        credentials=object(),  # type: ignore[arg-type]
        commands=object(),  # type: ignore[arg-type]
        nodes=object(),  # type: ignore[arg-type]
        executions=object(),  # type: ignore[arg-type]
        runs=object(),  # type: ignore[arg-type]
        paths=RunnerPaths(tmp_path / "runner"),
        registration_token=settings.runner_registration_token,
    )
    service.authenticate_bootstrap(_RUNNER_BOOTSTRAP_CANARY)
    with pytest.raises(AuthenticationError) as captured:
        service.authenticate_bootstrap("synthetic-wrong-bootstrap-token")

    assert captured.value.code == "runner_registration_denied"
    assert _RUNNER_BOOTSTRAP_CANARY not in repr(captured.value)

    daemon_config = RunnerDaemonConfig(
        server_url="http://127.0.0.1:8787",
        node_id="runner-canary",
        name="Runner Canary",
        state_path=tmp_path / "runner-state",
        registration_token=_RUNNER_BOOTSTRAP_CANARY,
    )
    assert daemon_config.registration_token == _RUNNER_BOOTSTRAP_CANARY
    assert _RUNNER_BOOTSTRAP_CANARY not in repr(daemon_config)

    with pytest.raises(DeploymentProfileError) as weak_service:
        RunnerControlService(
            credentials=object(),  # type: ignore[arg-type]
            commands=object(),  # type: ignore[arg-type]
            nodes=object(),  # type: ignore[arg-type]
            executions=object(),  # type: ignore[arg-type]
            runs=object(),  # type: ignore[arg-type]
            paths=RunnerPaths(tmp_path / "weak-runner"),
            registration_token="weak-bootstrap-token",
        )
    assert weak_service.value.code == "runner_registration_credential_weak"


@pytest.mark.parametrize("credential", ["", "r" * 31, " " * 32, "引" * 32])
def test_runner_config_rejects_weak_registration_credential_without_echo(
    tmp_path: Path,
    credential: str,
) -> None:
    with pytest.raises(RiftXConfigError) as captured:
        load_riftx_config(
            system_path=tmp_path / "missing-system.yaml",
            user_path=tmp_path / "missing-user.yaml",
            environment={"RIFTX_RUNNER_REGISTRATION_TOKEN": credential},
        )

    assert "runner_registration_credential_weak" in str(captured.value)
    assert "至少包含 32 个" in str(captured.value)
    if credential:
        assert credential not in str(captured.value)


def test_runner_daemon_config_rejects_weak_registration_credential() -> None:
    with pytest.raises(DeploymentProfileError) as captured:
        RunnerDaemonConfig(
            server_url="http://127.0.0.1:8787",
            node_id="runner-weak",
            name="Runner Weak",
            state_path=Path("runner-state"),
            registration_token="r" * 31,
        )

    assert captured.value.code == "runner_registration_credential_weak"
    assert "r" * 31 not in repr(captured.value)


def test_runtime_model_paths_default_to_local_non_example_files(tmp_path: Path) -> None:
    config = load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        environment={
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_ADMIN_TOKEN": "test-only-admin-operator-token-0002",
        },
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
        environment={
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_ADMIN_TOKEN": "test-only-admin-operator-token-0002",
        },
    )

    settings = APISettings.from_config(config)

    assert config.tools.path == Path("configs/tools.yaml")
    assert settings.tools_config_path == Path("configs/tools.yaml")


def test_web_search_has_no_default_provider(tmp_path: Path) -> None:
    config = load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        environment={},
    )

    assert config.web.search.enabled is False
    assert config.web.search.providers == ()
    assert config.web.search.searxng_endpoint is None


def test_connectors_are_disabled_by_default(tmp_path: Path) -> None:
    config = load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        environment={
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_ADMIN_TOKEN": "test-only-admin-operator-token-0002",
        },
    )

    assert config.connectors.enabled is False
    assert APISettings.from_config(config).connectors_enabled is False


def test_controlled_lsp_is_disabled_by_default(tmp_path: Path) -> None:
    config = load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        environment={},
    )

    assert config.code.lsp.enabled is False
    assert config.code.lsp.socket_path is None


@pytest.mark.parametrize(
    "lsp",
    [
        {"enabled": True},
        {
            "enabled": True,
            "socket_path": "relative/lsp.sock",
            "backend_id": "trusted-lsp",
            "backend_version": "1.0.0",
            "token_env": "RIFTX_LSP_TOKEN",
        },
        {
            "enabled": True,
            "socket_path": "/tmp/lsp.sock",
            "backend_id": "Bad Backend",
            "backend_version": "1.0.0",
            "token_env": "RIFTX_LSP_TOKEN",
        },
    ],
)
def test_controlled_lsp_configuration_fails_closed(
    tmp_path: Path,
    lsp: dict[str, object],
) -> None:
    explicit = tmp_path / "riftx.yaml"
    write_yaml(explicit, {"code": {"lsp": lsp}})

    with pytest.raises(RiftXConfigError, match="code.lsp"):
        load_riftx_config(
            system_path=tmp_path / "missing-system.yaml",
            user_path=tmp_path / "missing-user.yaml",
            explicit_path=explicit,
            environment={},
        )


@pytest.mark.parametrize(
    "search",
    [
        {"enabled": True, "providers": []},
        {"providers": ["searxng"]},
        {
            "providers": ["searxng"],
            "searxng_endpoint": "https://user:secret@search.example.test",
        },
        {"providers": ["openai_hosted", "openai_hosted"]},
    ],
)
def test_web_search_configuration_fails_closed(
    tmp_path: Path,
    search: dict[str, object],
) -> None:
    explicit = tmp_path / "riftx.yaml"
    write_yaml(explicit, {"web": {"search": search}})

    with pytest.raises(RiftXConfigError, match="web.search"):
        load_riftx_config(
            system_path=tmp_path / "missing-system.yaml",
            user_path=tmp_path / "missing-user.yaml",
            explicit_path=explicit,
            environment={},
        )


def test_kernel_containment_is_required_by_default(tmp_path: Path) -> None:
    config = load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        environment={
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_ADMIN_TOKEN": "test-only-admin-operator-token-0002",
        },
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


def test_plaintext_audit_preflight_token_key_is_rejected_from_yaml(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "riftx.yaml"
    write_yaml(config_path, {"audit": {"preflight_token_key": "A" * 43}})

    with pytest.raises(RiftXConfigError, match="secret field 'preflight_token_key'"):
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
    assert config.security.trust_profile.value == "local_single_operator"
    assert config.security.local_principal_path == Path(".riftx/secrets/local-principal.json")
    assert config.security.trust_proxy_auth is False
    assert config.mcp.max_concurrent_per_server == 2
    assert config.mcp.max_concurrent_total == 16
    assert config.mcp.discovery_timeout_seconds == 15
    assert config.mcp.refresh_interval_seconds == 60
    assert config.mcp.max_tools_per_server == 256
    assert config.mcp.max_schema_bytes == 65_536
    assert config.mcp.max_call_argument_bytes == 1_048_576
    assert config.mcp.max_call_result_bytes == 16_777_216
    assert config.mcp.circuit_breaker.failure_threshold == 3
    assert config.mcp.circuit_breaker.cooldown_seconds == 60
    assert config.mcp.servers == {}


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
