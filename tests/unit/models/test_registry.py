from __future__ import annotations

import json
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import riftx.models.registry as registry_module
from riftx.models import (
    ModelAPI,
    ModelProfile,
    ModelProfileNotFoundError,
    ModelProfileRegistry,
    ModelProviderKind,
    ModelRegistryConflictError,
    ModelRegistryError,
    ModelsConfig,
    RiftXModelProvider,
)


def _initial_config() -> ModelsConfig:
    return ModelsConfig(
        default_profile="primary",
        models={
            "primary": ModelProfile(
                provider=ModelProviderKind.OPENAI_COMPATIBLE,
                model="model-v1",
                api=ModelAPI.CHAT_COMPLETIONS,
                base_url="http://localhost:9000/v1",
                api_key_env="PRIMARY_KEY",
            )
        },
    )


def test_registry_initializes_missing_config_atomically(tmp_path: Path) -> None:
    config_path = tmp_path / "configs" / "models.yaml"
    registry = ModelProfileRegistry(
        config_path,
        tmp_path / ".riftx" / "secrets" / "models.json",
        initial_config=_initial_config(),
    )

    snapshot = registry.refresh()

    assert snapshot.config.default_profile == "primary"
    assert snapshot.config.models["primary"].model == "model-v1"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_registry_stores_api_keys_outside_yaml_with_strict_permissions(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "configs" / "models.yaml"
    secrets_path = tmp_path / ".riftx" / "secrets" / "models.json"
    registry = ModelProfileRegistry(
        config_path,
        secrets_path,
        initial_config=_initial_config(),
    )
    registry.refresh()

    registry.upsert(
        "primary",
        _initial_config().models["primary"],
        api_key="stored-secret",
    )

    assert "stored-secret" not in config_path.read_text()
    stored = json.loads(secrets_path.read_text())
    assert stored["version"] == 2
    assert stored["api_keys"]["primary"]["value"] == "stored-secret"
    assert len(stored["api_keys"]["primary"]["profile_digest"]) == 64
    assert stat.S_IMODE(secrets_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600


def test_registry_tightens_existing_credential_store_permissions(tmp_path: Path) -> None:
    secrets_path = tmp_path / "secrets" / "models.json"
    secrets_path.parent.mkdir(mode=0o755)
    secrets_path.write_text(json.dumps({"version": 1, "api_keys": {"primary": "stored-secret"}}))
    secrets_path.chmod(0o644)
    registry = ModelProfileRegistry(
        tmp_path / "models.yaml",
        secrets_path,
        initial_config=_initial_config(),
    )

    registry.refresh()

    assert registry.api_key("primary") == "stored-secret"
    assert stat.S_IMODE(secrets_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600


def test_registry_rejects_credential_store_with_symlinked_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "target"
    (target / "nested").mkdir(parents=True)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(target, target_is_directory=True)
    registry = ModelProfileRegistry(
        tmp_path / "models.yaml",
        linked_parent / "nested" / "models.json",
        initial_config=_initial_config(),
    )

    with pytest.raises(ModelRegistryError, match="symbolic links"):
        registry.refresh()


def test_registry_rolls_back_key_when_metadata_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "models.yaml"
    registry = ModelProfileRegistry(
        config_path,
        tmp_path / "secrets" / "models.json",
        initial_config=_initial_config(),
    )
    registry.refresh()
    registry.upsert("primary", _initial_config().models["primary"], api_key="old-secret")
    previous_config = config_path.read_bytes()

    def fail_metadata_write(_: ModelsConfig) -> None:
        raise OSError("simulated metadata failure")

    monkeypatch.setattr(registry, "_write_config", fail_metadata_write)
    with pytest.raises(OSError, match="simulated metadata failure"):
        registry.upsert(
            "primary",
            _initial_config().models["primary"].model_copy(update={"model": "model-v2"}),
            api_key="new-secret",
        )

    assert registry.api_key("primary") == "old-secret"
    assert config_path.read_bytes() == previous_config


def test_registry_restores_key_when_config_atomic_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "models.yaml"
    registry = ModelProfileRegistry(
        config_path,
        tmp_path / "secrets" / "models.json",
        initial_config=_initial_config(),
    )
    registry.refresh()
    registry.upsert("primary", _initial_config().models["primary"], api_key="old-secret")
    previous_config = config_path.read_bytes()
    original_atomic_write = registry_module._atomic_write

    def fail_config_write(
        path: Path,
        content: bytes,
        *,
        file_mode: int,
        directory_mode: int | None = None,
    ) -> None:
        if path == config_path:
            raise OSError("simulated config filesystem failure")
        original_atomic_write(
            path,
            content,
            file_mode=file_mode,
            directory_mode=directory_mode,
        )

    monkeypatch.setattr(registry_module, "_atomic_write", fail_config_write)
    with pytest.raises(OSError, match="simulated config filesystem failure"):
        registry.upsert(
            "primary",
            _initial_config().models["primary"].model_copy(update={"model": "model-v2"}),
            api_key="new-secret",
        )

    assert registry.api_key("primary") == "old-secret"
    assert config_path.read_bytes() == previous_config


def test_registry_restores_key_even_when_config_rollback_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "models.yaml"
    registry = ModelProfileRegistry(
        config_path,
        tmp_path / "secrets" / "models.json",
        initial_config=_initial_config(),
    )
    registry.refresh()
    registry.upsert("primary", _initial_config().models["primary"], api_key="old-secret")
    previous_config = config_path.read_bytes()
    original_atomic_write = registry_module._atomic_write

    def commit_then_fail_config_write(
        path: Path,
        content: bytes,
        *,
        file_mode: int,
        directory_mode: int | None = None,
    ) -> None:
        if path == config_path and content == previous_config:
            raise OSError("simulated config rollback failure")
        original_atomic_write(
            path,
            content,
            file_mode=file_mode,
            directory_mode=directory_mode,
        )
        if path == config_path:
            raise OSError("simulated post-commit config failure")

    monkeypatch.setattr(registry_module, "_atomic_write", commit_then_fail_config_write)
    with pytest.raises(ModelRegistryError, match="rollback was incomplete"):
        registry.upsert(
            "primary",
            _initial_config().models["primary"].model_copy(update={"model": "model-v2"}),
            api_key="new-secret",
        )

    assert (
        registry.secrets.get(
            "primary",
            profile_digest=registry_module._profile_digest(_initial_config().models["primary"]),
        )
        == "old-secret"
    )


def test_provider_prefers_environment_key_then_uses_stored_key(tmp_path: Path) -> None:
    registry = ModelProfileRegistry(
        tmp_path / "models.yaml",
        tmp_path / "secrets" / "models.json",
        initial_config=_initial_config(),
    )
    registry.refresh()
    registry.upsert(
        "primary",
        _initial_config().models["primary"],
        api_key="stored-secret",
    )

    environment_provider = RiftXModelProvider(
        registry,
        environment={"PRIMARY_KEY": "environment-secret"},
    )
    environment_provider.get_model("primary")
    stored_provider = RiftXModelProvider(registry, environment={})
    stored_provider.get_model("primary")

    assert environment_provider._clients["primary"].api_key == "environment-secret"
    assert stored_provider._clients["primary"].api_key == "stored-secret"


def test_provider_reloads_config_and_credentials_written_by_another_registry(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "models.yaml"
    secrets_path = tmp_path / "secrets" / "models.json"
    worker_registry = ModelProfileRegistry(
        config_path,
        secrets_path,
        initial_config=_initial_config(),
    )
    worker_registry.refresh()
    worker_registry.upsert(
        "primary",
        _initial_config().models["primary"],
        api_key="secret-v1",
    )
    provider = RiftXModelProvider(worker_registry, environment={})
    first = provider.get_model("primary")

    api_registry = ModelProfileRegistry(config_path, secrets_path)
    api_registry.refresh()
    api_registry.upsert(
        "primary",
        _initial_config().models["primary"].model_copy(update={"model": "model-v2"}),
        api_key="secret-v2",
    )

    second = provider.get_model("primary")

    assert second is not first
    assert provider.config.models["primary"].model == "model-v2"
    assert provider._clients["primary"].api_key == "secret-v2"


def test_registry_does_not_rebind_stored_key_to_changed_endpoint(tmp_path: Path) -> None:
    registry = ModelProfileRegistry(
        tmp_path / "models.yaml",
        tmp_path / "secrets" / "models.json",
        initial_config=_initial_config(),
    )
    registry.refresh()
    registry.upsert("primary", _initial_config().models["primary"], api_key="old-secret")
    changed_endpoint = (
        _initial_config()
        .models["primary"]
        .model_copy(update={"base_url": "https://capture.example/v1"})
    )

    registry.upsert("primary", changed_endpoint)

    assert registry.api_key("primary", changed_endpoint) is None
    assert "old-secret" not in registry.secrets.path.read_text()


def test_registry_preserves_stored_key_for_same_endpoint_metadata_update(
    tmp_path: Path,
) -> None:
    registry = ModelProfileRegistry(
        tmp_path / "models.yaml",
        tmp_path / "secrets" / "models.json",
        initial_config=_initial_config(),
    )
    registry.refresh()
    registry.upsert("primary", _initial_config().models["primary"], api_key="stored-secret")
    changed_model = (
        _initial_config()
        .models["primary"]
        .model_copy(update={"model": "model-v2", "timeout_seconds": 300})
    )

    registry.upsert("primary", changed_model)

    assert registry.api_key("primary", changed_model) == "stored-secret"


def test_cross_process_reader_cannot_observe_interleaved_endpoint_and_key(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "models.yaml"
    secrets_path = tmp_path / "secrets" / "models.json"
    registry = ModelProfileRegistry(
        config_path,
        secrets_path,
        initial_config=_initial_config(),
    )
    registry.refresh()
    registry.upsert("primary", _initial_config().models["primary"], api_key="secret-v1")
    new_profile = (
        _initial_config()
        .models["primary"]
        .model_copy(
            update={
                "model": "model-v2",
                "base_url": "https://models-v2.example/v1",
            }
        )
    )
    updated = ModelsConfig(default_profile="primary", models={"primary": new_profile})
    started_path = tmp_path / "reader-started"
    result_path = tmp_path / "reader-result.json"
    child_code = """
import json
import sys
from pathlib import Path
from riftx.models import ModelProfileRegistry

config_path, secrets_path, started_path, result_path = map(Path, sys.argv[1:])
started_path.write_text("started")
registry = ModelProfileRegistry(config_path, secrets_path)
snapshot = registry.refresh()
profile = snapshot.config.models["primary"]
result_path.write_text(json.dumps([profile.base_url, registry.api_key("primary", profile)]))
"""
    child: subprocess.Popen[bytes] | None = None

    try:
        with registry_module._registry_file_lock(registry.lock_path):
            registry.secrets.set(
                "primary",
                "secret-v2",
                profile_digest=registry_module._profile_digest(new_profile),
            )
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_code,
                    str(config_path),
                    str(secrets_path),
                    str(started_path),
                    str(result_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while not started_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert started_path.exists()
            time.sleep(0.1)
            assert child.poll() is None
            assert not result_path.exists()
            registry._write_config(updated)

        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, (stdout, stderr)
        assert json.loads(result_path.read_text()) == [
            "https://models-v2.example/v1",
            "secret-v2",
        ]
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_registry_requires_selecting_another_default_before_delete(tmp_path: Path) -> None:
    registry = ModelProfileRegistry(
        tmp_path / "models.yaml",
        tmp_path / "secrets" / "models.json",
        initial_config=_initial_config(),
    )
    registry.refresh()

    with pytest.raises(ModelRegistryConflictError, match="default"):
        registry.delete("primary")


def test_registry_validates_profile_selection_and_supports_both_request_modes(
    tmp_path: Path,
) -> None:
    registry = ModelProfileRegistry(
        tmp_path / "models.yaml",
        tmp_path / "secrets" / "models.json",
        initial_config=_initial_config(),
    )
    registry.refresh()
    registry.upsert(
        "responses",
        ModelProfile(
            provider=ModelProviderKind.OPENAI,
            model="responses-model",
            api=ModelAPI.RESPONSES,
            requires_api_key=False,
            api_key_env=None,
        ),
    )

    snapshot = registry.set_default("responses")

    assert snapshot.config.models["primary"].api is ModelAPI.CHAT_COMPLETIONS
    assert snapshot.config.models["responses"].api is ModelAPI.RESPONSES
    assert registry.resolve(None) == "responses"
    assert registry.resolve("primary") == "primary"
    with pytest.raises(ModelProfileNotFoundError, match="missing"):
        registry.resolve("missing")
    with pytest.raises(ValueError, match="profile names"):
        registry.upsert("invalid profile", _initial_config().models["primary"])


def test_registry_skips_secret_store_for_no_key_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ModelProfile(
        model="local-model",
        base_url="http://localhost:11434/v1",
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
        raise AssertionError("Secret Store was read")

    monkeypatch.setattr(registry.secrets, "get", fail)
    monkeypatch.setattr(registry.secrets, "contains", fail)

    assert registry.api_key("local", profile) is None
    assert registry.has_stored_api_key("local", profile) is False


def test_registry_removes_stored_key_with_deleted_profile(tmp_path: Path) -> None:
    secrets_path = tmp_path / "secrets" / "models.json"
    registry = ModelProfileRegistry(
        tmp_path / "models.yaml",
        secrets_path,
        initial_config=_initial_config(),
    )
    registry.refresh()
    registry.upsert(
        "temporary",
        _initial_config().models["primary"],
        api_key="temporary-secret",
    )

    registry.delete("temporary")

    assert (
        registry.has_stored_api_key(
            "temporary",
            _initial_config().models["primary"],
        )
        is False
    )
    assert "temporary-secret" not in secrets_path.read_text()
