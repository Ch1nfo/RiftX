"""Install bundled Official Packs into the authoritative Capability catalog."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from riftx.capabilities import PackInstall
from riftx.domain.base import utc_now

from .catalog import OfficialPackBundle, OfficialPackCatalog

OFFICIAL_PACK_SCOPE_ID = "riftx"


class OfficialPackRepository(Protocol):
    async def reconcile_official_packs(
        self,
        bundles: tuple[OfficialPackBundle, ...],
        *,
        scope_id: str,
        changed_at: datetime,
    ) -> tuple[PackInstall, ...]: ...


async def bootstrap_official_packs(
    repository: OfficialPackRepository,
    catalog: OfficialPackCatalog | None = None,
) -> tuple[PackInstall, ...]:
    """Atomically register, install, and repair immutable built-in Packs."""

    return await repository.reconcile_official_packs(
        (catalog or OfficialPackCatalog()).load(),
        scope_id=OFFICIAL_PACK_SCOPE_ID,
        changed_at=utc_now(),
    )
