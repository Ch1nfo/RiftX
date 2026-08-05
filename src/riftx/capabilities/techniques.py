"""Session-scoped selection of versioned Technique capabilities."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from riftx.domain.base import utc_now

from .models import (
    CapabilityKind,
    CapabilitySource,
    CapabilityVersion,
)
from .repository import CapabilityRepository
from .selection import (
    CapabilitySelectionStore,
    InMemoryCapabilitySelectionStore,
    SessionCapabilitySelection,
)


class TechniqueSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str
    digest: str
    title: str
    description: str
    source: CapabilitySource


class TechniqueSelectionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str
    digest: str
    source: CapabilitySource
    reason: str
    stale: bool


class TechniqueVisibilitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_techniques: list[TechniqueSummary] = Field(default_factory=list)
    loaded_techniques: list[TechniqueSelectionManifest] = Field(default_factory=list)

    def manifest(self) -> dict[str, object]:
        return {
            "available_technique_ids": [item.id for item in self.available_techniques],
            "loaded_techniques": [
                item.model_dump(mode="json") for item in self.loaded_techniques
            ],
            "stale_technique_ids": [
                item.id for item in self.loaded_techniques if item.stale
            ],
        }


class TechniqueContextManager:
    def __init__(
        self,
        repository: CapabilityRepository,
        store: CapabilitySelectionStore | None = None,
    ) -> None:
        self._repository = repository
        self._store = store or InMemoryCapabilitySelectionStore()

    async def list_techniques(self, *, session_id: str) -> list[TechniqueSummary]:
        versions = await self._repository.list_active_versions(CapabilityKind.TECHNIQUE)
        allowed = await self._store.get_allowlist(session_id, CapabilityKind.TECHNIQUE)
        return [
            _summary(version)
            for version in versions
            if allowed is None or version.manifest.capability_id in allowed
        ]

    async def select_technique(
        self,
        technique_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        reason: str = "agent_selected",
        selected_at: datetime | None = None,
    ) -> CapabilityVersion:
        await self._require_allowed(session_id, technique_id)
        existing = await self._store.get_selection(
            session_id,
            CapabilityKind.TECHNIQUE,
            technique_id,
        )
        if existing is not None:
            self._require_scope(existing, run_id=run_id, agent_id=agent_id)
            if not existing.active:
                await self._store.save_selection(
                    existing.model_copy(
                        update={
                            "active": True,
                            "updated_at": selected_at or utc_now(),
                            "unloaded_at": None,
                        }
                    )
                )
            return _pinned_version(existing)

        version = await self._active_version(technique_id)
        now = selected_at or utc_now()
        await self._store.save_selection(
            _selection(
                version,
                run_id=run_id,
                session_id=session_id,
                agent_id=agent_id,
                reason=reason,
                selected_at=now,
                updated_at=now,
            )
        )
        return version

    async def reload_technique(
        self,
        technique_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        reason: str = "agent_reloaded",
        reloaded_at: datetime | None = None,
    ) -> CapabilityVersion:
        await self._require_allowed(session_id, technique_id)
        existing = await self._store.get_selection(
            session_id,
            CapabilityKind.TECHNIQUE,
            technique_id,
        )
        if existing is None:
            raise ValueError(f"Technique {technique_id!r} is not selected")
        self._require_scope(existing, run_id=run_id, agent_id=agent_id)
        version = await self._active_version(technique_id)
        replacement = _selection(
            version,
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
            reason=reason,
            selected_at=existing.selected_at,
            updated_at=reloaded_at or utc_now(),
        )
        await self._store.replace_selection(
            replacement,
            expected_digest=existing.digest,
        )
        return version

    async def unload_technique(
        self,
        technique_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        unloaded_at: datetime | None = None,
    ) -> None:
        selection = await self._store.get_selection(
            session_id,
            CapabilityKind.TECHNIQUE,
            technique_id,
        )
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
                    "updated_at": now,
                    "unloaded_at": now,
                }
            )
        )

    async def restrict_techniques(
        self,
        technique_ids: list[str],
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> None:
        active = {
            version.manifest.capability_id
            for version in await self._repository.list_active_versions(
                CapabilityKind.TECHNIQUE
            )
        }
        unknown = sorted(set(technique_ids) - active)
        if unknown:
            raise KeyError(unknown[0])
        await self._store.set_allowlist(
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
            kind=CapabilityKind.TECHNIQUE,
            capability_ids=list(dict.fromkeys(technique_ids)),
        )

    async def visibility(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> TechniqueVisibilitySnapshot:
        available = await self.list_techniques(session_id=session_id)
        current = {item.id: item for item in available}
        selections = [
            selection
            for selection in await self._store.list_selections(
                session_id,
                kind=CapabilityKind.TECHNIQUE,
            )
            if selection.active
        ]
        for selection in selections:
            self._require_scope(selection, run_id=run_id, agent_id=agent_id)
            _pinned_version(selection)
        return TechniqueVisibilitySnapshot(
            available_techniques=available,
            loaded_techniques=[
                TechniqueSelectionManifest(
                    id=selection.capability_id,
                    version=selection.version,
                    digest=selection.digest,
                    source=selection.source,
                    reason=selection.reason,
                    stale=(
                        selection.capability_id not in current
                        or current[selection.capability_id].digest != selection.digest
                    ),
                )
                for selection in selections
            ],
        )

    async def _active_version(self, technique_id: str) -> CapabilityVersion:
        versions = [
            version
            for version in await self._repository.list_active_versions(
                CapabilityKind.TECHNIQUE
            )
            if version.manifest.capability_id == technique_id
        ]
        if len(versions) != 1:
            raise KeyError(technique_id)
        return versions[0]

    async def _require_allowed(self, session_id: str, technique_id: str) -> None:
        allowed = await self._store.get_allowlist(session_id, CapabilityKind.TECHNIQUE)
        if allowed is not None and technique_id not in allowed:
            raise PermissionError(
                f"Technique {technique_id!r} is outside the Session allowlist"
            )

    @staticmethod
    def _require_scope(
        selection: SessionCapabilitySelection,
        *,
        run_id: str,
        agent_id: str,
    ) -> None:
        if selection.run_id != run_id or selection.agent_id != agent_id:
            raise PermissionError(
                "Technique selection belongs to a different Agent Session scope"
            )


def _selection(
    version: CapabilityVersion,
    *,
    run_id: str,
    session_id: str,
    agent_id: str,
    reason: str,
    selected_at: datetime,
    updated_at: datetime,
) -> SessionCapabilitySelection:
    if version.manifest.kind is not CapabilityKind.TECHNIQUE:
        raise ValueError("Capability version is not a Technique")
    return SessionCapabilitySelection(
        run_id=run_id,
        session_id=session_id,
        agent_id=agent_id,
        kind=CapabilityKind.TECHNIQUE,
        capability_id=version.manifest.capability_id,
        version=version.manifest.version,
        digest=version.manifest_digest,
        source=version.manifest.provenance.source,
        reason=reason,
        snapshot={"capability_version": version.model_dump(mode="json")},
        selected_at=selected_at,
        updated_at=updated_at,
    )


def _pinned_version(selection: SessionCapabilitySelection) -> CapabilityVersion:
    version = CapabilityVersion.model_validate(selection.snapshot.get("capability_version"))
    if (
        selection.kind is not CapabilityKind.TECHNIQUE
        or version.manifest.kind is not CapabilityKind.TECHNIQUE
        or version.manifest.capability_id != selection.capability_id
        or version.manifest.version != selection.version
        or version.manifest_digest != selection.digest
        or version.manifest.provenance.source is not selection.source
    ):
        raise ValueError("Technique selection snapshot failed integrity validation")
    return version


def _summary(version: CapabilityVersion) -> TechniqueSummary:
    manifest = version.manifest
    return TechniqueSummary(
        id=manifest.capability_id,
        version=manifest.version,
        digest=version.manifest_digest,
        title=manifest.title,
        description=manifest.description,
        source=manifest.provenance.source,
    )
