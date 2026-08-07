from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from riftx.application.errors import RepositoryConflictError, RepositoryIntegrityError
from riftx.capabilities import (
    CAPABILITY_PACK_SCHEMA_VERSION,
    CAPABILITY_SCHEMA_VERSION,
    Capability,
    CapabilityDependency,
    CapabilityDependencyKind,
    CapabilityEffectClass,
    CapabilityKind,
    CapabilityManifest,
    CapabilityPack,
    CapabilityPackManifest,
    CapabilityPackMember,
    CapabilityPermission,
    CapabilityProvenance,
    CapabilitySource,
    CapabilityTrustTier,
    CapabilityVersion,
    CapabilityVersionStatus,
    ConfirmationPolicy,
    EvidenceContract,
    PackInstall,
    PackInstallStatus,
    PackLock,
    PackLockOwnerKind,
    PackStatus,
    capability_manifest_digest,
    capability_pack_digest,
)
from riftx.capabilities.models import (
    CapabilityCandidate,
    CapabilityCandidateStatus,
    CapabilityEvaluationResult,
    EvaluationResultStatus,
    PromotionRun,
    PromotionStatus,
    evaluation_report_digest,
)
from riftx.domain.enums import ApprovalLevel
from riftx.persistence import Database, SQLAlchemyCapabilityRepository

NOW = datetime(2026, 8, 5, 11, 0, tzinfo=UTC)


def manifest(*, version: str, title: str | None = None) -> CapabilityManifest:
    return CapabilityManifest(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        capability_id="web.request-analysis",
        version=version,
        kind=CapabilityKind.TECHNIQUE,
        title=title or f"Web request analysis {version}",
        description="Compare bounded request and response evidence.",
        domains=("web", "traffic"),
        triggers=("request diff",),
        dependencies=(
            CapabilityDependency(
                kind=CapabilityDependencyKind.TOOL,
                reference="target_http",
                version_constraint=">=1.0.0",
            ),
        ),
        permission=CapabilityPermission(
            effect_class=CapabilityEffectClass.TARGET_INTERACTION,
            approval_level=ApprovalLevel.SENSITIVE,
            requires_scope=True,
        ),
        evidence_contract=EvidenceContract(
            required_refs=("request", "response"),
            minimum_independent_sources=1,
            confirmation_policy=ConfirmationPolicy.EXPLICIT_VERIFICATION,
        ),
        provenance=CapabilityProvenance(
            publisher="riftx",
            source=CapabilitySource.OFFICIAL,
            source_reference=f"builtin://web/request-analysis/{version}",
            authored_by="riftx-maintainers",
            authored_at=NOW,
            source_digest="a" * 64,
        ),
        evaluation_case_ids=("eval.web.request-analysis",),
        trust_tier=CapabilityTrustTier.OFFICIAL,
    )


def version(
    value: str,
    *,
    version_id: str | None = None,
    status: CapabilityVersionStatus = CapabilityVersionStatus.ACTIVE,
    title: str | None = None,
) -> tuple[Capability, CapabilityVersion]:
    capability_manifest = manifest(version=value, title=title)
    capability = Capability(
        capability_id=capability_manifest.capability_id,
        kind=capability_manifest.kind,
        created_at=NOW,
    )
    activated_at = NOW if status is not CapabilityVersionStatus.APPROVED else None
    return capability, CapabilityVersion(
        version_id=version_id or f"version-{value}",
        manifest=capability_manifest,
        manifest_digest=capability_manifest_digest(capability_manifest),
        status=status,
        created_at=NOW,
        activated_at=activated_at,
    )


def pack(
    capability_version: CapabilityVersion,
    *,
    pack_version: str,
) -> CapabilityPack:
    pack_manifest = CapabilityPackManifest(
        schema_version=CAPABILITY_PACK_SCHEMA_VERSION,
        pack_id="official.web-foundation",
        version=pack_version,
        title=f"Official Web Foundation {pack_version}",
        description="Baseline web assessment capabilities.",
        source=CapabilitySource.OFFICIAL,
        publisher="riftx",
        members=(
            CapabilityPackMember(
                capability_id=capability_version.manifest.capability_id,
                version=capability_version.manifest.version,
                version_digest=capability_version.manifest_digest,
            ),
        ),
        provenance=CapabilityProvenance(
            publisher="riftx",
            source=CapabilitySource.OFFICIAL,
            source_reference=f"builtin://packs/web-foundation/{pack_version}",
            authored_by="riftx-maintainers",
            authored_at=NOW,
            source_digest="b" * 64,
        ),
    )
    return CapabilityPack(
        pack_version_id=f"pack-version-{pack_version}",
        manifest=pack_manifest,
        manifest_digest=capability_pack_digest(pack_manifest),
        status=PackStatus.ACTIVE,
        created_at=NOW,
    )


def lock(
    capability_version: CapabilityVersion,
    *,
    lock_id: str,
    owner_kind: PackLockOwnerKind,
    owner_id: str,
    acquired_at: datetime = NOW,
) -> PackLock:
    return PackLock(
        lock_id=lock_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        capability_id=capability_version.manifest.capability_id,
        capability_version_id=capability_version.version_id,
        capability_version=capability_version.manifest.version,
        capability_digest=capability_version.manifest_digest,
        acquired_at=acquired_at,
    )


async def test_version_registration_is_idempotent_immutable_and_restart_safe(
    tmp_path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}"
    database = Database(database_url)
    await database.create_schema()
    repository = SQLAlchemyCapabilityRepository(database.session_factory)
    capability, first = version("1.0.0")

    assert await repository.register_version(capability, first) == first
    replay_capability, replay = version("1.0.0", version_id="version-replay")
    assert await repository.register_version(replay_capability, replay) == first
    _, overwritten = version(
        "1.0.0",
        version_id="version-overwrite",
        title="Different immutable content",
    )
    with pytest.raises(RepositoryConflictError, match="cannot be overwritten"):
        await repository.register_version(capability, overwritten)

    candidate_manifest = manifest(version="1.1.0")
    candidate = CapabilityCandidate(
        candidate_id="candidate-1",
        proposed_manifest=candidate_manifest,
        candidate_digest=capability_manifest_digest(candidate_manifest),
        status=CapabilityCandidateStatus.DRAFT,
        proposed_by="operator-1",
        source_run_id="run-1",
        created_at=NOW,
        updated_at=NOW,
    )
    assert await repository.create_candidate(candidate) == candidate
    assert await repository.get_candidate(candidate.candidate_id) == candidate
    await database.dispose()

    reopened = Database(database_url)
    await reopened.create_schema()
    restarted = SQLAlchemyCapabilityRepository(reopened.session_factory)
    assert await restarted.get_version(first.version_id) == first
    assert await restarted.get_candidate(candidate.candidate_id) == candidate
    async with reopened.session_factory() as session:
        version_count = await session.scalar(text("SELECT count(*) FROM capability_versions"))
        candidate_count = await session.scalar(
            text("SELECT count(*) FROM capability_candidates")
        )
    assert version_count == candidate_count == 1
    await reopened.dispose()


async def test_active_session_lock_blocks_disable_until_idempotent_release(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyCapabilityRepository(database.session_factory)
    capability, active = version("1.0.0")
    await repository.register_version(capability, active)
    session_lock = lock(
        active,
        lock_id="lock-session-1",
        owner_kind=PackLockOwnerKind.RUN_SESSION,
        owner_id="session-1",
    )

    assert await repository.acquire_version_lock(session_lock) == session_lock
    assert await repository.acquire_version_lock(session_lock) == session_lock
    with pytest.raises(RepositoryConflictError, match="locked by an active owner"):
        await repository.set_version_status(
            active.version_id,
            CapabilityVersionStatus.DISABLED,
            changed_at=NOW + timedelta(minutes=1),
        )

    released = await repository.release_version_locks(
        PackLockOwnerKind.RUN_SESSION,
        "session-1",
        released_at=NOW + timedelta(minutes=2),
    )
    replayed_release = await repository.release_version_locks(
        PackLockOwnerKind.RUN_SESSION,
        "session-1",
        released_at=NOW + timedelta(minutes=2),
    )
    assert released == replayed_release
    disabled = await repository.set_version_status(
        active.version_id,
        CapabilityVersionStatus.DISABLED,
        changed_at=NOW + timedelta(minutes=3),
    )
    assert disabled.status is CapabilityVersionStatus.DISABLED
    assert (
        await repository.set_version_status(
            active.version_id,
            CapabilityVersionStatus.DISABLED,
            changed_at=NOW + timedelta(minutes=4),
        )
        == disabled
    )
    await database.dispose()


async def test_pack_install_disable_and_rollback_are_idempotent(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyCapabilityRepository(database.session_factory)
    capability, first = version("1.0.0")
    _, second = version("2.0.0")
    await repository.register_version(capability, first)
    await repository.register_version(capability, second)
    first_pack = pack(first, pack_version="1.0.0")
    second_pack = pack(second, pack_version="2.0.0")
    await repository.register_pack(first_pack)
    await repository.register_pack(second_pack)
    install = PackInstall(
        install_id="install-web",
        scope_type=CapabilitySource.OPERATOR,
        scope_id="operator-1",
        pack_id=second_pack.manifest.pack_id,
        pack_version_id=second_pack.pack_version_id,
        pack_version=second_pack.manifest.version,
        pack_digest=second_pack.manifest_digest,
        status=PackInstallStatus.INSTALLED,
        state_version=1,
        installed_at=NOW,
        updated_at=NOW,
    )
    second_lock = lock(
        second,
        lock_id="lock-install-v2",
        owner_kind=PackLockOwnerKind.PACK_INSTALL,
        owner_id=install.install_id,
    )

    assert await repository.install_pack(install, (second_lock,)) == install
    assert await repository.install_pack(install, (second_lock,)) == install
    disabled = await repository.disable_pack_install(
        install.install_id,
        disabled_at=NOW + timedelta(minutes=1),
    )
    assert disabled.status is PackInstallStatus.DISABLED
    assert (
        await repository.disable_pack_install(
            install.install_id,
            disabled_at=NOW + timedelta(minutes=2),
        )
        == disabled
    )
    first_lock = lock(
        first,
        lock_id="lock-install-v1",
        owner_kind=PackLockOwnerKind.PACK_INSTALL,
        owner_id=install.install_id,
        acquired_at=NOW + timedelta(minutes=3),
    )
    rolled_back = await repository.rollback_pack_install(
        install.install_id,
        first_pack,
        (first_lock,),
        changed_at=NOW + timedelta(minutes=3),
    )
    assert rolled_back.status is PackInstallStatus.INSTALLED
    assert rolled_back.pack_version_id == first_pack.pack_version_id
    assert rolled_back.previous_pack_version_id == second_pack.pack_version_id
    assert (
        await repository.rollback_pack_install(
            install.install_id,
            first_pack,
            (first_lock,),
            changed_at=NOW + timedelta(minutes=4),
        )
        == rolled_back
    )
    with pytest.raises(RepositoryConflictError, match="locked by an active owner"):
        await repository.set_version_status(
            first.version_id,
            CapabilityVersionStatus.DISABLED,
            changed_at=NOW + timedelta(minutes=5),
        )
    await database.dispose()


async def test_candidate_promotion_is_atomic_and_requires_passing_evaluation(
    tmp_path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyCapabilityRepository(database.session_factory)
    promoted_manifest = manifest(version="3.0.0")
    candidate = CapabilityCandidate(
        candidate_id="candidate-approved",
        proposed_manifest=promoted_manifest,
        candidate_digest=capability_manifest_digest(promoted_manifest),
        status=CapabilityCandidateStatus.APPROVED,
        proposed_by="operator-1",
        source_run_id="run-3",
        created_at=NOW,
        updated_at=NOW,
    )
    promotion = PromotionRun(
        promotion_id="promotion-approved",
        candidate_id=candidate.candidate_id,
        status=PromotionStatus.APPROVED,
        requested_by="operator-1",
        approval_reference="approval-1",
        created_at=NOW,
        updated_at=NOW,
    )
    report = {"passed": True, "scenario": "eval.web.request-analysis"}
    evaluation = CapabilityEvaluationResult(
        result_id="evaluation-passed",
        promotion_id=promotion.promotion_id,
        evaluator="security-eval/v1",
        status=EvaluationResultStatus.PASSED,
        scenario_ids=("eval.web.request-analysis",),
        report=report,
        report_digest=evaluation_report_digest(report),
        created_at=NOW,
    )
    capability = Capability(
        capability_id=promoted_manifest.capability_id,
        kind=promoted_manifest.kind,
        created_at=NOW,
    )
    promoted_version = CapabilityVersion(
        version_id="version-promoted",
        manifest=promoted_manifest,
        manifest_digest=candidate.candidate_digest,
        status=CapabilityVersionStatus.ACTIVE,
        created_at=NOW,
        activated_at=NOW + timedelta(minutes=1),
    )

    await repository.create_candidate(candidate)
    await repository.create_promotion(promotion)
    assert await repository.get_version(promoted_version.version_id) is None
    with pytest.raises(RepositoryConflictError, match="passing evaluation"):
        await repository.promote_candidate(
            candidate.candidate_id,
            promotion.promotion_id,
            capability,
            promoted_version,
            approval_reference="approval-1",
            promoted_at=NOW + timedelta(minutes=1),
        )
    await repository.add_evaluation_result(evaluation)
    assert (
        await repository.promote_candidate(
            candidate.candidate_id,
            promotion.promotion_id,
            capability,
            promoted_version,
            approval_reference="approval-1",
            promoted_at=NOW + timedelta(minutes=1),
        )
        == promoted_version
    )
    promoted_candidate = await repository.get_candidate(candidate.candidate_id)
    assert promoted_candidate is not None
    assert promoted_candidate.status is CapabilityCandidateStatus.PROMOTED
    assert promoted_candidate.promoted_version_id == promoted_version.version_id
    await database.dispose()


async def test_redundant_permission_corruption_fails_closed(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyCapabilityRepository(database.session_factory)
    capability, active = version("1.0.0")
    await repository.register_version(capability, active)
    async with database.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE capability_permissions SET effect_class = 'read_only' "
                "WHERE version_id = :version_id"
            ),
            {"version_id": active.version_id},
        )

    with pytest.raises(RepositoryIntegrityError):
        await repository.get_version(active.version_id)
    await database.dispose()
