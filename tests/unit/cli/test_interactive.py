from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from rich.console import Console

from riftx.cli import interactive
from riftx.cli.interactive import InteractiveState


class FakeClient:
    base_url = "http://control.test:8787"

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def create_run(self, payload: dict[str, object]) -> dict[str, Any]:
        self.calls.append(("create_run", payload))
        return {
            "id": "run-1",
            "node_id": payload.get("node_id", "local"),
            "model_profile": payload.get("model_profile"),
            "approval_mode": payload.get("approval_mode", "balanced"),
            "objective": {"description": payload["objective"]},
            "status": "created",
            "workspace_path": "/tmp/run-1",
            "temporal_workflow_id": "workflow-run-1",
        }

    def get_run(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("get_run", run_id))
        return {
            "id": run_id,
            "node_id": "node-2",
            "model_profile": "fast",
            "approval_mode": "manual",
            "objective": {"description": "Existing run"},
            "status": "running",
            "workspace_path": "/tmp/run-2",
            "temporal_workflow_id": "workflow-run-2",
        }

    def list_nodes(self) -> dict[str, Any]:
        self.calls.append(("list_nodes", None))
        return {"items": [self.get_node("node-2")]}

    def get_node(self, node_id: str) -> dict[str, Any]:
        self.calls.append(("get_node", node_id))
        return {
            "id": node_id,
            "name": "Runner Two",
            "status": "online",
            "platform": "linux",
            "architecture": "x86_64",
            "runner_version": "2.0.0",
            "capabilities": [],
            "labels": {},
            "last_seen_at": "2026-07-30T00:00:00Z",
        }

    def list_events(self, run_id: str, **kwargs: object) -> dict[str, Any]:
        self.calls.append(("list_events", (run_id, kwargs)))
        return {
            "items": [
                {
                    "event_type": "agent.plan_updated",
                    "payload": {"plan_summary": "Inspect, verify, and report."},
                }
            ]
        }

    def get_run_context(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("get_run_context", run_id))
        return {
            "id": "compilation-1",
            "model_profile": "gpt-test",
            "estimated_tokens": 42,
            "actual_input_tokens": 40,
            "actual_output_tokens": 8,
            "manifest": {"categories": {}},
        }

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("cancel_run", run_id))
        return {"accepted": True}

    def compact_run(self, run_id: str, *, max_history_items: int = 100) -> dict[str, Any]:
        self.calls.append(("compact_run", (run_id, max_history_items)))
        return {"accepted": True}


def make_console() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, width=120), output


def test_interactive_defaults_are_applied_to_new_run() -> None:
    client = FakeClient()
    state = InteractiveState()
    console, _ = make_console()

    assert interactive._handle_command("/node node-2", state, client, console) is False
    assert interactive._handle_command("/model fast", state, client, console) is False
    assert interactive._handle_command("/mode manual", state, client, console) is False
    assert interactive._handle_command("/new Inspect target", state, client, console) is False

    assert state.active_run_id == "run-1"
    assert client.calls[-1] == (
        "create_run",
        {
            "objective": "Inspect target",
            "node_id": "node-2",
            "approval_mode": "manual",
            "model_profile": "fast",
        },
    )


def test_interactive_resume_restores_run_selection_defaults() -> None:
    client = FakeClient()
    state = InteractiveState()
    console, _ = make_console()

    interactive._handle_command("/resume run-2", state, client, console)

    assert state.active_run_id == "run-2"
    assert state.node_id == "node-2"
    assert state.model_profile == "fast"
    assert state.approval_mode == "manual"


def test_interactive_plan_cancel_compact_and_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    state = InteractiveState(active_run_id="run-1")
    console, output = make_console()
    opened: list[str] = []
    monkeypatch.setattr(interactive.webbrowser, "open", lambda url: opened.append(url))

    interactive._handle_command("/plan", state, client, console)
    interactive._handle_command("/compact 25", state, client, console)
    interactive._handle_command("/cancel", state, client, console)
    interactive._handle_command("/web", state, client, console)

    assert ("compact_run", ("run-1", 25)) in client.calls
    assert ("cancel_run", "run-1") in client.calls
    assert "Inspect, verify, and report." in output.getvalue()
    assert opened == ["http://control.test:8787/runs/run-1"]


def test_interactive_context_inspector_uses_active_run() -> None:
    client = FakeClient()
    state = InteractiveState(active_run_id="run-1")
    console, output = make_console()

    interactive._handle_command("/context", state, client, console)

    assert ("get_run_context", "run-1") in client.calls
    assert "Context Inspector" in output.getvalue()
    assert "Tool Schemas" in output.getvalue()


def test_interactive_rejects_invalid_mode_and_compaction_limit() -> None:
    client = FakeClient()
    state = InteractiveState(active_run_id="run-1")
    console, _ = make_console()

    with pytest.raises(ValueError, match="auto.*balanced.*manual"):
        interactive._handle_command("/mode risky", state, client, console)
    with pytest.raises(ValueError, match="MAX_ITEMS"):
        interactive._handle_command("/compact 0", state, client, console)


def test_interactive_help_uses_selected_language() -> None:
    from riftx.cli.i18n import set_language

    client = FakeClient()
    state = InteractiveState()
    console, output = make_console()
    try:
        set_language("zh")
        interactive._handle_command("/help", state, client, console)
        assert "创建任务" in output.getvalue()
        assert "退出交互模式" in output.getvalue()
    finally:
        set_language("en")
