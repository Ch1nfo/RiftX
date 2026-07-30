from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import yaml

from riftx.domain import ToolAvailability
from riftx.tools import ToolRegistry, ToolUnavailableError

FIXTURE = Path(__file__).parents[2] / "tools" / "fixtures" / "fake_tool.py"


def write_config(path: Path, tools: dict[str, object]) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": "open",
                "tools": tools,
            },
            sort_keys=False,
        )
    )


def custom_tool(*, capabilities: list[str] | None = None) -> dict[str, object]:
    return {
        "enabled": True,
        "command": [sys.executable, str(FIXTURE)],
        "executor": "process",
        "capabilities": capabilities or ["custom_verification"],
        "version_probe": {"command": [sys.executable, str(FIXTURE), "--version"]},
        "approval": "sensitive",
        "timeout": 30,
        "environment": {"PYTHONUNBUFFERED": "1"},
    }


async def test_registry_probes_and_exposes_only_available_tools(tmp_path: Path) -> None:
    config_path = tmp_path / "tools.yaml"
    write_config(
        config_path,
        {
            "custom": custom_tool(),
            "missing": {
                "command": ["definitely-not-a-riftx-test-command"],
                "capabilities": ["custom_verification"],
            },
            "disabled": {
                "enabled": False,
                "command": ["also-missing"],
                "capabilities": ["custom_verification"],
            },
        },
    )
    registry = ToolRegistry(config_path, node_id="node-1")

    snapshot = await registry.refresh()

    assert snapshot.states["custom"].availability is ToolAvailability.AVAILABLE
    assert snapshot.states["custom"].version == "fake-tool 1.2.3"
    assert snapshot.states["missing"].availability is ToolAvailability.UNAVAILABLE
    assert snapshot.states["disabled"].availability is ToolAvailability.DISABLED
    assert [tool.id for tool in registry.available_tools()] == ["custom"]
    assert [tool.id for tool in registry.find_by_capability("custom_verification")] == ["custom"]
    assert registry.build_argv("custom", ["--target", "example.test"]) == [
        sys.executable,
        str(FIXTURE),
        "--target",
        "example.test",
    ]
    with pytest.raises(ToolUnavailableError):
        registry.get_available("missing")


async def test_registry_supports_path_commands(tmp_path: Path) -> None:
    config_path = tmp_path / "tools.yaml"
    executable_name = Path(sys.executable).name
    write_config(
        config_path,
        {"python": {"command": [executable_name], "capabilities": ["python"]}},
    )
    registry = ToolRegistry(
        config_path,
        node_id="node-1",
        host_environment={"PATH": str(Path(sys.executable).parent)},
    )

    snapshot = await registry.refresh()

    assert snapshot.states["python"].availability is ToolAvailability.AVAILABLE
    assert snapshot.states["python"].resolved_command is not None


async def test_registry_marks_missing_script_prefix_misconfigured(tmp_path: Path) -> None:
    config_path = tmp_path / "tools.yaml"
    missing_script = tmp_path / "missing.py"
    write_config(
        config_path,
        {
            "custom": {
                "command": [sys.executable, str(missing_script)],
                "capabilities": ["custom"],
            }
        },
    )

    snapshot = await ToolRegistry(config_path, node_id="node-1").refresh()

    assert snapshot.states["custom"].availability is ToolAvailability.MISCONFIGURED
    assert "prefix path does not exist" in (snapshot.states["custom"].reason or "")


async def test_registry_hot_reload_is_digest_based(tmp_path: Path) -> None:
    config_path = tmp_path / "tools.yaml"
    write_config(config_path, {"first": custom_tool(capabilities=["first"])})
    registry = ToolRegistry(config_path, node_id="node-1")
    first = await registry.refresh()

    unchanged = await registry.reload_if_changed()
    write_config(
        config_path,
        {
            "first": custom_tool(capabilities=["first"]),
            "second": custom_tool(capabilities=["second"]),
        },
    )
    os.utime(config_path, None)
    changed = await registry.reload_if_changed()

    assert unchanged is first
    assert changed.generation == first.generation + 1
    assert set(changed.definitions) == {"first", "second"}


async def test_version_probe_timeout_marks_tool_unavailable(tmp_path: Path) -> None:
    config_path = tmp_path / "tools.yaml"
    tool = custom_tool()
    tool["version_probe"] = {
        "command": [sys.executable, str(FIXTURE), "--slow-version"],
        "timeout_seconds": 0.05,
    }
    write_config(config_path, {"slow": tool})

    snapshot = await ToolRegistry(config_path, node_id="node-1").refresh()

    state = snapshot.states["slow"]
    assert state.availability is ToolAvailability.UNAVAILABLE
    assert "timed out" in (state.reason or "")


async def test_registry_atomically_updates_and_reloads_tool_definition(tmp_path: Path) -> None:
    from riftx.tools import RawToolDefinition

    config_path = tmp_path / "tools.yaml"
    write_config(config_path, {"custom": custom_tool(capabilities=["first"])})
    registry = ToolRegistry(config_path, node_id="node-1")
    first = await registry.refresh()

    updated = await registry.update_tool(
        "custom",
        RawToolDefinition(
            enabled=False,
            command=[sys.executable, str(FIXTURE)],
            capabilities=["second", "structured"],
            timeout=42,
            environment={"RIFTX_EDITED": "1"},
        ),
    )
    persisted = yaml.safe_load(config_path.read_text())

    assert updated.generation == first.generation + 1
    assert updated.definitions["custom"].capabilities == ["second", "structured"]
    assert updated.states["custom"].availability is ToolAvailability.DISABLED
    assert persisted["tools"]["custom"]["timeout"] == 42
    assert persisted["tools"]["custom"]["environment"] == {"RIFTX_EDITED": "1"}
    temporary_files = await asyncio.to_thread(lambda: list(tmp_path.glob(".tools.yaml.*.tmp")))
    assert not temporary_files
