from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import riftx.runner.daemon as daemon_module

runner = CliRunner()


def test_standalone_runner_help_omits_registration_token_option() -> None:
    result = runner.invoke(
        daemon_module.app,
        ["serve", "--help"],
        terminal_width=200,
    )

    assert result.exit_code == 0, result.output
    assert "--registration-t" not in result.output


def test_standalone_runner_rejects_registration_token_argv_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[daemon_module.RunnerDaemonConfig] = []
    canary = "standalone-bootstrap-canary-never-log-0001"

    async def fake_run(config: daemon_module.RunnerDaemonConfig) -> None:
        calls.append(config)

    monkeypatch.setattr(daemon_module, "run_runner_daemon", fake_run)
    result = runner.invoke(
        daemon_module.app,
        ["serve", "--registration-token", canary],
    )

    assert result.exit_code == 2
    assert "No such option: --registration-token" in result.output
    assert canary not in result.output
    assert calls == []


def test_standalone_runner_reads_exact_bootstrap_token_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[daemon_module.RunnerDaemonConfig] = []
    bootstrap_token = "standalone-runner-bootstrap-token-from-env-0001"

    async def fake_run(config: daemon_module.RunnerDaemonConfig) -> None:
        calls.append(config)

    monkeypatch.setattr(daemon_module, "run_runner_daemon", fake_run)
    result = runner.invoke(
        daemon_module.app,
        [
            "serve",
            "--server-url",
            "http://127.0.0.1:8787",
            "--node-id",
            "runner-env",
            "--name",
            "Runner Env",
            "--state-path",
            str(tmp_path / "state"),
            "--credential-path",
            str(tmp_path / "secrets" / "runner-credentials.json"),
        ],
        env={"RIFTX_RUNNER_REGISTRATION_TOKEN": bootstrap_token},
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].registration_token == bootstrap_token
    assert calls[0].audit.enabled is False
    assert bootstrap_token not in repr(calls[0])


def test_standalone_runner_loads_audit_from_explicit_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[daemon_module.RunnerDaemonConfig] = []
    config_path = tmp_path / "riftx.yaml"
    config_path.write_text("audit:\n  default_mode: diff\n", encoding="utf-8")

    async def fake_run(config: daemon_module.RunnerDaemonConfig) -> None:
        calls.append(config)

    monkeypatch.setattr(daemon_module, "run_runner_daemon", fake_run)
    result = runner.invoke(
        daemon_module.app,
        ["serve", "--config", str(config_path)],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].audit.default_mode == "diff"
