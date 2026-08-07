"""Persistence port for the authoritative Capability catalog and candidates."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import (
    Capability,
    CapabilityCandidate,
    CapabilityEvaluationResult,
    CapabilityKind,
    CapabilityPack,
    CapabilityVersion,
    CapabilityVersionStatus,
    PackInstall,
    PackLock,
    PackLockOwnerKind,
    PromotionRun,
)


class CapabilityRepository(Protocol):
    async def register_version(
        self,
        capability: Capability,
        version: CapabilityVersion,
    ) -> CapabilityVersion: ...

    async def get_version(self, version_id: str) -> CapabilityVersion | None: ...

    async def list_versions(self, capability_id: str) -> tuple[CapabilityVersion, ...]: ...

    async def list_versions_by_kind(
        self,
        kind: CapabilityKind,
    ) -> tuple[CapabilityVersion, ...]: ...

    async def list_active_versions(
        self,
        kind: CapabilityKind,
    ) -> tuple[CapabilityVersion, ...]: ...

    async def set_version_status(
        self,
        version_id: str,
        status: CapabilityVersionStatus,
        *,
        changed_at: datetime,
    ) -> CapabilityVersion: ...

    async def create_candidate(
        self,
        candidate: CapabilityCandidate,
    ) -> CapabilityCandidate: ...

    async def get_candidate(self, candidate_id: str) -> CapabilityCandidate | None: ...

    async def create_promotion(self, promotion: PromotionRun) -> PromotionRun: ...

    async def add_evaluation_result(
        self,
        result: CapabilityEvaluationResult,
    ) -> CapabilityEvaluationResult: ...

    async def promote_candidate(
        self,
        candidate_id: str,
        promotion_id: str,
        capability: Capability,
        version: CapabilityVersion,
        *,
        approval_reference: str,
        promoted_at: datetime,
    ) -> CapabilityVersion: ...

    async def register_pack(self, pack: CapabilityPack) -> CapabilityPack: ...

    async def install_pack(
        self,
        install: PackInstall,
        locks: tuple[PackLock, ...],
    ) -> PackInstall: ...

    async def disable_pack_install(
        self,
        install_id: str,
        *,
        disabled_at: datetime,
    ) -> PackInstall: ...

    async def rollback_pack_install(
        self,
        install_id: str,
        target_pack: CapabilityPack,
        locks: tuple[PackLock, ...],
        *,
        changed_at: datetime,
    ) -> PackInstall: ...

    async def acquire_version_lock(
        self,
        lock: PackLock,
    ) -> PackLock: ...

    async def release_version_locks(
        self,
        owner_kind: PackLockOwnerKind,
        owner_id: str,
        *,
        released_at: datetime,
    ) -> tuple[PackLock, ...]: ...
