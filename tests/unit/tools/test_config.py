from pathlib import Path

import pytest

from riftx.domain import ApprovalLevel, ExecutorType
from riftx.tools import ExecutionPolicy, ToolConfigError, load_tool_config, parse_tool_config


def test_parse_tool_config_applies_defaults_and_normalizes_capabilities() -> None:
    config = parse_tool_config(
        """
version: 1
tools:
  scanner:
    command: [scanner]
    capabilities: [port_scan, " port_scan ", ""]
"""
    )

    tool = config.tools["scanner"]
    assert config.execution_policy is ExecutionPolicy.REGISTERED_ONLY
    assert tool.executor is ExecutorType.PROCESS
    assert tool.approval is ApprovalLevel.NEVER
    assert tool.capabilities == ["port_scan"]
    assert tool.timeout == 1800


def test_parse_tool_config_rejects_unknown_fields() -> None:
    with pytest.raises(ToolConfigError, match="extra_forbidden"):
        parse_tool_config(
            """
version: 1
unknown: true
tools: {}
"""
        )


def test_parse_tool_config_rejects_empty_command() -> None:
    with pytest.raises(ToolConfigError, match="tool command"):
        parse_tool_config(
            """
version: 1
tools:
  broken:
    command: []
"""
        )


def test_parse_tool_config_rejects_future_version() -> None:
    with pytest.raises(ToolConfigError):
        parse_tool_config("version: 2\ntools: {}\n")


def test_example_config_is_valid() -> None:
    config = load_tool_config(Path("configs/tools.example.yaml"))
    assert config.execution_policy is ExecutionPolicy.REGISTERED_ONLY
    assert {"nmap", "nuclei", "msfconsole", "custom_poc"} <= set(config.tools)
