"""Session-scoped Progressive Skill selection and Context Compiler payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from riftx.application.errors import RepositoryConflictError
from riftx.capabilities import CapabilitySource
from riftx.domain.base import utc_now

from .models import SkillDocument, SkillReference, SkillSearchResult, SkillSummary
from .progressive import SkillReferenceNotFoundError
from .registry import SkillRegistry


class SkillSelectionState(BaseModel):
    """Pinned Skill package content owned by exactly one Agent Session."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    digest: str = Field(pattern="^[0-9a-f]{64}$")
    source: CapabilitySource
    reason: str = Field(min_length=1, max_length=1000)
    document: SkillDocument
    reference: SkillReference | None = None
    active: bool = True
    references_loaded: bool = False
    selected_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    unloaded_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_state_shape(self) -> SkillSelectionState:
        if self.active == (self.unloaded_at is not None):
            raise ValueError("active Skill selection and unloaded_at are inconsistent")
        if self.references_loaded and self.reference is None:
            raise ValueError("loaded Skill references require a pinned reference snapshot")
        if (
            self.document.id != self.skill_id
            or self.document.version != self.version
            or self.document.digest != self.digest
            or self.document.source is not self.source
        ):
            raise ValueError("Skill selection metadata does not match its document snapshot")
        return self


class SkillSelectionStore(Protocol):
    async def get_selection(
        self,
        session_id: str,
        skill_id: str,
    ) -> SkillSelectionState | None: ...

    async def list_selections(self, session_id: str) -> list[SkillSelectionState]: ...

    async def save_selection(self, selection: SkillSelectionState) -> None: ...

    async def replace_selection(
        self,
        selection: SkillSelectionState,
        *,
        expected_digest: str,
    ) -> None: ...

    async def get_allowlist(self, session_id: str) -> frozenset[str] | None: ...

    async def set_allowlist(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        skill_ids: list[str],
    ) -> None: ...


class InMemorySkillSelectionStore:
    """Ephemeral default used outside the production Worker."""

    def __init__(self) -> None:
        self._selections: dict[tuple[str, str], SkillSelectionState] = {}
        self._allowlists: dict[str, frozenset[str]] = {}

    async def get_selection(
        self,
        session_id: str,
        skill_id: str,
    ) -> SkillSelectionState | None:
        return self._selections.get((session_id, skill_id))

    async def list_selections(self, session_id: str) -> list[SkillSelectionState]:
        return sorted(
            (
                selection
                for (owner_session_id, _), selection in self._selections.items()
                if owner_session_id == session_id
            ),
            key=lambda selection: (selection.selected_at, selection.skill_id),
        )

    async def save_selection(self, selection: SkillSelectionState) -> None:
        key = (selection.session_id, selection.skill_id)
        existing = self._selections.get(key)
        if existing is not None and _pinned_skill_fields(existing) != _pinned_skill_fields(
            selection
        ):
            raise RepositoryConflictError(
                "Running Agent Session cannot replace a pinned Skill package"
            )
        self._selections[key] = selection

    async def replace_selection(
        self,
        selection: SkillSelectionState,
        *,
        expected_digest: str,
    ) -> None:
        key = (selection.session_id, selection.skill_id)
        existing = self._selections.get(key)
        if existing is None or existing.digest != expected_digest:
            raise RepositoryConflictError("Skill reload digest no longer matches")
        if (
            existing.run_id,
            existing.session_id,
            existing.agent_id,
            existing.skill_id,
        ) != (
            selection.run_id,
            selection.session_id,
            selection.agent_id,
            selection.skill_id,
        ):
            raise RepositoryConflictError("Skill reload changed its Session scope")
        self._selections[key] = selection

    async def get_allowlist(self, session_id: str) -> frozenset[str] | None:
        return self._allowlists.get(session_id)

    async def set_allowlist(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        skill_ids: list[str],
    ) -> None:
        del run_id, agent_id
        self._allowlists[session_id] = frozenset(skill_ids)


class SkillSelectionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    digest: str
    source: CapabilitySource
    reason: str
    references_loaded: bool
    stale: bool


class SkillVisibilitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_skills: list[SkillSummary] = Field(default_factory=list)
    loaded_skill_documents: list[SkillDocument] = Field(default_factory=list)
    loaded_skill_references: list[SkillReference] = Field(default_factory=list)
    loaded_skills: list[SkillSelectionManifest] = Field(default_factory=list)
    skill_registry_generation: int = Field(ge=1)

    def manifest(self) -> dict[str, object]:
        return {
            "available_skill_ids": [skill.id for skill in self.available_skills],
            "loaded_skill_document_ids": [skill.id for skill in self.loaded_skill_documents],
            "loaded_skill_reference_ids": [
                reference.skill_id for reference in self.loaded_skill_references
            ],
            "loaded_skills": [item.model_dump(mode="json") for item in self.loaded_skills],
            "stale_skill_ids": [item.id for item in self.loaded_skills if item.stale],
            "skill_registry_generation": self.skill_registry_generation,
        }


class ProgressiveSkillContextManager:
    """Pin selected Skill packages and isolate visibility by Agent Session."""

    def __init__(
        self,
        registry: SkillRegistry,
        store: SkillSelectionStore | None = None,
    ) -> None:
        self.registry = registry
        self._store = store or InMemorySkillSelectionStore()

    async def list_skills(
        self,
        *,
        session_id: str,
    ) -> list[SkillSummary]:
        return await self._filter_allowed(
            session_id,
            self.registry.list_skill_summaries(),
        )

    async def search_skills(
        self,
        query: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        capability: str | None = None,
        max_results: int = 8,
    ) -> list[SkillSearchResult]:
        del run_id, agent_id
        results: list[SkillSearchResult] = self.registry.search_skill_documents(
            query,
            capability=capability,
            max_results=max_results,
        )
        allowed = await self._store.get_allowlist(session_id)
        if allowed is None:
            return results
        return [result for result in results if result.skill.id in allowed]

    async def select_skill(
        self,
        skill_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        reason: str = "agent_selected",
        selected_at: datetime | None = None,
    ) -> SkillDocument:
        await self._require_allowed(session_id, skill_id)
        existing = await self._store.get_selection(session_id, skill_id)
        now = selected_at or utc_now()
        if existing is not None:
            self._require_scope(existing, run_id=run_id, agent_id=agent_id)
            if not existing.active:
                existing = existing.model_copy(
                    update={
                        "active": True,
                        "updated_at": now,
                        "unloaded_at": None,
                    }
                )
                await self._store.save_selection(existing)
            return existing.document

        document = self.registry.load_skill_document(skill_id)
        try:
            reference = self.registry.load_skill_references(skill_id)
        except SkillReferenceNotFoundError:
            reference = None
        await self._store.save_selection(
            SkillSelectionState(
                run_id=run_id,
                session_id=session_id,
                agent_id=agent_id,
                skill_id=skill_id,
                version=document.version,
                digest=document.digest,
                source=document.source,
                reason=reason,
                document=document,
                reference=reference,
                selected_at=now,
                updated_at=now,
            )
        )
        return document

    async def load_references(
        self,
        skill_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        loaded_at: datetime | None = None,
    ) -> SkillReference:
        selection = await self._require_active_selection(
            skill_id,
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
        )
        if selection.reference is None:
            raise SkillReferenceNotFoundError(
                f"Skill {skill_id!r} does not provide REFERENCES.md"
            )
        if not selection.references_loaded:
            await self._store.save_selection(
                selection.model_copy(
                    update={
                        "references_loaded": True,
                        "updated_at": loaded_at or utc_now(),
                    }
                )
            )
        return selection.reference

    async def reload_skill(
        self,
        skill_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        reason: str = "agent_reloaded",
        reloaded_at: datetime | None = None,
    ) -> SkillDocument:
        """Explicitly replace a pinned Skill snapshot with the current package."""

        await self._require_allowed(session_id, skill_id)
        existing = await self._store.get_selection(session_id, skill_id)
        if existing is None:
            raise ValueError(f"Skill {skill_id!r} is not selected")
        self._require_scope(existing, run_id=run_id, agent_id=agent_id)
        document = self.registry.load_skill_document(skill_id)
        try:
            reference = self.registry.load_skill_references(skill_id)
        except SkillReferenceNotFoundError:
            reference = None
        now = reloaded_at or utc_now()
        replacement = SkillSelectionState(
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
            skill_id=skill_id,
            version=document.version,
            digest=document.digest,
            source=document.source,
            reason=reason,
            document=document,
            reference=reference,
            active=True,
            references_loaded=False,
            selected_at=existing.selected_at,
            updated_at=now,
        )
        await self._store.replace_selection(
            replacement,
            expected_digest=existing.digest,
        )
        return document

    async def unload_skill(
        self,
        skill_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        unloaded_at: datetime | None = None,
    ) -> None:
        selection = await self._store.get_selection(session_id, skill_id)
        if selection is None:
            return
        self._require_scope(selection, run_id=run_id, agent_id=agent_id)
        if not selection.active:
            return
        now = unloaded_at or utc_now()
        await self._store.save_selection(
            selection.model_copy(
                update={
                    "active": False,
                    "references_loaded": False,
                    "updated_at": now,
                    "unloaded_at": now,
                }
            )
        )

    async def restrict_skills(
        self,
        skill_ids: list[str],
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> None:
        summaries: list[SkillSummary] = self.registry.list_skill_summaries()
        known = {summary.id for summary in summaries}
        unknown = sorted(set(skill_ids) - known)
        if unknown:
            raise KeyError(unknown[0])
        await self._store.set_allowlist(
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
            skill_ids=list(dict.fromkeys(skill_ids)),
        )

    async def visibility(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> SkillVisibilitySnapshot:
        summaries = await self.list_skills(session_id=session_id)
        current = {summary.id: summary for summary in summaries}
        selections = [
            selection
            for selection in await self._store.list_selections(session_id)
            if selection.active
        ]
        for selection in selections:
            self._require_scope(selection, run_id=run_id, agent_id=agent_id)
        manifests = [
            SkillSelectionManifest(
                id=selection.skill_id,
                version=selection.version,
                digest=selection.digest,
                source=selection.source,
                reason=selection.reason,
                references_loaded=selection.references_loaded,
                stale=(
                    selection.skill_id not in current
                    or current[selection.skill_id].digest != selection.digest
                ),
            )
            for selection in selections
        ]
        return SkillVisibilitySnapshot(
            available_skills=summaries,
            loaded_skill_documents=[selection.document for selection in selections],
            loaded_skill_references=[
                selection.reference
                for selection in selections
                if selection.references_loaded and selection.reference is not None
            ],
            loaded_skills=manifests,
            skill_registry_generation=self.registry.progressive_generation,
        )

    async def _filter_allowed(
        self,
        session_id: str,
        summaries: list[SkillSummary],
    ) -> list[SkillSummary]:
        allowed = await self._store.get_allowlist(session_id)
        if allowed is None:
            return summaries
        return [summary for summary in summaries if summary.id in allowed]

    async def _require_allowed(self, session_id: str, skill_id: str) -> None:
        allowed = await self._store.get_allowlist(session_id)
        if allowed is not None and skill_id not in allowed:
            raise PermissionError(f"Skill {skill_id!r} is outside the Session allowlist")

    async def _require_active_selection(
        self,
        skill_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> SkillSelectionState:
        selection = await self._store.get_selection(session_id, skill_id)
        if selection is None or not selection.active:
            raise ValueError(
                f"Skill {skill_id!r} must be selected before loading its references"
            )
        self._require_scope(selection, run_id=run_id, agent_id=agent_id)
        return selection

    @staticmethod
    def _require_scope(
        selection: SkillSelectionState,
        *,
        run_id: str,
        agent_id: str,
    ) -> None:
        if selection.run_id != run_id or selection.agent_id != agent_id:
            raise PermissionError("Skill selection belongs to a different Agent Session scope")


def _pinned_skill_fields(selection: SkillSelectionState) -> tuple[object, ...]:
    return (
        selection.run_id,
        selection.session_id,
        selection.agent_id,
        selection.skill_id,
        selection.version,
        selection.digest,
        selection.source,
        selection.reason,
        selection.document,
        selection.reference,
        selection.selected_at,
    )
