"""Progressive Skill adapter for unified Session Capability selections."""

from __future__ import annotations

from riftx.application.errors import RepositoryIntegrityError
from riftx.capabilities import (
    CapabilityKind,
    CapabilitySelectionStore,
    SessionCapabilitySelection,
)
from riftx.skills import (
    SkillDocument,
    SkillReference,
    SkillSelectionState,
    skill_capability_selection,
)

from .capability_selection_repository import SQLAlchemyCapabilitySelectionStore
from .transactions import SessionFactory


class SQLAlchemySkillSelectionStore:
    """Compatibility port backed by the unified Capability selection tables."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._store: CapabilitySelectionStore = SQLAlchemyCapabilitySelectionStore(session_factory)

    async def get_selection(
        self,
        session_id: str,
        skill_id: str,
    ) -> SkillSelectionState | None:
        selection = await self._store.get_selection(
            session_id,
            CapabilityKind.SKILL,
            skill_id,
        )
        return _from_selection(selection) if selection is not None else None

    async def list_selections(self, session_id: str) -> list[SkillSelectionState]:
        selections = await self._store.list_selections(
            session_id,
            kind=CapabilityKind.SKILL,
        )
        return [_from_selection(selection) for selection in selections]

    async def save_selection(self, selection: SkillSelectionState) -> None:
        await self._store.save_selection(skill_capability_selection(selection))

    async def replace_selection(
        self,
        selection: SkillSelectionState,
        *,
        expected_digest: str,
    ) -> None:
        await self._store.replace_selection(
            skill_capability_selection(selection),
            expected_digest=expected_digest,
        )

    async def get_allowlist(self, session_id: str) -> frozenset[str] | None:
        return await self._store.get_allowlist(session_id, CapabilityKind.SKILL)

    async def set_allowlist(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        skill_ids: list[str],
    ) -> None:
        await self._store.set_allowlist(
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
            kind=CapabilityKind.SKILL,
            capability_ids=skill_ids,
        )


def _from_selection(selection: SessionCapabilitySelection) -> SkillSelectionState:
    try:
        if selection.kind is not CapabilityKind.SKILL:
            raise ValueError("selection is not a Skill")
        document = SkillDocument.model_validate(selection.snapshot["document"])
        raw_reference = selection.snapshot.get("reference")
        reference = (
            SkillReference.model_validate(raw_reference) if raw_reference is not None else None
        )
        references_loaded = selection.state.get("references_loaded", False)
        if not isinstance(references_loaded, bool):
            raise ValueError("references_loaded is not a boolean")
        return SkillSelectionState(
            run_id=selection.run_id,
            session_id=selection.session_id,
            agent_id=selection.agent_id,
            skill_id=selection.capability_id,
            version=selection.version,
            digest=selection.digest,
            source=selection.source,
            reason=selection.reason,
            document=document,
            reference=reference,
            active=selection.active,
            references_loaded=references_loaded,
            selected_at=selection.selected_at,
            updated_at=selection.updated_at,
            unloaded_at=selection.unloaded_at,
        )
    except (KeyError, TypeError, ValueError):
        raise RepositoryIntegrityError(
            "AgentCapabilitySelection", selection.capability_id
        ) from None
