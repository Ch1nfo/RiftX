"""Read-only database migration and Official Pack consistency diagnostics."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.capabilities import PackInstallStatus, PackLockOwnerKind
from riftx.packs import OFFICIAL_PACK_SCOPE_ID, OfficialPackBundle, OfficialPackCatalog
from riftx.persistence.capability_records import (
    CapabilityPackInstallRecord,
    CapabilityPackLockRecord,
    CapabilityPackMemberRecord,
    CapabilityPackRecord,
    CapabilityVersionRecord,
)

ALEMBIC_HEAD_REVISION = "3c6e8a1f2b40"


class _DiagnosticsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatabaseMigrationDiagnostics(_DiagnosticsModel):
    status: Literal["ready", "unmanaged", "mismatch"]
    expected_revision: str
    current_revisions: tuple[str, ...] = ()


class OfficialPackDiagnostics(_DiagnosticsModel):
    status: Literal["ready", "drifted"]
    expected_pack_count: int
    installed_pack_count: int
    active_lock_count: int
    issues: tuple[str, ...] = ()


class SystemDiagnosticsSnapshot(_DiagnosticsModel):
    database: DatabaseMigrationDiagnostics
    official_packs: OfficialPackDiagnostics


class SystemDiagnosticsService:
    """Read authoritative persistence state without exposing stored content."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        catalog: OfficialPackCatalog | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._catalog = catalog or OfficialPackCatalog()

    async def snapshot(self) -> SystemDiagnosticsSnapshot:
        bundles = self._catalog.load()
        async with self._session_factory() as session:
            connection = await session.connection()
            tables = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            database = await _database_diagnostics(session, tables)
            packs = await _official_pack_diagnostics(session, tables, bundles)
        return SystemDiagnosticsSnapshot(database=database, official_packs=packs)


async def _database_diagnostics(
    session: AsyncSession,
    tables: set[str],
) -> DatabaseMigrationDiagnostics:
    if "alembic_version" not in tables:
        return DatabaseMigrationDiagnostics(
            status="unmanaged",
            expected_revision=ALEMBIC_HEAD_REVISION,
        )
    revisions = tuple(
        sorted(
            str(value)
            for value in (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalars()
        )
    )
    return DatabaseMigrationDiagnostics(
        status=("ready" if revisions == (ALEMBIC_HEAD_REVISION,) else "mismatch"),
        expected_revision=ALEMBIC_HEAD_REVISION,
        current_revisions=revisions,
    )


async def _official_pack_diagnostics(
    session: AsyncSession,
    tables: set[str],
    bundles: tuple[OfficialPackBundle, ...],
) -> OfficialPackDiagnostics:
    required_tables = {
        "capability_versions",
        "capability_packs",
        "capability_pack_members",
        "capability_pack_installs",
        "capability_pack_locks",
    }
    if not required_tables.issubset(tables):
        return OfficialPackDiagnostics(
            status="drifted",
            expected_pack_count=len(bundles),
            installed_pack_count=0,
            active_lock_count=0,
            issues=("capability_pack_tables_missing",),
        )

    installs = tuple(
        (
            await session.scalars(
                select(CapabilityPackInstallRecord).where(
                    CapabilityPackInstallRecord.scope_type == "official",
                    CapabilityPackInstallRecord.scope_id == OFFICIAL_PACK_SCOPE_ID,
                )
            )
        ).all()
    )
    install_ids = {record.id for record in installs}
    packs = tuple((await session.scalars(select(CapabilityPackRecord))).all())
    versions = tuple((await session.scalars(select(CapabilityVersionRecord))).all())
    members = tuple((await session.scalars(select(CapabilityPackMemberRecord))).all())
    locks = tuple(
        (
            await session.scalars(
                select(CapabilityPackLockRecord).where(
                    CapabilityPackLockRecord.owner_kind == PackLockOwnerKind.PACK_INSTALL.value,
                    CapabilityPackLockRecord.released_at.is_(None),
                )
            )
        ).all()
    )
    active_locks = tuple(lock for lock in locks if lock.owner_id in install_ids)
    installs_by_pack = {record.pack_id: record for record in installs}
    packs_by_id = {record.id: record for record in packs}
    versions_by_id = {record.id: record for record in versions}
    members_by_pack: dict[str, list[CapabilityPackMemberRecord]] = {}
    for member in members:
        members_by_pack.setdefault(member.pack_version_id, []).append(member)
    locks_by_owner: dict[str, list[CapabilityPackLockRecord]] = {}
    for lock in active_locks:
        locks_by_owner.setdefault(lock.owner_id, []).append(lock)

    issues: list[str] = []
    expected_ids = {bundle.source.pack_id for bundle in bundles}
    for unexpected in sorted(set(installs_by_pack) - expected_ids):
        issues.append(f"unexpected_install:{unexpected}")
    for bundle in bundles:
        pack_id = bundle.source.pack_id
        expected_pack = bundle.pack
        persisted_pack = packs_by_id.get(expected_pack.pack_version_id)
        if persisted_pack is not None and (
            persisted_pack.pack_id != pack_id
            or persisted_pack.version != expected_pack.manifest.version
            or persisted_pack.status != expected_pack.status.value
            or persisted_pack.manifest_json != expected_pack.manifest.model_dump(mode="json")
            or persisted_pack.manifest_digest != expected_pack.manifest_digest
        ):
            issues.append(f"pack_digest_drift:{pack_id}")

        if any(
            (record := versions_by_id.get(version.version_id)) is not None
            and (
                record.capability_id != version.manifest.capability_id
                or record.version != version.manifest.version
                or record.status != version.status.value
                or record.manifest_json != version.manifest.model_dump(mode="json")
                or record.manifest_digest != version.manifest_digest
            )
            for version in bundle.capability_versions
        ):
            issues.append(f"capability_version_drift:{pack_id}")

        actual_members = sorted(
            members_by_pack.get(expected_pack.pack_version_id, []),
            key=lambda item: item.position,
        )
        expected_versions = {
            version.manifest.capability_id: version for version in bundle.capability_versions
        }
        if persisted_pack is not None and (
            len(actual_members) != len(expected_pack.manifest.members)
            or any(
                actual.position != position
                or actual.capability_id != expected.capability_id
                or actual.capability_version_id
                != expected_versions[expected.capability_id].version_id
                or actual.capability_version != expected.version
                or actual.capability_digest != expected.version_digest
                for position, (actual, expected) in enumerate(
                    zip(actual_members, expected_pack.manifest.members, strict=False)
                )
            )
        ):
            issues.append(f"pack_member_drift:{pack_id}")

        install = installs_by_pack.get(pack_id)
        if install is None:
            issues.append(f"missing_install:{pack_id}")
            continue
        if (
            install.status != PackInstallStatus.INSTALLED.value
            or install.pack_version_id != expected_pack.pack_version_id
            or install.pack_version != expected_pack.manifest.version
            or install.pack_digest != expected_pack.manifest_digest
        ):
            issues.append(f"install_drift:{pack_id}")
        installed_pack = packs_by_id.get(install.pack_version_id)
        if (
            installed_pack is None
            or installed_pack.pack_id != pack_id
            or installed_pack.version != expected_pack.manifest.version
            or installed_pack.manifest_digest != expected_pack.manifest_digest
        ):
            issue = f"pack_digest_drift:{pack_id}"
            if issue not in issues:
                issues.append(issue)

        owner_locks = locks_by_owner.get(install.id, [])
        actual_locks = {lock.capability_id: lock for lock in owner_locks}
        if len(owner_locks) != len(expected_versions) or set(actual_locks) != set(
            expected_versions
        ):
            issues.append(f"lock_set_drift:{pack_id}")
            continue
        if any(
            actual_locks[capability_id].capability_version_id != version.version_id
            or actual_locks[capability_id].capability_version != str(version.manifest.version)
            or actual_locks[capability_id].capability_digest != version.manifest_digest
            for capability_id, version in expected_versions.items()
        ):
            issues.append(f"lock_digest_drift:{pack_id}")

    return OfficialPackDiagnostics(
        status="ready" if not issues else "drifted",
        expected_pack_count=len(bundles),
        installed_pack_count=sum(
            record.status == PackInstallStatus.INSTALLED.value for record in installs
        ),
        active_lock_count=len(active_locks),
        issues=tuple(issues),
    )


__all__ = [
    "ALEMBIC_HEAD_REVISION",
    "DatabaseMigrationDiagnostics",
    "OfficialPackDiagnostics",
    "SystemDiagnosticsService",
    "SystemDiagnosticsSnapshot",
]
