"""Per-agent Progressive Skill selection and Context Compiler payloads."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from .models import SkillDocument, SkillReference, SkillSearchResult, SkillSummary
from .registry import SkillRegistry


class SkillVisibilitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_skills: list[SkillSummary] = Field(default_factory=list)
    loaded_skill_documents: list[SkillDocument] = Field(default_factory=list)
    loaded_skill_references: list[SkillReference] = Field(default_factory=list)
    skill_registry_generation: int = Field(ge=1)

    def manifest(self) -> dict[str, object]:
        return {
            "available_skill_ids": [skill.id for skill in self.available_skills],
            "loaded_skill_document_ids": [skill.id for skill in self.loaded_skill_documents],
            "loaded_skill_reference_ids": [
                reference.skill_id for reference in self.loaded_skill_references
            ],
            "skill_registry_generation": self.skill_registry_generation,
        }


@dataclass(slots=True)
class _ScopedSkills:
    documents: set[str] = field(default_factory=set)
    references: set[str] = field(default_factory=set)


class ProgressiveSkillContextManager:
    """Keep selected Skill procedures and references isolated by agent session."""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry
        self._sets: dict[tuple[str, str, str], _ScopedSkills] = {}

    def list_skills(self) -> list[SkillSummary]:
        return self.registry.list_skill_summaries()

    def search_skills(
        self,
        query: str,
        *,
        capability: str | None = None,
        max_results: int = 8,
    ) -> list[SkillSearchResult]:
        return self.registry.search_skill_documents(
            query,
            capability=capability,
            max_results=max_results,
        )

    def select_skill(
        self,
        skill_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> SkillDocument:
        document = self.registry.load_skill_document(skill_id)
        self._scope(run_id, session_id, agent_id).documents.add(skill_id)
        return document

    def load_references(
        self,
        skill_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> SkillReference:
        scope = self._scope(run_id, session_id, agent_id)
        if skill_id not in scope.documents:
            raise ValueError(
                f"Skill {skill_id!r} must be selected before loading its references"
            )
        reference = self.registry.load_skill_references(skill_id)
        scope.references.add(skill_id)
        return reference

    def visibility(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> SkillVisibilitySnapshot:
        scope = self._scope(run_id, session_id, agent_id)
        summaries = self.registry.list_skill_summaries()
        known_ids = {summary.id for summary in summaries}
        documents = [
            self.registry.load_skill_document(skill_id)
            for skill_id in sorted(scope.documents & known_ids)
        ]
        references = [
            self.registry.load_skill_references(skill_id)
            for skill_id in sorted(scope.references & known_ids)
        ]
        return SkillVisibilitySnapshot(
            available_skills=summaries,
            loaded_skill_documents=documents,
            loaded_skill_references=references,
            skill_registry_generation=self.registry.progressive_generation,
        )

    def _scope(self, run_id: str, session_id: str, agent_id: str) -> _ScopedSkills:
        return self._sets.setdefault((run_id, session_id, agent_id), _ScopedSkills())
