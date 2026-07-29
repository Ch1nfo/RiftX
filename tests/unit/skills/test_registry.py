from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from riftx.skills import (
    BaseSkill,
    DuplicateSkillError,
    SkillContext,
    SkillRegistry,
    SkillResult,
    create_default_skill_registry,
)
from riftx.tools import ToolRegistry, ToolUnavailableError


class EmptyArguments(BaseModel):
    pass


class DemoSkill(BaseSkill):
    id = "demo"
    description = "demo skill"
    required_capabilities = frozenset({"demo"})
    arguments_model = EmptyArguments

    async def execute(self, context: SkillContext, arguments: BaseModel) -> SkillResult:
        raise NotImplementedError


class FakeEntryPoint:
    name = "demo"

    @staticmethod
    def load() -> type[DemoSkill]:
        return DemoSkill


def write_tools(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": {
                    "alpha": {
                        "command": [sys.executable],
                        "capabilities": ["scan", "alpha"],
                    },
                    "beta": {
                        "command": [sys.executable],
                        "capabilities": ["scan", "beta"],
                    },
                },
            },
            sort_keys=False,
        )
    )


def test_default_registry_contains_generic_skills() -> None:
    registry = create_default_skill_registry()
    assert [skill.id for skill in registry.list()] == [
        "run_registered_tool",
        "run_shell",
    ]


def test_registry_rejects_duplicate_skill() -> None:
    registry = SkillRegistry()
    registry.register(DemoSkill())
    with pytest.raises(DuplicateSkillError):
        registry.register(DemoSkill())


def test_registry_loads_python_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "riftx.skills.registry.metadata.entry_points",
        lambda *, group: [FakeEntryPoint()] if group == "riftx.skills" else [],
    )
    registry = SkillRegistry()

    loaded = registry.load_entry_points()

    assert [skill.id for skill in loaded] == ["demo"]
    assert registry.get("demo") is loaded[0]


async def test_tool_selection_order(tmp_path: Path) -> None:
    config_path = tmp_path / "tools.yaml"
    write_tools(config_path)
    tools = ToolRegistry(config_path, node_id="node-1")
    await tools.refresh()

    requested = SkillRegistry.select_tool(
        tools,
        required_capabilities={"scan"},
        preferred_tools=("beta",),
        requested_tool_id="alpha",
    )
    preferred = SkillRegistry.select_tool(
        tools,
        required_capabilities={"scan"},
        preferred_tools=("beta",),
    )
    fallback = SkillRegistry.select_tool(
        tools,
        required_capabilities={"scan"},
    )

    assert requested.id == "alpha"
    assert preferred.id == "beta"
    assert fallback.id == "alpha"


async def test_tool_selection_rejects_missing_capability(tmp_path: Path) -> None:
    config_path = tmp_path / "tools.yaml"
    write_tools(config_path)
    tools = ToolRegistry(config_path, node_id="node-1")
    await tools.refresh()

    with pytest.raises(ToolUnavailableError, match="lacks capabilities"):
        SkillRegistry.select_tool(
            tools,
            required_capabilities={"not-present"},
            requested_tool_id="alpha",
        )
