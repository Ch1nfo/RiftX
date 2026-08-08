"""Local Capability inventory and Operator Skill lifecycle commands."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast
from uuid import NAMESPACE_URL, uuid5

from pydantic import JsonValue
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from riftx.application.errors import RepositoryError
from riftx.capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    Capability,
    CapabilityEffectClass,
    CapabilityKind,
    CapabilityManifest,
    CapabilityPermission,
    CapabilityProvenance,
    CapabilitySource,
    CapabilityTrustTier,
    CapabilityVersion,
    CapabilityVersionStatus,
    ConfirmationPolicy,
    EvidenceContract,
    capability_manifest_digest,
)
from riftx.config import RiftXConfig
from riftx.database_maintenance import SQLiteMigrationStatus, inspect_sqlite_migration
from riftx.diagnostics import SystemDiagnosticsService, SystemDiagnosticsSnapshot
from riftx.domain import ApprovalLevel
from riftx.domain.base import utc_now
from riftx.packs import OfficialPackCatalog
from riftx.persistence import Database, SQLAlchemyCapabilityRepository
from riftx.skills import (
    ProgressiveSkillRegistry,
    SkillDocument,
    SkillDocumentError,
    SkillPackageRoot,
)


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


@dataclass(frozen=True, slots=True)
class OperatorSkillInventoryItem:
    skill_id: str
    version: str
    source_digest: str
    source_status: Literal["ready", "missing", "drifted"]
    capability_status: str
    version_id: str | None
    manifest_digest: str | None


def validate_operator_skills(
    config: RiftXConfig,
    skill_id: str | None = None,
    *,
    cwd: Path | None = None,
) -> tuple[SkillDocument, ...]:
    """Validate Operator Skill packages without touching persistence."""

    documents = _operator_skill_documents(config, cwd=cwd)
    if skill_id is None:
        return tuple(documents.values())
    try:
        return (documents[skill_id],)
    except KeyError:
        raise CapabilityManagementError(
            f"Operator Skill {skill_id!r} was not found in {_operator_skill_root(config, cwd)}."
        ) from None


def register_operator_skill(
    config: RiftXConfig,
    skill_id: str,
    *,
    cwd: Path | None = None,
    changed_at: datetime | None = None,
) -> CapabilityVersion:
    """Register one validated Operator Skill as an approved immutable version."""

    document = validate_operator_skills(config, skill_id, cwd=cwd)[0]
    now = changed_at or utc_now()

    async def operation(repository: SQLAlchemyCapabilityRepository) -> CapabilityVersion:
        versions = await repository.list_versions(skill_id)
        existing = _registered_operator_version(versions, document.version)
        if existing is not None:
            if existing.manifest.provenance.source_digest == document.digest:
                return existing
            raise CapabilityManagementError(
                f"Operator Skill {skill_id!r} version {document.version!r} has changed; "
                "increase the Skill version before registering it again."
            )
        capability, version = _operator_skill_version(document, created_at=now)
        return await repository.register_version(capability, version)

    return _run_operator_skill_operation(config, operation, cwd=cwd)


def activate_operator_skill(
    config: RiftXConfig,
    skill_id: str,
    version: str,
    *,
    cwd: Path | None = None,
    changed_at: datetime | None = None,
) -> CapabilityVersion:
    """Activate the registered version matching the current local Skill package."""

    document = validate_operator_skills(config, skill_id, cwd=cwd)[0]
    target_version = _require_source_version(document, version)
    now = changed_at or utc_now()

    async def operation(repository: SQLAlchemyCapabilityRepository) -> CapabilityVersion:
        versions = await repository.list_versions(skill_id)
        target = _require_registered_operator_version(
            versions,
            target_version,
            skill_id=skill_id,
        )
        _require_matching_source(target, document)
        # ponytail: lifecycle writes are local-operator commands; the admission
        # resolver rejects duplicate active versions. Add an atomic repository
        # transition only if real multi-writer lifecycle management appears.
        active = _active_operator_versions(
            await repository.list_active_versions(CapabilityKind.SKILL),
            skill_id,
        )
        if len(active) > 1:
            raise CapabilityManagementError(
                f"Operator Skill {skill_id!r} has multiple active versions; repair the "
                "Capability state before activation."
            )
        if active and active[0].version_id != target.version_id:
            raise CapabilityManagementError(
                f"Operator Skill {skill_id!r} already has active version "
                f"{active[0].manifest.version!r}; disable it before activating {version!r}."
            )
        return await repository.set_version_status(
            target.version_id,
            CapabilityVersionStatus.ACTIVE,
            changed_at=now,
        )

    return _run_operator_skill_operation(config, operation, cwd=cwd)


def disable_operator_skill(
    config: RiftXConfig,
    skill_id: str,
    version: str | None = None,
    *,
    cwd: Path | None = None,
    changed_at: datetime | None = None,
) -> CapabilityVersion:
    """Disable one Operator Skill version for future Pentest admissions."""

    now = changed_at or utc_now()

    async def operation(repository: SQLAlchemyCapabilityRepository) -> CapabilityVersion:
        versions = await repository.list_versions(skill_id)
        operator_versions = _operator_versions(versions)
        if version is None:
            active = [
                item
                for item in operator_versions
                if item.status is CapabilityVersionStatus.ACTIVE
            ]
            if len(active) != 1:
                raise CapabilityManagementError(
                    f"Operator Skill {skill_id!r} does not have exactly one active version; "
                    "specify the version explicitly."
                )
            target = active[0]
        else:
            target = _require_registered_operator_version(
                operator_versions,
                version,
                skill_id=skill_id,
            )
        if target.status is CapabilityVersionStatus.DISABLED:
            return target
        if target.status not in {
            CapabilityVersionStatus.ACTIVE,
            CapabilityVersionStatus.DEGRADED,
        }:
            raise CapabilityManagementError(
                f"Operator Skill {skill_id!r} version {target.manifest.version!r} is "
                f"{target.status.value}, not active."
            )
        return await repository.set_version_status(
            target.version_id,
            CapabilityVersionStatus.DISABLED,
            changed_at=now,
        )

    return _run_operator_skill_operation(config, operation, cwd=cwd)


def rollback_operator_skill(
    config: RiftXConfig,
    skill_id: str,
    version: str,
    *,
    cwd: Path | None = None,
    changed_at: datetime | None = None,
) -> CapabilityVersion:
    """Switch admission to an old version after its source package is restored."""

    document = validate_operator_skills(config, skill_id, cwd=cwd)[0]
    target_version = _require_source_version(document, version)
    now = changed_at or utc_now()

    async def operation(repository: SQLAlchemyCapabilityRepository) -> CapabilityVersion:
        versions = await repository.list_versions(skill_id)
        target = _require_registered_operator_version(
            versions,
            target_version,
            skill_id=skill_id,
        )
        _require_matching_source(target, document)
        if target.status is CapabilityVersionStatus.ARCHIVED:
            raise CapabilityManagementError(
                f"Operator Skill {skill_id!r} version {version!r} is archived and cannot be "
                "rolled back."
            )
        active = _active_operator_versions(
            await repository.list_active_versions(CapabilityKind.SKILL),
            skill_id,
        )
        if len(active) > 1:
            raise CapabilityManagementError(
                f"Operator Skill {skill_id!r} has multiple active versions; repair the "
                "Capability state before rollback."
            )
        if active and active[0].version_id == target.version_id:
            return target
        if active:
            await repository.set_version_status(
                active[0].version_id,
                CapabilityVersionStatus.DISABLED,
                changed_at=now,
            )
        try:
            return await repository.set_version_status(
                target.version_id,
                CapabilityVersionStatus.ACTIVE,
                changed_at=now,
            )
        except RepositoryError as exc:
            raise CapabilityManagementError(
                f"Operator Skill {skill_id!r} rollback left no active version; repair the "
                f"target version and run activate again: {exc}"
            ) from exc

    return _run_operator_skill_operation(config, operation, cwd=cwd)


def inspect_operator_skills(
    config: RiftXConfig,
    skill_id: str | None = None,
    *,
    cwd: Path | None = None,
) -> tuple[OperatorSkillInventoryItem, ...]:
    """Join current local packages with their registered lifecycle state."""

    documents = _operator_skill_documents(config, cwd=cwd)

    async def operation(
        repository: SQLAlchemyCapabilityRepository,
    ) -> tuple[OperatorSkillInventoryItem, ...]:
        registered_versions = _operator_versions(
            tuple(
                item
                for item in await repository.list_versions_by_kind(CapabilityKind.SKILL)
                if item.manifest.provenance.source is CapabilitySource.OPERATOR
            )
        )
        ids = set(documents)
        ids.update(item.manifest.capability_id for item in registered_versions)
        if skill_id is not None:
            ids = {skill_id}
        items: list[OperatorSkillInventoryItem] = []
        for current_id in sorted(ids):
            document = documents.get(current_id)
            versions = tuple(
                item
                for item in registered_versions
                if item.manifest.capability_id == current_id
            )
            for registered_version in versions:
                source_digest = registered_version.manifest.provenance.source_digest
                assert source_digest is not None
                source_status: Literal["ready", "missing", "drifted"]
                if (
                    document is None
                    or document.version != registered_version.manifest.version
                ):
                    source_status = "missing"
                elif document.digest == source_digest:
                    source_status = "ready"
                else:
                    source_status = "drifted"
                items.append(
                    OperatorSkillInventoryItem(
                        skill_id=current_id,
                        version=registered_version.manifest.version,
                        source_digest=source_digest,
                        source_status=source_status,
                        capability_status=registered_version.status.value,
                        version_id=registered_version.version_id,
                        manifest_digest=registered_version.manifest_digest,
                    )
                )
            if document is not None and not any(
                item.manifest.version == document.version for item in versions
            ):
                items.append(
                    OperatorSkillInventoryItem(
                        skill_id=current_id,
                        version=document.version,
                        source_digest=document.digest,
                        source_status="ready",
                        capability_status="unregistered",
                        version_id=None,
                        manifest_digest=None,
                    )
                )
        if skill_id is not None and not items:
            raise CapabilityManagementError(
                f"Operator Skill {skill_id!r} has no local package or registered version."
            )
        return tuple(items)

    return _run_operator_skill_operation(config, operation, cwd=cwd)


def _operator_skill_documents(
    config: RiftXConfig,
    *,
    cwd: Path | None,
) -> dict[str, SkillDocument]:
    if not isinstance(config, RiftXConfig):
        raise CapabilityManagementError("Capability configuration is invalid")
    root = _operator_skill_root(config, cwd)
    try:
        documents = ProgressiveSkillRegistry(
            (
                SkillPackageRoot(
                    root,
                    expected_source=CapabilitySource.OPERATOR,
                ),
            )
        ).validate()
    except (OSError, UnicodeError, SkillDocumentError, TypeError, ValueError) as exc:
        raise CapabilityManagementError(f"Operator Skills are invalid: {exc}") from exc
    return {document.id: document for document in documents}


def _operator_skill_root(config: RiftXConfig, cwd: Path | None) -> Path:
    working_directory = Path.cwd() if cwd is None else cwd
    root = config.skills.path.expanduser()
    return root if root.is_absolute() else working_directory / root


def _operator_skill_version(
    document: SkillDocument,
    *,
    created_at: datetime,
) -> tuple[Capability, CapabilityVersion]:
    try:
        manifest = CapabilityManifest(
            schema_version=CAPABILITY_SCHEMA_VERSION,
            capability_id=document.id,
            version=document.version,
            kind=CapabilityKind.SKILL,
            title=document.name,
            description=document.description,
            domains=("pentest", "operator-skill"),
            permission=CapabilityPermission(
                effect_class=CapabilityEffectClass.TARGET_INTERACTION,
                approval_level=ApprovalLevel.ALWAYS,
                requires_scope=True,
            ),
            input_schema=cast(dict[str, JsonValue], document.input_schema or {}),
            output_schema=cast(dict[str, JsonValue], document.output_schema or {}),
            evidence_contract=EvidenceContract(
                required_refs=("run_report",),
                minimum_independent_sources=1,
                confirmation_policy=ConfirmationPolicy.MANUAL_REVIEW,
            ),
            provenance=CapabilityProvenance(
                publisher="local-operator",
                source=CapabilitySource.OPERATOR,
                source_reference=(
                    f"operator://skills/{document.id}/{document.version}"
                ),
                authored_by="local-operator",
                authored_at=created_at,
                source_digest=document.digest,
            ),
            trust_tier=CapabilityTrustTier.LOCAL,
        )
    except ValueError as exc:
        raise CapabilityManagementError(
            f"Operator Skill {document.id!r} cannot be registered: {exc}"
        ) from exc
    capability = Capability(
        capability_id=document.id,
        kind=CapabilityKind.SKILL,
        created_at=created_at,
    )
    version = CapabilityVersion(
        version_id=str(
            uuid5(
                NAMESPACE_URL,
                "riftx:operator-skill-version:"
                f"{document.id}:{document.version}:{document.digest}",
            )
        ),
        manifest=manifest,
        manifest_digest=capability_manifest_digest(manifest),
        status=CapabilityVersionStatus.APPROVED,
        created_at=created_at,
    )
    return capability, version


def _operator_versions(
    versions: tuple[CapabilityVersion, ...] | list[CapabilityVersion],
) -> tuple[CapabilityVersion, ...]:
    if any(
        item.manifest.kind is not CapabilityKind.SKILL
        or item.manifest.provenance.source is not CapabilitySource.OPERATOR
        for item in versions
    ):
        capability_id = versions[0].manifest.capability_id if versions else "unknown"
        raise CapabilityManagementError(
            f"Capability ID {capability_id!r} is not owned by an Operator Skill."
        )
    result = tuple(versions)
    if any(item.manifest.provenance.source_digest is None for item in result):
        raise CapabilityManagementError("Operator Skill Capability provenance is incomplete.")
    return result


def _registered_operator_version(
    versions: tuple[CapabilityVersion, ...] | list[CapabilityVersion],
    version: str,
) -> CapabilityVersion | None:
    return next(
        (
            item
            for item in _operator_versions(versions)
            if item.manifest.version == version
        ),
        None,
    )


def _require_registered_operator_version(
    versions: tuple[CapabilityVersion, ...] | list[CapabilityVersion],
    version: str,
    *,
    skill_id: str,
) -> CapabilityVersion:
    registered = _registered_operator_version(versions, version)
    if registered is None:
        raise CapabilityManagementError(
            f"Operator Skill {skill_id!r} version {version!r} is not registered; "
            "run `riftx skills register` first."
        )
    return registered


def _active_operator_versions(
    versions: tuple[CapabilityVersion, ...],
    skill_id: str,
) -> tuple[CapabilityVersion, ...]:
    return tuple(
        item
        for item in versions
        if item.manifest.capability_id == skill_id
        and item.manifest.kind is CapabilityKind.SKILL
        and item.manifest.provenance.source is CapabilitySource.OPERATOR
    )


def _require_source_version(document: SkillDocument, version: str) -> str:
    if document.version != version:
        raise CapabilityManagementError(
            f"Operator Skill {document.id!r} source is version {document.version!r}, not "
            f"{version!r}; restore the requested source package first."
        )
    return version


def _require_matching_source(
    version: CapabilityVersion,
    document: SkillDocument,
) -> None:
    provenance = version.manifest.provenance
    if (
        version.manifest.capability_id != document.id
        or version.manifest.version != document.version
        or version.manifest.kind is not CapabilityKind.SKILL
        or provenance.source is not CapabilitySource.OPERATOR
        or provenance.source_digest != document.digest
    ):
        raise CapabilityManagementError(
            f"Operator Skill {document.id!r} source does not match registered version "
            f"{version.manifest.version!r}; expected digest {provenance.source_digest}."
        )


def _run_operator_skill_operation[T](
    config: RiftXConfig,
    operation: Callable[[SQLAlchemyCapabilityRepository], Awaitable[T]],
    *,
    cwd: Path | None,
) -> T:
    if not isinstance(config, RiftXConfig):
        raise CapabilityManagementError("Capability configuration is invalid")
    working_directory = Path.cwd() if cwd is None else cwd
    database_url = _ready_database_url(config.database.url, working_directory)

    async def run() -> T:
        database = Database(database_url)
        try:
            return await operation(SQLAlchemyCapabilityRepository(database.session_factory))
        finally:
            await database.dispose()

    try:
        return asyncio.run(run())
    except CapabilityManagementError:
        raise
    except (OSError, RepositoryError, SQLAlchemyError, TypeError, ValueError) as exc:
        raise CapabilityManagementError(
            f"Operator Skill persistence operation failed: {exc}"
        ) from exc


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
    "OperatorSkillInventoryItem",
    "CapabilityInventoryItem",
    "CapabilityManagementError",
    "LocalCapabilityState",
    "PackInventoryItem",
    "activate_operator_skill",
    "disable_operator_skill",
    "inspect_local_capability_state",
    "inspect_operator_skills",
    "register_operator_skill",
    "rollback_operator_skill",
    "validate_operator_skills",
]
