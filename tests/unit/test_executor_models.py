from pathlib import Path

import pytest

from riftx.executors import (
    EnvironmentMode,
    ProcessExecutionRequest,
    ShellKind,
    build_shell_argv,
    merge_environment,
)


def test_environment_layers_override_and_remove_values() -> None:
    environment = merge_environment(
        {"SHARED": "node", "NODE_ONLY": "1"},
        {"SHARED": "tool", "REMOVE_ME": None},
        {"SHARED": "run"},
        {"SHARED": "execution"},
        mode=EnvironmentMode.INHERIT,
        host_environment={"SHARED": "host", "REMOVE_ME": "yes"},
    )

    assert environment == {"SHARED": "execution", "NODE_ONLY": "1"}


def test_clean_environment_does_not_inherit_host_values() -> None:
    environment = merge_environment(
        {"ONLY": "explicit"},
        mode=EnvironmentMode.CLEAN,
        host_environment={"SECRET": "host"},
    )
    assert environment == {"ONLY": "explicit"}


def test_inherited_environment_strips_control_plane_and_secret_values() -> None:
    environment = merge_environment(
        mode=EnvironmentMode.INHERIT,
        host_environment={
            "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "RIFTX_CGROUP_V2_ROOT": "/sys/fs/cgroup/riftx",
            "RIFTX_LLM_API_KEY": "model-secret",
            "OPENAI_API_KEY": "openai-secret",
            "TEMPORAL_API_KEY": "temporal-secret",
            "GITHUB_TOKEN": "github-secret",
            "DATABASE_URL": "postgresql://secret",
            "SSH_AUTH_SOCK": "/private/agent.sock",
        },
    )

    assert environment == {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}


def test_explicit_layer_can_intentionally_restore_filtered_variable() -> None:
    environment = merge_environment(
        {"OPENAI_API_KEY": "tool-scoped-key"},
        mode=EnvironmentMode.INHERIT,
        host_environment={
            "PATH": "/usr/bin:/bin",
            "OPENAI_API_KEY": "host-key",
        },
    )

    assert environment == {
        "PATH": "/usr/bin:/bin",
        "OPENAI_API_KEY": "tool-scoped-key",
    }


def test_environment_rejects_invalid_names() -> None:
    with pytest.raises(ValueError, match="invalid environment"):
        merge_environment({"BAD=NAME": "value"}, mode=EnvironmentMode.CLEAN)


@pytest.mark.parametrize(
    ("shell", "path", "expected_prefix"),
    [
        (ShellKind.BASH, Path("/bin/bash"), ["/bin/bash", "-lc"]),
        (ShellKind.ZSH, Path("/bin/zsh"), ["/bin/zsh", "-lc"]),
        (
            ShellKind.POWERSHELL,
            Path("pwsh.exe"),
            ["pwsh.exe", "-NoLogo", "-NoProfile", "-Command"],
        ),
        (ShellKind.CMD, Path("cmd.exe"), ["cmd.exe", "/d", "/s", "/c"]),
    ],
)
def test_shell_argv_is_explicit(shell: ShellKind, path: Path, expected_prefix: list[str]) -> None:
    assert build_shell_argv(shell, path, "echo ok") == [*expected_prefix, "echo ok"]


def test_process_request_rejects_missing_cwd(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cwd does not exist"):
        ProcessExecutionRequest(
            execution_key="missing-cwd",
            argv=["echo", "ok"],
            cwd=tmp_path / "missing",
            stdout_path=tmp_path / "stdout.log",
            stderr_path=tmp_path / "stderr.log",
        )
