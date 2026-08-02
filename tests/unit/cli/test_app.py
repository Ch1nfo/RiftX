"""Typer command tests proving the CLI delegates to the HTTP client."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

import riftx.cli.app as cli_module
from riftx.cli.client import RiftXAPIError
from riftx.cli.render import render_error

runner = CliRunner()


class FakeAPIClient:
    instances: list[FakeAPIClient] = []
    fail = False
    unhealthy = False

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.calls: list[tuple[str, object]] = []
        self.__class__.instances.append(self)

    def __enter__(self) -> FakeAPIClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def create_run(self, payload: dict[str, object]) -> dict[str, Any]:
        self.calls.append(("create_run", payload))
        return {
            "id": "run-1",
            "status": "created",
            "objective": {"description": payload["objective"]},
            "node_id": payload.get("node_id", "local"),
            "approval_mode": payload.get("approval_mode", "balanced"),
            "workspace_path": payload.get("workspace_path", "/tmp/run-1"),
            "temporal_workflow_id": "workflow-run-1",
        }

    def list_runs(self, **kwargs: object) -> dict[str, Any]:
        self.calls.append(("list_runs", kwargs))
        if self.fail:
            raise RiftXAPIError(
                status_code=503,
                code="temporal_unavailable",
                message="Temporal unavailable",
            )
        return {"items": []}

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("cancel_run", run_id))
        return {"run": {"id": run_id, "status": "cancelled"}}

    def pause_run(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("pause_run", run_id))
        return {"run": {"id": run_id, "status": "paused"}}

    def cancel_current_execution(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("cancel_current_execution", run_id))
        return {"run": {"id": run_id, "status": "running"}}

    def get_run_metrics(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("get_run_metrics", run_id))
        return {"run_id": run_id, "metrics": {}, "generated_at": "now"}

    def compact_run(self, run_id: str, *, max_history_items: int = 100) -> dict[str, Any]:
        self.calls.append(("compact_run", (run_id, max_history_items)))
        return {"run": {"id": run_id, "status": "running"}}

    def create_memory(self, payload: dict[str, object]) -> dict[str, Any]:
        self.calls.append(("create_memory", payload))
        return {"id": "memory-1", **payload, "status": "active"}

    def list_memories(self, **kwargs: object) -> dict[str, Any]:
        self.calls.append(("list_memories", kwargs))
        return {"items": []}

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        self.calls.append(("get_memory", memory_id))
        return {"id": memory_id, "source_refs": []}

    def update_memory(self, memory_id: str, payload: dict[str, object]) -> dict[str, Any]:
        self.calls.append(("update_memory", (memory_id, payload)))
        return {"id": memory_id, **payload, "source_refs": payload.get("source_refs", [])}

    def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.calls.append(("delete_memory", memory_id))
        return {"id": memory_id, "status": "deleted"}

    def pin_memory(self, memory_id: str, *, pinned: bool = True) -> dict[str, Any]:
        self.calls.append(("pin_memory", (memory_id, pinned)))
        return {"id": memory_id, "pinned": pinned}

    def switch_run_model(self, run_id: str, model_profile: str) -> dict[str, Any]:
        self.calls.append(("switch_run_model", (run_id, model_profile)))
        return {"run": {"id": run_id, "status": "running"}}

    def list_model_profiles(self) -> dict[str, Any]:
        self.calls.append(("list_model_profiles", None))
        return self._model_profiles_payload()

    def get_model_profile(self, profile_name: str) -> dict[str, Any]:
        self.calls.append(("get_model_profile", profile_name))
        return self._model_profile(profile_name)

    def configure_model_profile(
        self,
        profile_name: str,
        payload: dict[str, object],
    ) -> dict[str, Any]:
        self.calls.append(("configure_model_profile", (profile_name, payload)))
        return {
            **self._model_profile(profile_name),
            **{key: value for key, value in payload.items() if key != "api_key"},
            "has_stored_api_key": "api_key" in payload,
            "api_key_configured": bool(
                payload.get("api_key") or not payload.get("requires_api_key", True)
            ),
        }

    def set_default_model_profile(self, profile_name: str) -> dict[str, Any]:
        self.calls.append(("set_default_model_profile", profile_name))
        payload = self._model_profiles_payload()
        payload["default_profile"] = profile_name
        payload["effective_default_profile"] = profile_name
        return payload

    def delete_model_profile(self, profile_name: str) -> dict[str, Any]:
        self.calls.append(("delete_model_profile", profile_name))
        return self._model_profiles_payload()

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        self.calls.append(("get_execution", execution_id))
        return self._execution(execution_id)

    def list_executions(self, run_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        self.calls.append(("list_executions", (run_id, limit, offset)))
        return {"items": [self._execution("execution-1")]}

    def wait_execution(
        self,
        execution_id: str,
        *,
        timeout_seconds: float = 30.0,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 64 * 1024,
        next_poll_after_seconds: int = 10,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "wait_execution",
                (
                    execution_id,
                    timeout_seconds,
                    stdout_cursor,
                    stderr_cursor,
                    max_bytes,
                    next_poll_after_seconds,
                ),
            )
        )
        return {
            "wait_status": "wait_timeout",
            "execution_status": "running",
            "execution_id": execution_id,
            "partial_output": "started",
            "next_poll_after_seconds": 10,
        }

    def cancel_execution(self, execution_id: str) -> dict[str, Any]:
        self.calls.append(("cancel_execution", execution_id))
        return {**self._execution(execution_id), "status": "cancelled"}

    def list_nodes(self, *, status: str | None = None) -> dict[str, Any]:
        self.calls.append(("list_nodes", status))
        return {"items": [self._node("node-1")]}

    def get_node(self, node_id: str) -> dict[str, Any]:
        self.calls.append(("get_node", node_id))
        return self._node(node_id)

    def disconnect_node(self, node_id: str) -> dict[str, Any]:
        self.calls.append(("disconnect_node", node_id))
        return {**self._node(node_id), "status": "offline"}

    def refresh_tools(self, node_id: str) -> dict[str, Any]:
        self.calls.append(("refresh_tools", node_id))
        availability = "unavailable" if self.unhealthy else "available"
        return {
            "node_id": node_id,
            "generation": 2,
            "tools": [
                {
                    "definition": {
                        "id": "python",
                        "enabled": True,
                        "executor": "process",
                        "capabilities": ["scripting"],
                    },
                    "state": {"availability": availability},
                }
            ],
        }

    def list_tools(self, node_id: str) -> dict[str, Any]:
        self.calls.append(("list_tools", node_id))
        return {
            "node_id": node_id,
            "generation": 1,
            "tools": [
                {
                    "definition": {
                        "id": "python",
                        "enabled": True,
                        "executor": "process",
                        "capabilities": ["scripting"],
                    },
                    "state": {"availability": "available"},
                }
            ],
        }

    def list_approvals(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("list_approvals", run_id))
        return {"items": []}

    def approve(self, approval_id: str, *, approve_for_run: bool = False) -> dict[str, Any]:
        self.calls.append(("approve", (approval_id, approve_for_run)))
        return {"id": approval_id, "status": "approved"}

    def reject(self, approval_id: str, *, reason: str | None = None) -> dict[str, Any]:
        self.calls.append(("reject", (approval_id, reason)))
        return {"id": approval_id, "status": "rejected"}

    def create_terminal(
        self,
        run_id: str,
        **kwargs: object,
    ) -> dict[str, Any]:
        self.calls.append(("create_terminal", (run_id, kwargs)))
        return {
            "id": "terminal-1",
            "run_id": run_id,
            "status": "open",
            "owner": kwargs.get("owner", "agent"),
            "argv": kwargs.get("argv") or ["/bin/sh"],
            "cwd": kwargs.get("cwd") or "/tmp/run-1",
            "cols": kwargs.get("cols", 120),
            "rows": kwargs.get("rows", 40),
            "pid": 123,
        }

    def get_terminal(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("get_terminal", session_id))
        return {
            "id": session_id,
            "run_id": "run-1",
            "status": "open",
            "owner": "agent",
            "argv": ["/bin/sh"],
            "cwd": "/tmp/run-1",
            "cols": 120,
            "rows": 40,
            "pid": 123,
        }

    def close_terminal(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("close_terminal", session_id))
        return {
            "id": session_id,
            "run_id": "run-1",
            "status": "closed",
            "owner": "agent",
            "argv": ["/bin/sh"],
            "cwd": "/tmp/run-1",
            "cols": 120,
            "rows": 40,
            "pid": 123,
        }

    def register_artifact(
        self,
        run_id: str,
        source_path: str,
        **kwargs: object,
    ) -> dict[str, Any]:
        self.calls.append(("register_artifact", (run_id, source_path, kwargs)))
        return self._artifact("artifact-1", run_id)

    def list_artifacts(self, run_id: str, **kwargs: object) -> dict[str, Any]:
        self.calls.append(("list_artifacts", (run_id, kwargs)))
        return {"items": [self._artifact("artifact-1", run_id)]}

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        self.calls.append(("get_artifact", artifact_id))
        return self._artifact(artifact_id, "run-1")

    def generate_reports(self, run_id: str, **kwargs: object) -> dict[str, Any]:
        self.calls.append(("generate_reports", (run_id, kwargs)))
        return {"items": [self._report("report-1", run_id)]}

    def list_reports(self, run_id: str, **kwargs: object) -> dict[str, Any]:
        self.calls.append(("list_reports", (run_id, kwargs)))
        return {"items": [self._report("report-1", run_id)]}

    def get_report(self, report_id: str) -> dict[str, Any]:
        self.calls.append(("get_report", report_id))
        return self._report(report_id, "run-1")

    @staticmethod
    def _execution(execution_id: str) -> dict[str, Any]:
        return {
            "id": execution_id,
            "execution_key": "execution:v1:test",
            "run_id": "run-1",
            "session_id": "session-1",
            "tool_call_id": "tool-call-1",
            "attempt_group": "initial",
            "node_id": "local",
            "status": "running",
            "argv": ["echo", "ok"],
            "pid": 123,
            "exit_code": None,
        }

    @classmethod
    def _model_profiles_payload(cls) -> dict[str, Any]:
        return {
            "generation": 1,
            "source_digest": "digest",
            "default_profile": "primary",
            "effective_default_profile": "primary",
            "profile_override": None,
            "profiles": [cls._model_profile("primary")],
        }

    @staticmethod
    def _model_profile(profile_name: str) -> dict[str, Any]:
        return {
            "name": profile_name,
            "provider": "openai_compatible",
            "model": f"{profile_name}-model",
            "request_mode": "chat_completions",
            "base_url": "http://models.test/v1",
            "api_key_env": "RIFTX_MODEL_API_KEY",
            "requires_api_key": True,
            "timeout_seconds": 120,
            "max_retries": 2,
            "has_stored_api_key": False,
            "api_key_configured": False,
            "is_default": profile_name == "primary",
            "is_effective_default": profile_name == "primary",
        }

    @staticmethod
    def _node(node_id: str) -> dict[str, Any]:
        return {
            "id": node_id,
            "name": "Runner One",
            "status": "online",
            "platform": "linux",
            "architecture": "x86_64",
            "runner_version": "2.0.0",
            "capabilities": ["scripting"],
            "labels": {},
            "last_seen_at": "2026-07-29T00:00:00Z",
        }

    @staticmethod
    def _report(report_id: str, run_id: str) -> dict[str, Any]:
        return {
            "id": report_id,
            "run_id": run_id,
            "format": "markdown",
            "artifact_id": "artifact-report-1",
            "finding_ids": ["finding-1"],
            "created_at": "2026-07-29T00:00:00Z",
            "content_url": "/api/v1/artifacts/artifact-report-1/content",
        }

    @staticmethod
    def _artifact(artifact_id: str, run_id: str) -> dict[str, Any]:
        return {
            "id": artifact_id,
            "run_id": run_id,
            "name": "scan.txt",
            "mime_type": "text/plain",
            "sha256": "a" * 64,
            "size": 4,
            "execution_id": None,
            "description": "scan",
            "content_url": f"/api/v1/artifacts/{artifact_id}/content",
        }


@pytest.fixture(autouse=True)
def fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAPIClient.instances.clear()
    FakeAPIClient.fail = False
    FakeAPIClient.unhealthy = False
    monkeypatch.setattr(cli_module, "APIClient", FakeAPIClient)


def test_run_create_builds_api_payload() -> None:
    result = runner.invoke(
        cli_module.app,
        [
            "--api-url",
            "http://example:9000",
            "run",
            "create",
            "Inspect service",
            "--engagement",
            "Authorized test",
            "--mode",
            "manual",
            "--model",
            "fast",
            "--success",
            "Identify version",
            "--entry",
            "url=https://example.test",
        ],
    )

    assert result.exit_code == 0, result.output
    normalized_output = " ".join(result.output.split())
    assert "waiting for your first concrete instruction" in normalized_output
    assert 'riftx run message run-1 "YOUR INSTRUCTION"' in result.output
    assert "http://example:9000/runs/run-1" in result.output
    client = FakeAPIClient.instances[0]
    assert client.base_url == "http://example:9000"
    assert client.calls == [
        (
            "create_run",
            {
                "objective": "Inspect service",
                "approval_mode": "manual",
                "model_profile": "fast",
                "success_criteria": [{"description": "Identify version", "required": True}],
                "entry_points": [{"kind": "url", "value": "https://example.test"}],
                "engagement": {"name": "Authorized test"},
            },
        )
    ]


def test_api_error_produces_nonzero_exit() -> None:
    FakeAPIClient.fail = True
    result = runner.invoke(cli_module.app, ["run", "list"])
    assert result.exit_code == 1


def test_api_stop_error_renders_each_execution_node_and_disposition() -> None:
    output = StringIO()
    error = RiftXAPIError(
        status_code=503,
        code="execution_cancel_failed",
        message="Could not confirm every execution stopped",
        details={
            "execution_ids": [
                "execution-stopped",
                "execution-confirmed-no-status",
                "execution-lost",
            ],
            "execution_nodes": {
                "execution-stopped": "local",
                "execution-confirmed-no-status": "local",
                "execution-lost": "remote-1",
            },
            "execution_statuses": {
                "execution-stopped": "cancelled",
                "execution-confirmed-no-status": "cancelled",
                "execution-lost": "lost",
            },
            "confirmed_execution_ids": [
                "execution-stopped",
                "execution-confirmed-no-status",
            ],
            "confirmed_statuses": {"execution-stopped": "cancelled"},
            "failed_executions": {
                "execution-lost": "Runner did not acknowledge process termination"
            },
        },
    )

    render_error(Console(file=output, width=200, color_system=None), error)

    rendered = output.getvalue()
    assert "Safety stop disposition" in rendered
    assert "execution-stopped" in rendered
    assert "local" in rendered
    assert "Stopped (cancelled)" in rendered
    assert "execution-confirmed-no-status" in rendered
    assert "Stop confirmed" in rendered
    assert "execution-lost" in rendered
    assert "remote-1" in rendered
    assert "Stop unconfirmed (lost)" in rendered
    assert "did not acknowledge" in rendered


def test_api_safety_stop_error_renders_only_allowlisted_resource_dispositions() -> None:
    output = StringIO()
    error = RiftXAPIError(
        status_code=503,
        code="safety_stop_failed",
        message="Safety stop was not confirmed",
        details={
            "stop_resources": {
                "executions": {
                    "attempted_ids": ["execution-1"],
                    "node_ids": {"execution-1": "local"},
                    "observed_statuses": {"execution-1": "cancelled"},
                    "confirmed_ids": ["execution-1"],
                    "confirmed_statuses": {"execution-1": "cancelled"},
                    "failures": {},
                    "succeeded": True,
                    "diagnostics": "do-not-render-diagnostics",
                },
                "browser_sessions": {
                    "attempted_ids": ["browser-1"],
                    "node_ids": {"browser-1": "remote-browser"},
                    "observed_statuses": {"browser-1": "open"},
                    "confirmed_ids": [],
                    "confirmed_statuses": {},
                    "failures": {"browser-1": "Browser process did not acknowledge close"},
                    "succeeded": False,
                },
                "target_http_requests": {
                    "attempted_ids": ["request-1"],
                    "node_ids": {},
                    "observed_statuses": {"request-1": "aborted"},
                    "confirmed_ids": [],
                    "confirmed_statuses": {},
                    "failures": {},
                    "succeeded": True,
                },
                "unknown_resource": {
                    "attempted_ids": ["do-not-render-resource"],
                },
            },
            "internal_reason": "do-not-render-internal-reason",
        },
    )

    render_error(Console(file=output, width=240, color_system=None), error)

    rendered = output.getvalue()
    assert "Safety stop disposition" in rendered
    assert "Execution" in rendered
    assert "execution-1" in rendered
    assert "Stopped (cancelled)" in rendered
    assert "Browser session" in rendered
    assert "browser-1" in rendered
    assert "Stop unconfirmed (open)" in rendered
    assert "did not acknowledge close" in rendered
    assert "Target HTTP request" in rendered
    assert "request-1" in rendered
    assert "Stop confirmed" in rendered
    assert "do-not-render-resource" not in rendered
    assert "do-not-render-diagnostics" not in rendered
    assert "do-not-render-internal-reason" not in rendered


def test_run_cancel_delegates_to_shared_http_client() -> None:
    result = runner.invoke(cli_module.app, ["run", "cancel", "run-1"])

    assert result.exit_code == 0, result.output
    assert "Run cancellation confirmed; active effects stopped." in result.output
    assert FakeAPIClient.instances[0].calls == [("cancel_run", "run-1")]


def test_run_pause_reports_confirmed_stop() -> None:
    result = runner.invoke(cli_module.app, ["run", "pause", "run-1"])

    assert result.exit_code == 0, result.output
    assert "Pause confirmed; active effects stopped." in result.output
    assert FakeAPIClient.instances[0].calls == [("pause_run", "run-1")]


def test_cancel_current_reports_confirmed_execution_stop() -> None:
    result = runner.invoke(cli_module.app, ["run", "cancel-current", "run-1"])

    assert result.exit_code == 0, result.output
    assert "Current execution stop confirmed." in result.output
    assert FakeAPIClient.instances[0].calls == [("cancel_current_execution", "run-1")]


def test_run_metrics_uses_shared_observability_endpoint() -> None:
    result = runner.invoke(cli_module.app, ["run", "metrics", "run-1"])

    assert result.exit_code == 0, result.output
    assert "Runtime Metrics" in result.output
    assert FakeAPIClient.instances[0].calls == [("get_run_metrics", "run-1")]


def test_run_compact_delegates_to_shared_http_client() -> None:
    result = runner.invoke(
        cli_module.app,
        ["run", "compact", "run-1", "--max-items", "25"],
    )

    assert result.exit_code == 0, result.output
    assert FakeAPIClient.instances[0].calls == [("compact_run", ("run-1", 25))]


def test_run_model_switch_delegates_to_shared_http_client() -> None:
    result = runner.invoke(cli_module.app, ["run", "model", "run-1", "deep"])

    assert result.exit_code == 0, result.output
    assert FakeAPIClient.instances[0].calls == [("switch_run_model", ("run-1", "deep"))]


def test_model_commands_delegate_to_profile_endpoints() -> None:
    listed = runner.invoke(cli_module.app, ["model", "list"])
    shown = runner.invoke(cli_module.app, ["model", "show", "primary"])
    selected = runner.invoke(cli_module.app, ["model", "default", "fast"])
    removed = runner.invoke(cli_module.app, ["model", "remove", "old", "--yes"])

    for result in (listed, shown, selected, removed):
        assert result.exit_code == 0, result.output
    assert "Requires API key" in shown.output
    assert "Timeout (seconds)" in shown.output
    assert "120" in shown.output
    assert "Max retries" in shown.output
    assert "2" in shown.output
    assert FakeAPIClient.instances[0].calls == [("list_model_profiles", None)]
    assert FakeAPIClient.instances[1].calls == [("get_model_profile", "primary")]
    assert FakeAPIClient.instances[2].calls == [("set_default_model_profile", "fast")]
    assert FakeAPIClient.instances[3].calls == [("delete_model_profile", "old")]


def test_model_configure_reads_api_key_from_stdin_without_printing_it() -> None:
    result = runner.invoke(
        cli_module.app,
        [
            "model",
            "configure",
            "lab",
            "--model",
            "lab-model",
            "--provider",
            "openai_compatible",
            "--request-mode",
            "responses",
            "--base-url",
            "https://models.test/v1",
            "--api-key-env",
            "RIFTX_MODEL_LAB_KEY",
            "--timeout",
            "30",
            "--max-retries",
            "4",
            "--api-key-stdin",
        ],
        input="stdin-secret-value\n",
    )

    assert result.exit_code == 0, result.output
    assert "stdin-secret-value" not in result.output
    assert FakeAPIClient.instances[0].calls == [
        (
            "configure_model_profile",
            (
                "lab",
                {
                    "provider": "openai_compatible",
                    "model": "lab-model",
                    "request_mode": "responses",
                    "base_url": "https://models.test/v1",
                    "api_key_env": "RIFTX_MODEL_LAB_KEY",
                    "requires_api_key": True,
                    "timeout_seconds": 30.0,
                    "max_retries": 4,
                    "clear_stored_api_key": False,
                    "api_key": "stdin-secret-value",
                },
            ),
        )
    ]


def test_model_configure_hidden_prompt_does_not_echo_api_key() -> None:
    result = runner.invoke(
        cli_module.app,
        [
            "model",
            "configure",
            "lab",
            "--model",
            "lab-model",
            "--base-url",
            "https://models.test/v1",
            "--api-key-prompt",
        ],
        input="prompt-secret-value\n",
    )

    assert result.exit_code == 0, result.output
    assert "prompt-secret-value" not in result.output
    payload = FakeAPIClient.instances[0].calls[0][1][1]
    assert payload["api_key"] == "prompt-secret-value"


def test_model_configure_has_no_plaintext_api_key_option() -> None:
    result = runner.invoke(
        cli_module.app,
        [
            "model",
            "configure",
            "lab",
            "--model",
            "lab-model",
            "--api-key",
            "visible-secret-value",
        ],
    )

    assert result.exit_code == 2
    assert "visible-secret-value" not in result.output
    assert FakeAPIClient.instances == []


@pytest.mark.parametrize(
    ("option", "value", "expected_message"),
    [
        (
            "--api-key-env",
            "AWS_SECRET_ACCESS_KEY",
            "RIFTX_MODEL_",
        ),
        (
            "--base-url",
            "http://169.254.169.254/v1",
            "must not target a link-local",
        ),
        (
            "--base-url",
            "https://${AWS_SECRET_ACCESS_KEY}.capture.example/v1",
            "environment references",
        ),
    ],
)
def test_model_configure_rejects_remote_secret_and_endpoint_escalation(
    option: str,
    value: str,
    expected_message: str,
) -> None:
    arguments = ["model", "configure", "lab", "--model", "lab-model"]
    if option != "--base-url":
        arguments.extend(("--base-url", "https://models.test/v1"))
    arguments.extend((option, value))
    result = runner.invoke(
        cli_module.app,
        arguments,
        terminal_width=200,
    )

    assert result.exit_code == 2
    assert expected_message in result.output
    assert FakeAPIClient.instances == []


@pytest.mark.parametrize("timeout", ["nan", "inf", "0", "600.01"])
def test_model_configure_rejects_unsafe_timeout_locally(timeout: str) -> None:
    result = runner.invoke(
        cli_module.app,
        [
            "model",
            "configure",
            "lab",
            "--model",
            "lab-model",
            "--base-url",
            "https://models.test/v1",
            "--timeout",
            timeout,
        ],
        terminal_width=200,
    )

    assert result.exit_code == 2
    assert "timeout must be finite" in result.output
    assert "600 seconds" in result.output
    assert FakeAPIClient.instances == []


def test_model_configure_requires_base_url_for_compatible_provider() -> None:
    result = runner.invoke(
        cli_module.app,
        ["model", "configure", "lab", "--model", "lab-model"],
        terminal_width=200,
    )

    assert result.exit_code == 2
    assert "explicit base_url" in result.output
    assert FakeAPIClient.instances == []


def test_model_configure_allows_openai_without_base_url() -> None:
    result = runner.invoke(
        cli_module.app,
        [
            "model",
            "configure",
            "openai",
            "--provider",
            "openai",
            "--model",
            "openai-model",
            "--no-api-key",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = FakeAPIClient.instances[0].calls[0][1][1]
    assert payload["provider"] == "openai"
    assert payload["base_url"] is None
    assert payload["request_mode"] == "chat_completions"


def test_model_configure_does_not_read_key_input_when_credentials_are_disabled() -> None:
    result = runner.invoke(
        cli_module.app,
        [
            "model",
            "configure",
            "local",
            "--model",
            "local-model",
            "--base-url",
            "http://127.0.0.1:11434/v1",
            "--no-api-key",
            "--api-key-stdin",
        ],
        input="must-not-be-read\n",
    )

    assert result.exit_code == 2
    assert "must-not-be-read" not in result.output
    assert "API key input cannot be used" in result.output
    assert "--no-api-key" in result.output
    assert FakeAPIClient.instances == []


def test_memory_commands_delegate_to_shared_http_client() -> None:
    created = runner.invoke(
        cli_module.app,
        [
            "memory",
            "create",
            "procedural",
            "node",
            "node-1",
            "Nuclei WAF",
            "Lower the rate limit",
            "--source",
            "user://messages/message-1",
            "--keyword",
            "nuclei",
            "--pin",
        ],
    )
    listed = runner.invoke(
        cli_module.app,
        ["memory", "list", "--scope", "node", "--scope-id", "node-1"],
    )
    edited = runner.invoke(
        cli_module.app,
        ["memory", "edit", "memory-1", "--summary", "Updated"],
    )
    pinned = runner.invoke(cli_module.app, ["memory", "pin", "memory-1", "--off"])
    deleted = runner.invoke(cli_module.app, ["memory", "forget", "memory-1"])

    for result in (created, listed, edited, pinned, deleted):
        assert result.exit_code == 0, result.output
    assert FakeAPIClient.instances[0].calls[0][0] == "create_memory"
    assert FakeAPIClient.instances[1].calls == [
        (
            "list_memories",
            {
                "scope_type": "node",
                "scope_id": "node-1",
                "include_inactive": False,
            },
        )
    ]
    assert FakeAPIClient.instances[2].calls == [
        ("update_memory", ("memory-1", {"summary": "Updated"}))
    ]
    assert FakeAPIClient.instances[3].calls == [("pin_memory", ("memory-1", False))]
    assert FakeAPIClient.instances[4].calls == [("delete_memory", "memory-1")]


def test_tools_doctor_fails_for_enabled_unavailable_tool() -> None:
    FakeAPIClient.unhealthy = True
    result = runner.invoke(cli_module.app, ["tools", "doctor", "--node", "local"])
    assert result.exit_code == 1


def test_approval_commands_delegate_to_shared_http_client() -> None:
    listed = runner.invoke(cli_module.app, ["approvals", "run-1"])
    approved = runner.invoke(cli_module.app, ["approve", "approval-1", "--for-run"])
    rejected = runner.invoke(
        cli_module.app,
        ["reject", "approval-2", "--reason", "Outside scope"],
    )

    assert listed.exit_code == 0, listed.output
    assert approved.exit_code == 0, approved.output
    assert rejected.exit_code == 0, rejected.output
    assert FakeAPIClient.instances[0].calls == [("list_approvals", "run-1")]
    assert FakeAPIClient.instances[1].calls == [("approve", ("approval-1", True))]
    assert FakeAPIClient.instances[2].calls == [("reject", ("approval-2", "Outside scope"))]


def test_terminal_commands_delegate_to_shared_control_plane() -> None:
    created = runner.invoke(
        cli_module.app,
        [
            "terminal",
            "create",
            "run-1",
            "--cwd",
            "/tmp/run-1",
            "--cols",
            "132",
            "--rows",
            "48",
            "--",
            "python",
            "-i",
        ],
    )
    shown = runner.invoke(cli_module.app, ["terminal", "show", "terminal-1"])
    closed = runner.invoke(cli_module.app, ["terminal", "close", "terminal-1"])

    assert created.exit_code == 0, created.output
    assert shown.exit_code == 0, shown.output
    assert closed.exit_code == 0, closed.output
    assert FakeAPIClient.instances[0].calls == [
        (
            "create_terminal",
            (
                "run-1",
                {
                    "argv": ["python", "-i"],
                    "cwd": "/tmp/run-1",
                    "cols": 132,
                    "rows": 48,
                    "owner": "agent",
                },
            ),
        )
    ]
    assert FakeAPIClient.instances[1].calls == [("get_terminal", "terminal-1")]
    assert FakeAPIClient.instances[2].calls == [("close_terminal", "terminal-1")]


def test_report_commands_delegate_to_shared_control_plane() -> None:
    generated = runner.invoke(
        cli_module.app,
        ["report", "generate", "run-1", "--format", "markdown", "--format", "json"],
    )
    listed = runner.invoke(
        cli_module.app,
        ["report", "list", "run-1", "--format", "markdown", "--limit", "10"],
    )
    shown = runner.invoke(cli_module.app, ["report", "show", "report-1"])

    assert generated.exit_code == 0, generated.output
    assert listed.exit_code == 0, listed.output
    assert shown.exit_code == 0, shown.output
    assert FakeAPIClient.instances[0].calls == [
        ("generate_reports", ("run-1", {"formats": ["markdown", "json"]}))
    ]
    assert FakeAPIClient.instances[1].calls == [
        (
            "list_reports",
            ("run-1", {"format": "markdown", "limit": 10, "offset": 0}),
        )
    ]
    assert FakeAPIClient.instances[2].calls == [("get_report", "report-1")]


def test_artifact_commands_delegate_to_shared_control_plane() -> None:
    registered = runner.invoke(
        cli_module.app,
        [
            "artifact",
            "register",
            "run-1",
            "/tmp/run-1/scan.txt",
            "--name",
            "evidence.txt",
            "--mime-type",
            "text/plain",
            "--description",
            "scan output",
            "--execution-id",
            "execution-1",
        ],
    )
    listed = runner.invoke(
        cli_module.app,
        ["artifact", "list", "run-1", "--execution-id", "execution-1"],
    )
    shown = runner.invoke(cli_module.app, ["artifact", "show", "artifact-1"])

    assert registered.exit_code == 0, registered.output
    assert listed.exit_code == 0, listed.output
    assert shown.exit_code == 0, shown.output
    assert FakeAPIClient.instances[0].calls == [
        (
            "register_artifact",
            (
                "run-1",
                "/tmp/run-1/scan.txt",
                {
                    "name": "evidence.txt",
                    "mime_type": "text/plain",
                    "description": "scan output",
                    "execution_id": "execution-1",
                },
            ),
        )
    ]
    assert FakeAPIClient.instances[1].calls == [
        (
            "list_artifacts",
            (
                "run-1",
                {"execution_id": "execution-1", "limit": 100, "offset": 0},
            ),
        )
    ]
    assert FakeAPIClient.instances[2].calls == [("get_artifact", "artifact-1")]


def test_execution_commands_query_and_cancel_durable_execution() -> None:
    shown = runner.invoke(cli_module.app, ["execution", "show", "execution-1"])
    listed = runner.invoke(cli_module.app, ["execution", "list", "--run", "run-1"])
    waited = runner.invoke(
        cli_module.app,
        ["execution", "wait", "execution-1", "--timeout", "0.5"],
    )
    cancelled = runner.invoke(cli_module.app, ["execution", "cancel", "execution-1"])

    assert shown.exit_code == 0, shown.output
    assert listed.exit_code == 0, listed.output
    assert waited.exit_code == 0, waited.output
    assert "wait_timeout" in waited.output.lower()
    assert cancelled.exit_code == 0, cancelled.output
    assert FakeAPIClient.instances[0].calls == [("get_execution", "execution-1")]
    assert FakeAPIClient.instances[1].calls == [("list_executions", ("run-1", 100, 0))]
    assert FakeAPIClient.instances[2].calls == [
        ("wait_execution", ("execution-1", 0.5, 0, 0, 65536, 10))
    ]
    assert FakeAPIClient.instances[3].calls == [("cancel_execution", "execution-1")]


def test_node_commands_delegate_to_shared_control_plane() -> None:
    listed = runner.invoke(cli_module.app, ["node", "list", "--status", "online"])
    shown = runner.invoke(cli_module.app, ["node", "show", "node-1"])
    disconnected = runner.invoke(cli_module.app, ["node", "disconnect", "node-1"])

    assert listed.exit_code == 0, listed.output
    assert shown.exit_code == 0, shown.output
    assert disconnected.exit_code == 0, disconnected.output
    assert FakeAPIClient.instances[0].calls == [("list_nodes", "online")]
    assert FakeAPIClient.instances[1].calls == [("get_node", "node-1")]
    assert FakeAPIClient.instances[2].calls == [("disconnect_node", "node-1")]


def test_cli_loads_explicit_config_and_derives_api_url(tmp_path: Any) -> None:
    config_path = tmp_path / "riftx.yaml"
    config_path.write_text("server:\n  host: config.test\n  port: 9443\n")

    result = runner.invoke(
        cli_module.app,
        ["--config", str(config_path), "run", "list"],
    )

    assert result.exit_code == 0, result.output
    assert FakeAPIClient.instances[0].base_url == "http://config.test:9443"


def test_serve_applies_cli_overrides_after_config(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RIFTX_ADMIN_TOKEN",
        "test-only-local-operator-token-0001",
    )
    config_path = tmp_path / "riftx.yaml"
    config_path.write_text(
        "server:\n  host: 127.0.0.1\n  port: 9000\n"
        "security:\n"
        "  trust_profile: local_single_operator\n"
        f"  local_principal_path: {tmp_path / 'local-principal.json'}\n"
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli_module.uvicorn,
        "run",
        lambda application, **kwargs: calls.append({"application": application, **kwargs}),
    )

    result = runner.invoke(
        cli_module.app,
        ["--config", str(config_path), "serve", "--port", "9001"],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 9001


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "control.test"])
def test_serve_rejects_non_loopback_bind_without_trusted_proxy(
    host: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli_module.uvicorn,
        "run",
        lambda application, **kwargs: calls.append({"application": application, **kwargs}),
    )

    result = runner.invoke(
        cli_module.app,
        ["serve", "--host", host],
        env={
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_LOCAL_PRINCIPAL_PATH": str(tmp_path / "local-principal.json"),
            "RIFTX_ADMIN_TOKEN": "test-only-local-operator-token-0001",
        },
    )

    assert result.exit_code == 2
    assert "local_profile_requires_loopback" in result.output
    assert "要求监听回环地址" in result.output
    assert calls == []


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_serve_accepts_loopback_bind(
    host: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli_module.uvicorn,
        "run",
        lambda application, **kwargs: calls.append({"application": application, **kwargs}),
    )

    result = runner.invoke(
        cli_module.app,
        ["serve", "--host", host],
        env={
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_LOCAL_PRINCIPAL_PATH": str(tmp_path / "local-principal.json"),
            "RIFTX_ADMIN_TOKEN": "test-only-local-operator-token-0001",
        },
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["host"] == host


def test_serve_rejects_weak_operator_credential_before_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli_module.uvicorn,
        "run",
        lambda application, **kwargs: calls.append({"application": application, **kwargs}),
    )

    result = runner.invoke(
        cli_module.app,
        ["serve"],
        env={
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_LOCAL_PRINCIPAL_PATH": str(tmp_path / "local-principal.json"),
            "RIFTX_ADMIN_TOKEN": "short-test-token",
        },
    )

    assert result.exit_code == 2
    assert "local_operator_credential_weak" in result.output
    assert "至少包含 32 个" in result.output
    assert calls == []


def test_tools_show_filters_registry_response() -> None:
    result = runner.invoke(cli_module.app, ["tools", "show", "python", "--node", "node-1"])

    assert result.exit_code == 0, result.output
    assert FakeAPIClient.instances[0].calls == [("list_tools", "node-1")]
    assert "python" in result.output


def test_tools_show_fails_for_unknown_tool() -> None:
    result = runner.invoke(cli_module.app, ["tools", "show", "missing"])

    assert result.exit_code == 2
    assert "was not found" in result.output


def test_worker_command_builds_and_runs_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    class FakeRuntime:
        async def run(self) -> None:
            calls.append("run")

    async def fake_build(config: object) -> FakeRuntime:
        calls.append(config)
        return FakeRuntime()

    monkeypatch.setattr(cli_module, "build_temporal_worker", fake_build)

    result = runner.invoke(cli_module.app, ["worker"])

    assert result.exit_code == 0, result.output
    assert isinstance(calls[0], cli_module.RiftXConfig)
    assert calls[1] == "run"


def test_runner_command_help_omits_registration_token_option() -> None:
    result = runner.invoke(
        cli_module.app,
        ["runner", "--help"],
        terminal_width=200,
    )

    assert result.exit_code == 0, result.output
    assert "--registration-t" not in result.output


def test_runner_command_rejects_registration_token_argv_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[cli_module.RunnerDaemonConfig] = []
    canary = "shared-cli-bootstrap-canary-never-log-0001"

    async def fake_run(config: cli_module.RunnerDaemonConfig) -> None:
        calls.append(config)

    monkeypatch.setattr(cli_module, "run_runner_daemon", fake_run)
    result = runner.invoke(
        cli_module.app,
        ["runner", "--registration-token", canary],
    )

    assert result.exit_code == 2
    assert "No such option: --registration-token" in result.output
    assert canary not in result.output
    assert calls == []


def test_runner_command_applies_cli_overrides_and_environment_bootstrap_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[cli_module.RunnerDaemonConfig] = []
    bootstrap_token = "test-only-runner-bootstrap-token-0004"

    async def fake_run(config: cli_module.RunnerDaemonConfig) -> None:
        calls.append(config)

    monkeypatch.setattr(cli_module, "run_runner_daemon", fake_run)
    state_path = tmp_path / "runner-state"
    credential_path = tmp_path / "secrets" / "runner-credentials.json"

    result = runner.invoke(
        cli_module.app,
        [
            "runner",
            "--server-url",
            "http://control.test:8787",
            "--node-id",
            "node-7",
            "--name",
            "Runner Seven",
            "--state-path",
            str(state_path),
            "--credential-path",
            str(credential_path),
        ],
        env={"RIFTX_RUNNER_REGISTRATION_TOKEN": bootstrap_token},
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        cli_module.RunnerDaemonConfig(
            server_url="http://control.test:8787",
            node_id="node-7",
            name="Runner Seven",
            state_path=state_path,
            credential_path=credential_path,
            registration_token=bootstrap_token,
        )
    ]


@pytest.mark.parametrize("option", ["--state-path", "--credential-path"])
def test_runner_path_override_is_rejected_when_audit_sources_are_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    option: str,
) -> None:
    calls: list[cli_module.RunnerDaemonConfig] = []

    async def fake_run(config: cli_module.RunnerDaemonConfig) -> None:
        calls.append(config)

    monkeypatch.setattr(cli_module, "run_runner_daemon", fake_run)
    source = tmp_path / "source"
    source.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    config_path = tmp_path / "riftx.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "database": {
                    "url": f"sqlite+aiosqlite:///{state / 'riftx.db'}",
                },
                "workspace": {"root": str(state / "workspaces")},
                "runner": {
                    "state_path": str(state / "runner"),
                    "credential_path": str(state / "runner-credentials.json"),
                },
                "models": {"secrets_path": str(state / "models.json")},
                "security": {"local_principal_path": str(state / "principal.json")},
                "audit": {
                    "source_roots": [str(source)],
                    "snapshot_root": str(state / "snapshots"),
                    "temp_root": str(state / "tmp"),
                    "fix_root": str(state / "fixes"),
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        cli_module.app,
        [
            "--config",
            str(config_path),
            "runner",
            option,
            str(source / "forbidden-storage"),
        ],
    )

    assert result.exit_code == 2
    assert "deployment-owned when Audit source roots are configured" in result.output
    assert str(source) not in result.output
    assert calls == []


def test_web_command_prints_and_optionally_opens_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(cli_module.webbrowser, "open", lambda url: opened.append(url))

    result = runner.invoke(
        cli_module.app,
        ["--api-url", "http://control.test:8787/", "web"],
    )
    no_open = runner.invoke(
        cli_module.app,
        ["--api-url", "http://control.test:8787", "web", "--no-open"],
    )

    assert result.exit_code == 0, result.output
    assert no_open.exit_code == 0, no_open.output
    assert "http://control.test:8787/" in result.output
    assert opened == ["http://control.test:8787/"]


def test_cli_language_option_and_environment_switch_output() -> None:
    chinese = runner.invoke(cli_module.app, ["--language", "zh", "run", "list"])
    environment = runner.invoke(
        cli_module.app,
        ["run", "list"],
        env={"RIFTX_LANGUAGE": "zh-CN"},
    )
    english = runner.invoke(cli_module.app, ["--language", "en", "run", "list"])
    invalid = runner.invoke(cli_module.app, ["--language", "fr", "run", "list"])

    assert chinese.exit_code == 0, chinese.output
    assert "未找到任务" in chinese.output
    assert environment.exit_code == 0, environment.output
    assert "未找到任务" in environment.output
    assert english.exit_code == 0, english.output
    assert "No runs found" in english.output
    assert invalid.exit_code == 2
