"""Skill registration, entry-point loading, and Tool selection."""

from __future__ import annotations

from importlib import metadata

from riftx.domain import ToolAvailability
from riftx.tools import ToolDefinition, ToolRegistry, ToolUnavailableError

from .base import BaseSkill


class SkillNotFoundError(KeyError):
    pass


class DuplicateSkillError(ValueError):
    pass


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, BaseSkill] = {}

    def register(self, skill: BaseSkill, *, replace: bool = False) -> None:
        if skill.id in self._skills and not replace:
            raise DuplicateSkillError(f"skill {skill.id!r} is already registered")
        self._skills[skill.id] = skill

    def get(self, skill_id: str) -> BaseSkill:
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise SkillNotFoundError(skill_id) from exc

    def list(self) -> list[BaseSkill]:
        return list(self._skills.values())

    def find_for_capability(self, capability: str) -> list[BaseSkill]:
        return [
            skill for skill in self._skills.values() if capability in skill.required_capabilities
        ]

    def load_entry_points(self, *, group: str = "riftx.skills") -> list[BaseSkill]:
        loaded: list[BaseSkill] = []
        for entry_point in metadata.entry_points(group=group):
            candidate = entry_point.load()
            skill = candidate() if isinstance(candidate, type) else candidate
            if not isinstance(skill, BaseSkill):
                raise TypeError(f"entry point {entry_point.name!r} did not provide a BaseSkill")
            self.register(skill)
            loaded.append(skill)
        return loaded

    @staticmethod
    def select_tool(
        tool_registry: ToolRegistry,
        *,
        required_capabilities: set[str] | frozenset[str],
        preferred_tools: tuple[str, ...] | list[str] = (),
        requested_tool_id: str | None = None,
    ) -> ToolDefinition:
        if requested_tool_id is not None:
            definition = tool_registry.get_available(requested_tool_id)
            _require_capabilities(definition, required_capabilities)
            return definition

        for tool_id in preferred_tools:
            state = tool_registry.snapshot.states.get(tool_id)
            if state is None or state.availability is not ToolAvailability.AVAILABLE:
                continue
            definition = tool_registry.get(tool_id)
            if required_capabilities <= set(definition.capabilities):
                return definition

        for definition in tool_registry.available_tools():
            if required_capabilities <= set(definition.capabilities):
                return definition
        capabilities = ", ".join(sorted(required_capabilities)) or "<none>"
        raise ToolUnavailableError(
            f"no available tool provides required capabilities: {capabilities}"
        )


def _require_capabilities(
    definition: ToolDefinition, required_capabilities: set[str] | frozenset[str]
) -> None:
    missing = required_capabilities - set(definition.capabilities)
    if missing:
        raise ToolUnavailableError(
            f"tool {definition.id!r} lacks capabilities: {', '.join(sorted(missing))}"
        )
