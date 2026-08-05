"""Executable Skill contracts, registries, and built-in skills."""

from pathlib import Path

from .base import BaseSkill, SkillContext, SkillResult
from .context import (
    InMemorySkillSelectionStore,
    ProgressiveSkillContextManager,
    SkillSelectionState,
    SkillSelectionStore,
    SkillVisibilitySnapshot,
)
from .generic import (
    PortScanArguments,
    PortScanSkill,
    RegisteredToolArguments,
    RegisteredToolSkill,
    ShellArguments,
    ShellSkill,
)
from .models import (
    SkillDocument,
    SkillFrontMatter,
    SkillReference,
    SkillSearchResult,
    SkillSummary,
)
from .progressive import (
    ProgressiveSkillRegistry,
    SkillDocumentError,
    SkillReferenceNotFoundError,
)
from .registry import DuplicateSkillError, SkillNotFoundError, SkillRegistry


def create_default_skill_registry(skill_root: Path | None = None) -> SkillRegistry:
    registry = SkillRegistry(skill_root)
    registry.register(RegisteredToolSkill())
    registry.register(ShellSkill())
    registry.register(PortScanSkill())
    return registry


__all__ = [
    "BaseSkill",
    "DuplicateSkillError",
    "InMemorySkillSelectionStore",
    "PortScanArguments",
    "ProgressiveSkillContextManager",
    "ProgressiveSkillRegistry",
    "SkillDocument",
    "SkillDocumentError",
    "SkillFrontMatter",
    "SkillReference",
    "SkillReferenceNotFoundError",
    "SkillSearchResult",
    "SkillSelectionState",
    "SkillSelectionStore",
    "SkillSummary",
    "SkillVisibilitySnapshot",
    "PortScanSkill",
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
