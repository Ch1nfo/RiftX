"""Executable Skill contracts, registries, and built-in skills."""

from pathlib import Path

from .base import BaseSkill, SkillContext, SkillResult
from .context import (
    InMemorySkillSelectionStore,
    ProgressiveSkillContextManager,
    SkillSelectionState,
    SkillSelectionStore,
    SkillVisibilitySnapshot,
    build_skill_selection_state,
    skill_capability_selection,
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
    SkillPackageRoot,
    SkillReferenceNotFoundError,
)
from .registry import DuplicateSkillError, SkillNotFoundError, SkillRegistry


def create_default_skill_registry(
    skill_root: Path | None = None,
    *,
    official_skill_roots: tuple[Path, ...] = (),
) -> SkillRegistry:
    registry = SkillRegistry(
        skill_root,
        official_skill_roots=official_skill_roots,
    )
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
    "build_skill_selection_state",
    "skill_capability_selection",
    "PortScanSkill",
    "RegisteredToolArguments",
    "RegisteredToolSkill",
    "ShellArguments",
    "ShellSkill",
    "SkillContext",
    "SkillNotFoundError",
    "SkillPackageRoot",
    "SkillRegistry",
    "SkillResult",
    "create_default_skill_registry",
]
