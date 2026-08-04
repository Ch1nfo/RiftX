from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

import riftx.runner.daemon as daemon_module
from riftx.audit.source_ingest import SourceIngestBackendAvailability
from riftx.config import AuditConfig, AuditSourceIngestConfig
from riftx.domain import AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY
from riftx.runner.control_client import RunnerControlClient

runner = CliRunner()


def _enabled_audit(tmp_path: Path) -> AuditConfig:
    source_root = tmp_path / "source"
    source_root.mkdir()
    state_root = tmp_path / "audit-state"
    state_root.mkdir()
    return AuditConfig(
        enabled=True,
        source_roots=(source_root,),
        snapshot_root=state_root / "snapshots",
        temp_root=state_root / "tmp",
        fix_root=state_root / "fixes",
        source_ingest=AuditSourceIngestConfig(image_digest="a" * 64),
    )


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


def test_registration_advertises_audit_only_after_exact_readiness_probe(
    tmp_path: Path,
) -> None:
    audit = _enabled_audit(tmp_path)
    base = daemon_module.RunnerDaemonConfig(
        server_url="http://control.invalid",
        node_id="local",
        name="Local Runner",
        state_path=tmp_path / "runner",
        audit=audit,
        capabilities=(AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY, "process"),
        labels={
            "audit_source_ingest_available": "spoofed",
            "audit_source_ingest_policy_digest": "spoofed",
        },
    )

    unavailable = base.registration
    assert AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY not in unavailable.capabilities
    assert not {key for key in unavailable.labels or {} if key.startswith("audit_source_ingest_")}

    ready = daemon_module.replace(base, audit_preflight_ready=True).registration
    assert AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY in ready.capabilities
    assert ready.labels is not None
    assert ready.labels["audit_source_ingest_available"] == "true"
    assert ready.labels["audit_source_ingest_backend_id"] == "linux_container"
    assert ready.labels["audit_source_ingest_image_digest"] == "a" * 64
    assert ready.labels["audit_source_ingest_policy_digest"] != "spoofed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("available", "can_enable", "expected_ready"),
    [
        (True, True, True),
        (False, True, False),
        (True, False, False),
    ],
)
async def test_audit_runner_is_built_only_when_source_ingest_is_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
    can_enable: bool,
    expected_ready: bool,
) -> None:
    audit = _enabled_audit(tmp_path)
    config = daemon_module.RunnerDaemonConfig(
        server_url="http://control.invalid",
        node_id="local",
        name="Local Runner",
        state_path=tmp_path / "runner",
        audit=audit,
    )
    config.state_path.mkdir()

    class FakeBackend:
        def __init__(self, *, audit: AuditConfig, state_root: Path) -> None:
            assert audit == config.audit
            assert state_root == config.state_path

        async def reconcile_mount_probe(self) -> str | None:
            return None

        async def probe_availability(self) -> SourceIngestBackendAvailability:
            return SourceIngestBackendAvailability(
                available=available,
                reason_code=None if available else "audit_sandbox_unavailable",
                component_digest="b" * 64 if available else None,
                worker_digest="c" * 64 if available else None,
            )

    built: list[dict[str, object]] = []

    class FakeAuditRunner:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs)

    monkeypatch.setattr(
        daemon_module,
        "DockerAuditPreflightCapsuleBackend",
        FakeBackend,
    )
    monkeypatch.setattr(daemon_module, "AuditPreflightRunner", FakeAuditRunner)

    class FakeClient:
        def can_enable_protocol_capability(self, capability: str) -> bool:
            assert capability == AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY
            return can_enable

    configured, audit_runner = await daemon_module._configure_audit_preflight(
        config,
        cast(RunnerControlClient, FakeClient()),
    )

    assert configured.audit_preflight_ready is expected_ready
    assert (audit_runner is not None) is expected_ready
    assert len(built) == int(expected_ready)
    assert (
        AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY in configured.registration.capabilities
    ) is expected_ready


@pytest.mark.asyncio
async def test_disabled_audit_still_reconciles_readiness_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = daemon_module.RunnerDaemonConfig(
        server_url="http://control.invalid",
        node_id="local",
        name="Local Runner",
        state_path=tmp_path / "runner",
        audit=AuditConfig(enabled=False),
    )
    config.state_path.mkdir()
    reconciled = 0

    class FakeBackend:
        def __init__(self, *, audit: AuditConfig, state_root: Path) -> None:
            assert audit == config.audit
            assert state_root == config.state_path

        async def reconcile_mount_probe(self) -> str | None:
            nonlocal reconciled
            reconciled += 1
            return "d" * 64

        async def probe_availability(self) -> SourceIngestBackendAvailability:
            raise AssertionError("disabled Audit must not run a new readiness probe")

    monkeypatch.setattr(
        daemon_module,
        "DockerAuditPreflightCapsuleBackend",
        FakeBackend,
    )

    class FakeClient:
        def can_enable_protocol_capability(self, _capability: str) -> bool:
            raise AssertionError("disabled Audit must not enable capability")

    configured, audit_runner = await daemon_module._configure_audit_preflight(
        config,
        cast(RunnerControlClient, FakeClient()),
    )

    assert reconciled == 1
    assert configured.audit_preflight_ready is False
    assert audit_runner is None


@pytest.mark.asyncio
async def test_daemon_starts_audit_only_after_auth_and_closes_it_before_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    holder: dict[str, daemon_module.RunnerDaemon] = {}

    class FakeClient:
        async def connect(self, _: object) -> str:
            order.append("connect")
            return "token"

        async def poll(self, **_: object) -> None:
            order.append("ordinary_poll")
            holder["daemon"]._closed = True

        async def close(self) -> None:
            order.append("client_close")

    class FakeAuditRunner:
        async def start(self) -> None:
            order.append("audit_start")

        async def close(self) -> None:
            order.append("audit_close")

    daemon = daemon_module.RunnerDaemon(
        config=daemon_module.RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="local",
            name="Local Runner",
            state_path=tmp_path / "runner",
        ),
        client=cast(RunnerControlClient, FakeClient()),
        supervisor=cast(daemon_module.ExecutionRunner, object()),
        executions=cast(daemon_module.ExecutionRepository, object()),
        audit_preflight_runner=cast(
            daemon_module.AuditPreflightRunner,
            FakeAuditRunner(),
        ),
    )
    holder["daemon"] = daemon

    async def fake_resume_active() -> None:
        order.append("resume_active")

    monkeypatch.setattr(daemon, "resume_active", fake_resume_active)

    await daemon.run_forever()
    await daemon.close()

    assert order == [
        "connect",
        "audit_start",
        "resume_active",
        "ordinary_poll",
        "audit_close",
        "client_close",
    ]
