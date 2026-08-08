"""Local onboarding filesystem and configuration contracts."""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest
import yaml

import riftx.onboarding as onboarding
from riftx.config import load_riftx_config
from riftx.models.config import (
    ModelAPI,
    ModelProfile,
    ModelProviderKind,
    load_models_config,
)
from riftx.onboarding import (
    OnboardError,
    initialize_local_onboarding,
    resolve_onboard_tool_template,
    resolve_onboard_web_dist,
)
from riftx.tools.config import load_tool_config


def _tool_template(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": "registered_only",
                "tools": {
                    "available": {
                        "command": ["available-tool"],
                        "capabilities": ["demo"],
                    },
                    "missing": {
                        "command": ["missing-tool"],
                        "capabilities": ["optional"],
                    },
                    "operator_disabled": {
                        "enabled": False,
                        "command": ["disabled-tool"],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _local_profile() -> ModelProfile:
    return ModelProfile(
        provider=ModelProviderKind.OPENAI_COMPATIBLE,
        model="qwen-local",
        api=ModelAPI.CHAT_COMPLETIONS,
        base_url="http://127.0.0.1:11434/v1",
        api_key_env=None,
        requires_api_key=False,
    )


def test_onboarding_creates_authoritative_user_config_and_disables_missing_tools(
    tmp_path: Path,
) -> None:
    executable_root = tmp_path / "bin"
    executable_root.mkdir()
    executable = executable_root / "available-tool"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    config_path = tmp_path / "config" / "riftx" / "riftx.yaml"

    result = initialize_local_onboarding(
        config_path,
        model_profile=_local_profile(),
        environment={
            "PATH": str(executable_root),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        tool_template_path=_tool_template(tmp_path / "tools-template.yaml"),
    )

    assert result.disabled_tools == ("missing",)
    assert result.config_path == config_path
    assert result.models_path == config_path.parent / "models.yaml"
    assert result.tools_path == config_path.parent / "tools.yaml"
    for path in (result.config_path, result.models_path, result.tools_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for path in (
        result.config_path.parent,
        tmp_path / "state" / "riftx",
        tmp_path / "state" / "riftx" / "secrets",
        tmp_path / "data" / "riftx",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700

    config = load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        explicit_path=result.config_path,
        environment={},
    )
    models = load_models_config(result.models_path)
    tools = load_tool_config(result.tools_path)

    assert config.models.path == result.models_path
    assert config.tools.path == result.tools_path
    assert config.workspace.root == tmp_path / "data" / "riftx" / "workspaces"
    assert config.web.dist_path == resolve_onboard_web_dist()
    assert (config.web.dist_path / "index.html").is_file()
    assert config.audit.snapshot_root == tmp_path / "data" / "riftx" / "audit" / "snapshots"
    assert not (tmp_path / "data" / "riftx" / "audit" / "tmp").exists()
    assert not (tmp_path / "data" / "riftx" / "audit" / "fixes").exists()
    assert config.security.trust_profile == "local_single_operator"
    assert models.models["primary"] == _local_profile()
    assert tools.tools["available"].enabled
    assert not tools.tools["missing"].enabled
    assert not tools.tools["operator_disabled"].enabled


def test_onboarding_refuses_existing_or_symbolic_configuration_paths(
    tmp_path: Path,
) -> None:
    template = _tool_template(tmp_path / "tools-template.yaml")
    existing = tmp_path / "existing" / "riftx.yaml"
    existing.parent.mkdir()
    existing.write_text("sentinel\n", encoding="utf-8")

    with pytest.raises(OnboardError, match="already exists"):
        initialize_local_onboarding(
            existing,
            model_profile=_local_profile(),
            environment={
                "XDG_STATE_HOME": str(tmp_path / "state-a"),
                "XDG_DATA_HOME": str(tmp_path / "data-a"),
            },
            tool_template_path=template,
        )

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(OnboardError, match="symbolic"):
        initialize_local_onboarding(
            linked / "riftx.yaml",
            model_profile=_local_profile(),
            environment={
                "XDG_STATE_HOME": str(tmp_path / "state-b"),
                "XDG_DATA_HOME": str(tmp_path / "data-b"),
            },
            tool_template_path=template,
        )

    assert existing.read_text(encoding="utf-8") == "sentinel\n"
    assert not (real / "riftx.yaml").exists()


def test_onboarding_rolls_back_new_files_and_directories_on_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config" / "riftx" / "riftx.yaml"
    original_write = onboarding._write_new_file
    calls = 0

    def fail_second_write(path: Path, content: bytes) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        return original_write(path, content)

    monkeypatch.setattr(onboarding, "_write_new_file", fail_second_write)

    with pytest.raises(OnboardError, match="rolled back"):
        initialize_local_onboarding(
            config_path,
            model_profile=_local_profile(),
            environment={
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "XDG_DATA_HOME": str(tmp_path / "data"),
            },
            tool_template_path=_tool_template(tmp_path / "tools-template.yaml"),
        )

    assert not config_path.parent.exists()
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "data").exists()


def test_onboarding_removes_a_new_file_when_directory_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config" / "riftx" / "riftx.yaml"
    monkeypatch.setattr(
        onboarding,
        "_fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("injected fsync failure")),
    )

    with pytest.raises(OnboardError, match="rolled back"):
        initialize_local_onboarding(
            config_path,
            model_profile=_local_profile(),
            environment={
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "XDG_DATA_HOME": str(tmp_path / "data"),
            },
            tool_template_path=_tool_template(tmp_path / "tools-template.yaml"),
        )

    assert not config_path.parent.exists()
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "data").exists()


def test_onboarding_tool_template_is_packaged_from_the_authoritative_config() -> None:
    template = resolve_onboard_tool_template()
    assert template.name == "tools.example.yaml"
    assert load_tool_config(template).tools

    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["setuptools"]["data-files"]["share/riftx/templates"] == [
        "configs/tools.example.yaml"
    ]
    assert project["tool"]["setuptools"]["package-data"]["riftx"] == [
        "_webui/index.html",
        "_webui/assets/*",
    ]
