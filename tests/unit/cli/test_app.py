"""Typer command tests proving the CLI delegates to the HTTP client."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

import riftx.cli.app as cli_module
from riftx.cli.client import RiftXAPIError

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
            "--success",
            "Identify version",
            "--entry",
            "url=https://example.test",
        ],
    )

    assert result.exit_code == 0, result.output
    client = FakeAPIClient.instances[0]
    assert client.base_url == "http://example:9000"
    assert client.calls == [
        (
            "create_run",
            {
                "objective": "Inspect service",
                "approval_mode": "manual",
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
