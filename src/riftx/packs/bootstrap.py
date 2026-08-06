"""Install bundled Official Packs into the authoritative Capability catalog."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from riftx.capabilities import (
    Capability,
    CapabilityRepository,
    CapabilitySource,
    PackInstall,
    PackInstallStatus,
    PackLock,
    PackLockOwnerKind,
)

from .catalog import OfficialPackCatalog

OFFICIAL_PACK_SCOPE_ID = "riftx"


async def bootstrap_official_packs(
    repository: CapabilityRepository,
    catalog: OfficialPackCatalog | None = None,
) -> tuple[PackInstall, ...]:
    """Register and pin immutable built-in Packs, failing closed on drift."""

    installs: list[PackInstall] = []
    for bundle in (catalog or OfficialPackCatalog()).load():
        registered_versions = {}
        for version in bundle.capability_versions:
            registered_versions[version.manifest.capability_id] = (
                await repository.register_version(
                    Capability(
                        capability_id=version.manifest.capability_id,
                        kind=version.manifest.kind,
                        created_at=version.created_at,
                    ),
                    version,
                )
            )
        pack = await repository.register_pack(bundle.pack)
        install_id = _stable_id(
            "official-pack-install",
            pack.manifest.pack_id,
            pack.manifest.version,
        )
        install = PackInstall(
            install_id=install_id,
            scope_type=CapabilitySource.OFFICIAL,
            scope_id=OFFICIAL_PACK_SCOPE_ID,
            pack_id=pack.manifest.pack_id,
            pack_version_id=pack.pack_version_id,
            pack_version=pack.manifest.version,
            pack_digest=pack.manifest_digest,
            status=PackInstallStatus.INSTALLED,
            state_version=1,
            installed_at=pack.created_at,
            updated_at=pack.created_at,
        )
        locks = tuple(
            PackLock(
                lock_id=_stable_id(
                    "official-pack-lock",
                    pack.manifest.pack_id,
                    pack.manifest.version,
                    member.capability_id,
                ),
                owner_kind=PackLockOwnerKind.PACK_INSTALL,
                owner_id=install_id,
                capability_id=member.capability_id,
                capability_version_id=registered_versions[
                    member.capability_id
                ].version_id,
                capability_version=member.version,
                capability_digest=member.version_digest,
                acquired_at=pack.created_at,
            )
            for member in pack.manifest.members
        )
        installs.append(await repository.install_pack(install, locks))
    return tuple(installs)


def _stable_id(kind: str, *parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, ":".join(("riftx", kind, *parts))))
