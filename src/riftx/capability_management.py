"""Read-only local Capability and Official Pack inventory."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from riftx.application.errors import RepositoryError
from riftx.capabilities import CapabilityKind, CapabilityVersion
from riftx.config import RiftXConfig
from riftx.database_maintenance import SQLiteMigrationStatus, inspect_sqlite_migration
from riftx.diagnostics import SystemDiagnosticsService, SystemDiagnosticsSnapshot
from riftx.packs import OfficialPackCatalog
from riftx.persistence import Database, SQLAlchemyCapabilityRepository


class CapabilityManagementError(RuntimeError):
    """Raised when local Capability state cannot be read authoritatively."""


@dataclass(frozen=True, slots=True)
class CapabilityInventoryItem:
    capability_id: str
    version: str
    kind: str
    source: str
    trust_tier: str
    status: str
    manifest_digest: str


@dataclass(frozen=True, slots=True)
class PackInventoryItem:
    pack_id: str
    version: str
    capability_count: int
    persistence_status: Literal["ready", "missing", "drifted", "unavailable"]
    manifest_digest: str


@dataclass(frozen=True, slots=True)
class LocalCapabilityState:
    capabilities: tuple[CapabilityInventoryItem, ...]
    packs: tuple[PackInventoryItem, ...]
    verification_status: Literal["ready", "drifted"]
    issues: tuple[str, ...]


def inspect_local_capability_state(
    config: RiftXConfig,
    *,
    cwd: Path | None = None,
    catalog: OfficialPackCatalog | None = None,
) -> LocalCapabilityState:
    """Read active Capability versions and verify Official Pack persistence."""

    if not isinstance(config, RiftXConfig):
        raise CapabilityManagementError("Capability configuration is invalid")
    working_directory = Path.cwd() if cwd is None else cwd
    database_url = _ready_database_url(config.database.url, working_directory)
    pack_catalog = catalog or OfficialPackCatalog()
    try:
        bundles = pack_catalog.load()
    except (OSError, TypeError, ValueError) as exc:
        raise CapabilityManagementError(f"Official Pack catalog is invalid: {exc}") from exc

    async def read() -> tuple[tuple[CapabilityVersion, ...], SystemDiagnosticsSnapshot]:
        database = Database(database_url)
        try:
            repository = SQLAlchemyCapabilityRepository(database.session_factory)
            versions: list[CapabilityVersion] = []
            for kind in CapabilityKind:
                versions.extend(await repository.list_active_versions(kind))
            diagnostics = await SystemDiagnosticsService(
                database.session_factory,
                pack_catalog,
            ).snapshot()
            return tuple(versions), diagnostics
        finally:
            await database.dispose()

    try:
        versions, diagnostics = asyncio.run(read())
    except (OSError, RepositoryError, SQLAlchemyError, TypeError, ValueError) as exc:
        raise CapabilityManagementError(
            f"Capability persistence could not be read: {exc}"
        ) from exc

    capabilities = tuple(
        sorted(
            (
                CapabilityInventoryItem(
                    capability_id=version.manifest.capability_id,
                    version=version.manifest.version,
                    kind=version.manifest.kind.value,
                    source=version.manifest.provenance.source.value,
                    trust_tier=version.manifest.trust_tier.value,
                    status=version.status.value,
                    manifest_digest=version.manifest_digest,
                )
                for version in versions
            ),
            key=lambda item: (item.capability_id, item.version, item.manifest_digest),
        )
    )
    pack_diagnostics = diagnostics.official_packs
    issues = pack_diagnostics.issues
    global_unavailable = any(":" not in issue for issue in issues)
    packs = tuple(
        PackInventoryItem(
            pack_id=bundle.source.pack_id,
            version=bundle.source.version,
            capability_count=len(bundle.capability_versions),
            persistence_status=_pack_status(
                bundle.source.pack_id,
                issues,
                global_unavailable=global_unavailable,
            ),
            manifest_digest=bundle.pack.manifest_digest,
        )
        for bundle in bundles
    )
    return LocalCapabilityState(
        capabilities=capabilities,
        packs=packs,
        verification_status=(
            "ready"
            if diagnostics.database.status == "ready" and pack_diagnostics.status == "ready"
            else "drifted"
        ),
        issues=issues,
    )


def _ready_database_url(database_url: str, cwd: Path) -> str:
    state = inspect_sqlite_migration(database_url, cwd=cwd)
    if state is None:
        raise CapabilityManagementError(
            "Local Capability inventory requires file-backed SQLite."
        )
    if state.status is SQLiteMigrationStatus.MISSING:
        raise CapabilityManagementError(
            "Local Capability database is not initialized; run `riftx onboard`."
        )
    if state.status is not SQLiteMigrationStatus.READY:
        raise CapabilityManagementError(
            f"Local Capability database is {state.status.value}; run `riftx doctor --fix`."
        )
    try:
        url = make_url(database_url)
    except ArgumentError as exc:
        raise CapabilityManagementError(f"Database URL is invalid: {exc}") from exc
    assert url.database is not None
    path = Path(str(url.database)).expanduser()
    if not path.is_absolute():
        path = cwd / path
    absolute = Path(os.path.abspath(os.fspath(path)))
    return url.set(
        drivername="sqlite+aiosqlite",
        database=str(absolute),
    ).render_as_string(hide_password=False)


def _pack_status(
    pack_id: str,
    issues: tuple[str, ...],
    *,
    global_unavailable: bool,
) -> Literal["ready", "missing", "drifted", "unavailable"]:
    if global_unavailable:
        return "unavailable"
    pack_issues = tuple(issue for issue in issues if issue.endswith(f":{pack_id}"))
    if f"missing_install:{pack_id}" in pack_issues:
        return "missing"
    return "drifted" if pack_issues else "ready"


__all__ = [
    "CapabilityInventoryItem",
    "CapabilityManagementError",
    "LocalCapabilityState",
    "PackInventoryItem",
    "inspect_local_capability_state",
]
