"""Session-scoped, version-pinned Capability selections."""

from __future__ import annotations

from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from riftx.application.errors import RepositoryConflictError
from riftx.domain.base import utc_now

from .models import CapabilityKind, CapabilitySource

_SELECTABLE_KINDS = frozenset({CapabilityKind.TOOL, CapabilityKind.SKILL, CapabilityKind.TECHNIQUE})


class SessionCapabilitySelection(BaseModel):
    """Pinned Capability snapshot owned by exactly one Agent Session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1, max_length=128)
    kind: CapabilityKind
    capability_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=1024)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: CapabilitySource
    reason: str = Field(min_length=1, max_length=1000)
    snapshot: dict[str, JsonValue]
    state: dict[str, JsonValue] = Field(default_factory=dict)
    active: bool = True
    selected_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    unloaded_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> SessionCapabilitySelection:
        if self.kind not in _SELECTABLE_KINDS:
            raise ValueError("only Tool, Skill, and Technique can be selected")
        if self.active == (self.unloaded_at is not None):
            raise ValueError("active Capability selection and unloaded_at are inconsistent")
        if self.updated_at < self.selected_at:
            raise ValueError("Capability selection update cannot predate selection")
        return self


class CapabilitySelectionStore(Protocol):
    async def get_selection(
        self,
        session_id: str,
        kind: CapabilityKind,
        capability_id: str,
    ) -> SessionCapabilitySelection | None: ...

    async def list_selections(
        self,
        session_id: str,
        *,
        kind: CapabilityKind | None = None,
    ) -> list[SessionCapabilitySelection]: ...

    async def save_selection(self, selection: SessionCapabilitySelection) -> None: ...

    async def replace_selection(
        self,
        selection: SessionCapabilitySelection,
        *,
        expected_digest: str,
    ) -> None: ...

    async def get_allowlist(
        self,
        session_id: str,
        kind: CapabilityKind,
    ) -> frozenset[str] | None: ...

    async def set_allowlist(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        kind: CapabilityKind,
        capability_ids: list[str],
    ) -> None: ...


class SessionCapabilityManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CapabilityKind
    capability_id: str
    version: str
    digest: str
    source: CapabilitySource
    reason: str
    active: bool


class SessionCapabilityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    session_id: str
    agent_id: str
    selections: list[SessionCapabilityManifestEntry] = Field(default_factory=list)


class SessionCapabilityManifestReader:
    def __init__(self, store: CapabilitySelectionStore) -> None:
        self._store = store

    async def read(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> SessionCapabilityManifest:
        selections = await self._store.list_selections(session_id)
        for selection in selections:
            if selection.run_id != run_id or selection.agent_id != agent_id:
                raise PermissionError(
                    "Capability manifest belongs to a different Agent Session scope"
                )
        return SessionCapabilityManifest(
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
            selections=[
                SessionCapabilityManifestEntry(
                    kind=selection.kind,
                    capability_id=selection.capability_id,
                    version=selection.version,
                    digest=selection.digest,
                    source=selection.source,
                    reason=selection.reason,
                    active=selection.active,
                )
                for selection in selections
            ],
        )


class InMemoryCapabilitySelectionStore:
    """Ephemeral store for tests and non-production runtimes."""

    def __init__(self) -> None:
        self._selections: dict[tuple[str, CapabilityKind, str], SessionCapabilitySelection] = {}
        self._allowlists: dict[tuple[str, CapabilityKind], frozenset[str]] = {}

    async def get_selection(
        self,
        session_id: str,
        kind: CapabilityKind,
        capability_id: str,
    ) -> SessionCapabilitySelection | None:
        return self._selections.get((session_id, kind, capability_id))

    async def list_selections(
        self,
        session_id: str,
        *,
        kind: CapabilityKind | None = None,
    ) -> list[SessionCapabilitySelection]:
        return sorted(
            (
                selection
                for (owner_session_id, owner_kind, _), selection in self._selections.items()
                if owner_session_id == session_id and (kind is None or owner_kind is kind)
            ),
            key=lambda selection: (
                selection.selected_at,
                selection.kind.value,
                selection.capability_id,
            ),
        )

    async def save_selection(self, selection: SessionCapabilitySelection) -> None:
        key = (selection.session_id, selection.kind, selection.capability_id)
        existing = self._selections.get(key)
        if existing is not None and _pinned_fields(existing) != _pinned_fields(selection):
            raise RepositoryConflictError(
                "Running Agent Session cannot replace a pinned Capability"
            )
        self._selections[key] = selection

    async def replace_selection(
        self,
        selection: SessionCapabilitySelection,
        *,
        expected_digest: str,
    ) -> None:
        key = (selection.session_id, selection.kind, selection.capability_id)
        existing = self._selections.get(key)
        if existing is None or existing.digest != expected_digest:
            raise RepositoryConflictError("Capability reload digest no longer matches")
        if _scope_fields(existing) != _scope_fields(selection):
            raise RepositoryConflictError("Capability reload changed its Session scope")
        self._selections[key] = selection

    async def get_allowlist(
        self,
        session_id: str,
        kind: CapabilityKind,
    ) -> frozenset[str] | None:
        return self._allowlists.get((session_id, kind))

    async def set_allowlist(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        kind: CapabilityKind,
        capability_ids: list[str],
    ) -> None:
        del run_id, agent_id
        key = (session_id, kind)
        value = frozenset(capability_ids)
        existing = self._allowlists.get(key)
        if existing is not None and existing != value:
            raise RepositoryConflictError("Capability allowlist is immutable after delegation")
        self._allowlists[key] = value


def _scope_fields(selection: SessionCapabilitySelection) -> tuple[object, ...]:
    return (
        selection.run_id,
        selection.session_id,
        selection.agent_id,
        selection.kind,
        selection.capability_id,
    )


def _pinned_fields(selection: SessionCapabilitySelection) -> tuple[object, ...]:
    return (
        *_scope_fields(selection),
        selection.version,
        selection.digest,
        selection.source,
        selection.reason,
        selection.snapshot,
        selection.selected_at,
    )
