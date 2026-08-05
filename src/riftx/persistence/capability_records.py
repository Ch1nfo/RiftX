"""SQLAlchemy records for the versioned Capability catalog and learning candidates."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .orm import Base
from .types import UTCDateTime


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


class CapabilityRecord(Base):
    __tablename__ = "capabilities"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('tool', 'technique', 'skill', 'playbook', 'knowledge', 'eval_case')",
            name="ck_capabilities_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)


class CapabilityVersionRecord(Base):
    __tablename__ = "capability_versions"
    __table_args__ = (
        UniqueConstraint(
            "capability_id",
            "version",
            name="uq_capability_versions_capability_version",
        ),
        CheckConstraint(
            "schema_version = 'riftx.capability/v1'",
            name="ck_capability_versions_schema",
        ),
        CheckConstraint(
            "status IN ('approved', 'active', 'disabled', 'degraded', "
            "'deprecated', 'archived')",
            name="ck_capability_versions_status",
        ),
        CheckConstraint(
            _lower_hex_digest_check("manifest_digest"),
            name="ck_capability_versions_manifest_digest",
        ),
        CheckConstraint(
            "(status = 'active' AND activated_at IS NOT NULL) OR status <> 'active'",
            name="ck_capability_versions_active_time",
        ),
        CheckConstraint(
            "retired_at IS NULL OR activated_at IS NOT NULL",
            name="ck_capability_versions_retired_requires_activation",
        ),
        Index("ix_capability_versions_status", "status"),
        Index("ix_capability_versions_capability_status", "capability_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    capability_id: Mapped[str] = mapped_column(
        ForeignKey("capabilities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    provenance_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    publisher: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
    activated_at: Mapped[object | None] = mapped_column(UTCDateTime(), nullable=True)
    retired_at: Mapped[object | None] = mapped_column(UTCDateTime(), nullable=True)


class CapabilityDependencyRecord(Base):
    __tablename__ = "capability_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "kind",
            "reference",
            name="uq_capability_dependencies_identity",
        ),
        CheckConstraint(
            "kind IN ('tool', 'skill', 'capability', 'platform')",
            name="ck_capability_dependencies_kind",
        ),
        CheckConstraint("position >= 0", name="ck_capability_dependencies_position"),
    )

    version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    reference: Mapped[str] = mapped_column(String(256), nullable=False)
    version_constraint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    optional: Mapped[bool] = mapped_column(Boolean, nullable=False)


class CapabilityPermissionRecord(Base):
    __tablename__ = "capability_permissions"
    __table_args__ = (
        CheckConstraint(
            "effect_class IN ('read_only', 'local_mutation', 'target_interaction', "
            "'code_execution', 'credential_access', 'external_service')",
            name="ck_capability_permissions_effect_class",
        ),
        CheckConstraint(
            "approval_level IN ('never', 'sensitive', 'always')",
            name="ck_capability_permissions_approval_level",
        ),
        CheckConstraint(
            "effect_class <> 'target_interaction' OR requires_scope = 1",
            name="ck_capability_permissions_target_scope",
        ),
    )

    version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    effect_class: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_level: Mapped[str] = mapped_column(String(32), nullable=False)
    requires_scope: Mapped[bool] = mapped_column(Boolean, nullable=False)
    credential_references_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)


class CapabilityEvidenceContractRecord(Base):
    __tablename__ = "capability_evidence_contracts"
    __table_args__ = (
        CheckConstraint(
            "minimum_independent_sources >= 0",
            name="ck_capability_evidence_contracts_minimum_sources",
        ),
        CheckConstraint(
            "confirmation_policy IN ('explicit_verification', 'independent_sources', "
            "'manual_review')",
            name="ck_capability_evidence_contracts_confirmation_policy",
        ),
        CheckConstraint(
            "confirmation_policy <> 'independent_sources' "
            "OR minimum_independent_sources >= 2",
            name="ck_capability_evidence_contracts_independent_sources",
        ),
    )

    version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    required_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    minimum_independent_sources: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmation_policy: Mapped[str] = mapped_column(String(32), nullable=False)


class CapabilityCandidateRecord(Base):
    __tablename__ = "capability_candidates"
    __table_args__ = (
        UniqueConstraint(
            "capability_id",
            "proposed_version",
            "candidate_digest",
            name="uq_capability_candidates_proposal",
        ),
        CheckConstraint(
            "kind IN ('tool', 'technique', 'skill', 'playbook', 'knowledge', 'eval_case')",
            name="ck_capability_candidates_kind",
        ),
        CheckConstraint(
            "status IN ('draft', 'tested', 'approved', 'rejected', 'promoted')",
            name="ck_capability_candidates_status",
        ),
        CheckConstraint(
            _lower_hex_digest_check("candidate_digest"),
            name="ck_capability_candidates_digest",
        ),
        CheckConstraint(
            "(status = 'promoted' AND promoted_version_id IS NOT NULL) OR "
            "(status <> 'promoted' AND promoted_version_id IS NULL)",
            name="ck_capability_candidates_promotion_shape",
        ),
        Index("ix_capability_candidates_status_created", "status", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False)
    proposed_version: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(256), nullable=False)
    source_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    promoted_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)


class CapabilityPromotionRunRecord(Base):
    __tablename__ = "capability_promotion_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'evaluating', 'waiting_approval', 'approved', "
            "'rejected', 'promoted', 'failed')",
            name="ck_capability_promotion_runs_status",
        ),
        CheckConstraint(
            "status <> 'promoted' OR promoted_version_id IS NOT NULL",
            name="ck_capability_promotion_runs_promoted_version",
        ),
        Index("ix_capability_promotion_runs_candidate", "candidate_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("capability_candidates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(256), nullable=False)
    approval_reference: Mapped[str | None] = mapped_column(String(256), nullable=True)
    promoted_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)


class CapabilityEvaluationResultRecord(Base):
    __tablename__ = "capability_evaluation_results"
    __table_args__ = (
        CheckConstraint(
            "status IN ('passed', 'failed', 'inconclusive')",
            name="ck_capability_evaluation_results_status",
        ),
        CheckConstraint(
            _lower_hex_digest_check("report_digest"),
            name="ck_capability_evaluation_results_digest",
        ),
        Index("ix_capability_evaluation_results_promotion", "promotion_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    promotion_id: Mapped[str] = mapped_column(
        ForeignKey("capability_promotion_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    evaluator: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    report_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    report_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)


class CapabilityPackRecord(Base):
    __tablename__ = "capability_packs"
    __table_args__ = (
        UniqueConstraint("pack_id", "version", name="uq_capability_packs_pack_version"),
        CheckConstraint(
            "schema_version = 'riftx.capability-pack/v1'",
            name="ck_capability_packs_schema",
        ),
        CheckConstraint(
            "status IN ('active', 'deprecated', 'archived')",
            name="ck_capability_packs_status",
        ),
        CheckConstraint(
            "source IN ('official', 'operator', 'organization', 'engagement')",
            name="ck_capability_packs_source",
        ),
        CheckConstraint(
            _lower_hex_digest_check("manifest_digest"),
            name="ck_capability_packs_manifest_digest",
        ),
        Index("ix_capability_packs_pack_status", "pack_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    pack_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    publisher: Mapped[str] = mapped_column(String(256), nullable=False)
    provenance_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)


class CapabilityPackMemberRecord(Base):
    __tablename__ = "capability_pack_members"
    __table_args__ = (
        UniqueConstraint(
            "pack_version_id",
            "capability_id",
            name="uq_capability_pack_members_capability",
        ),
        CheckConstraint(
            _lower_hex_digest_check("capability_digest"),
            name="ck_capability_pack_members_digest",
        ),
    )

    pack_version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_packs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False)
    capability_version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    capability_version: Mapped[str] = mapped_column(String(128), nullable=False)
    capability_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class CapabilityPackInstallRecord(Base):
    __tablename__ = "capability_pack_installs"
    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "scope_id",
            "pack_id",
            name="uq_capability_pack_installs_scope_pack",
        ),
        CheckConstraint(
            "scope_type IN ('official', 'operator', 'organization', 'engagement')",
            name="ck_capability_pack_installs_scope_type",
        ),
        CheckConstraint(
            "status IN ('installed', 'disabled')",
            name="ck_capability_pack_installs_status",
        ),
        CheckConstraint(
            _lower_hex_digest_check("pack_digest"),
            name="ck_capability_pack_installs_pack_digest",
        ),
        CheckConstraint("state_version >= 1", name="ck_capability_pack_installs_state"),
        CheckConstraint(
            "(status = 'disabled' AND disabled_at IS NOT NULL) OR "
            "(status = 'installed' AND disabled_at IS NULL)",
            name="ck_capability_pack_installs_disabled_shape",
        ),
        Index("ix_capability_pack_installs_scope_status", "scope_type", "scope_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    pack_id: Mapped[str] = mapped_column(String(128), nullable=False)
    pack_version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_packs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    pack_version: Mapped[str] = mapped_column(String(128), nullable=False)
    pack_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_pack_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("capability_packs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    installed_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
    disabled_at: Mapped[object | None] = mapped_column(UTCDateTime(), nullable=True)


class CapabilityPackLockRecord(Base):
    __tablename__ = "capability_pack_locks"
    __table_args__ = (
        CheckConstraint(
            "owner_kind IN ('pack_install', 'run_session')",
            name="ck_capability_pack_locks_owner_kind",
        ),
        CheckConstraint(
            _lower_hex_digest_check("capability_digest"),
            name="ck_capability_pack_locks_capability_digest",
        ),
        CheckConstraint(
            "released_at IS NULL OR released_at >= acquired_at",
            name="ck_capability_pack_locks_release_time",
        ),
        Index(
            "ix_capability_pack_locks_owner_active",
            "owner_kind",
            "owner_id",
            "released_at",
        ),
        Index(
            "ix_capability_pack_locks_version_active",
            "capability_version_id",
            "released_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False)
    capability_version_id: Mapped[str] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    capability_version: Mapped[str] = mapped_column(String(128), nullable=False)
    capability_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
    released_at: Mapped[object | None] = mapped_column(UTCDateTime(), nullable=True)
