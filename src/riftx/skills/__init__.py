"""Executable Skill contracts, registries, and built-in generic skills."""

from .base import BaseSkill, SkillContext, SkillResult
from .generic import (
    RegisteredToolArguments,
    RegisteredToolSkill,
    ShellArguments,
    ShellSkill,
)
from .registry import DuplicateSkillError, SkillNotFoundError, SkillRegistry


def create_default_skill_registry() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(RegisteredToolSkill())
    registry.register(ShellSkill())
    return registry


__all__ = [
    "BaseSkill",
    "DuplicateSkillError",
    "RegisteredToolArguments",
    "RegisteredToolSkill",
    "ShellArguments",
    "ShellSkill",
    "SkillContext",
    "SkillNotFoundError",
    "SkillRegistry",
    "SkillResult",
    "create_default_skill_registry",
]
