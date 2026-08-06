"""SQLAlchemy mappings for durable RiftX business state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    column,
)
from sqlalchemy.exc import CompileError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.functions import FunctionElement

from .types import UTCDateTime

ID_LENGTH = 64
AUDIT_ID_LENGTH = 128
AUDIT_TOKEN_LENGTH = 256
TOOL_CALL_INTENT_ID_LENGTH = 128
STATUS_LENGTH = 32


def _lower_hex_digest_check(column: str) -> str:
    """Return a portable CHECK expression for one lowercase SHA-256 digest."""

    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def _optional_lower_hex_digest_check(column: str) -> str:
    return f"{column} IS NULL OR ({_lower_hex_digest_check(column)})"


def _git_object_id_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) IN (40, 64) AND length({remainder}) = 0"


def _optional_git_object_id_check(column: str) -> str:
    return f"{column} IS NULL OR ({_git_object_id_check(column)})"


def _canonical_uuid_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef-":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column}) = 36 AND substr({column}, 9, 1) = '-' "
        f"AND substr({column}, 14, 1) = '-' AND substr({column}, 19, 1) = '-' "
        f"AND substr({column}, 24, 1) = '-' "
        f"AND length(replace({column}, '-', '')) = 32 "
        f"AND length({remainder}) = 0 "
        f"AND {column} <> '00000000-0000-0000-0000-000000000000'"
    )


def _default_artifact_storage_key(context: Any) -> str:
    values = context.get_current_parameters()
    return (
        f"runs/{values['run_id']}/artifacts/{values['id']}/{values['name']}"
    )


def _default_artifact_access_class(context: Any) -> str:
    if context.get_current_parameters().get("audit_id") is not None:
        raise ValueError("Audit-owned Artifacts require an explicit access class")
    return "public_export"


def _default_artifact_ingest_provenance(context: Any) -> dict[str, Any]:
    if context.get_current_parameters().get("audit_id") is not None:
        raise ValueError("Audit-owned Artifacts require explicit ingest provenance")
    return {
        "schema_version": "riftx.artifact-ingest-provenance/v1",
        "method": "legacy_migrated",
        "producer_node_id": None,
        "producer_execution_id": None,
    }


class _ArtifactStorageComponentsAreSafe(FunctionElement):
    """Dialect-portable storage component predicate used by Artifact DDL."""

    type = Boolean()
    inherit_cache = True


class _ArtifactMimeTypeIsSafe(FunctionElement):
    """Dialect-portable printable-ASCII HTTP media type predicate."""

    type = Boolean()
    inherit_cache = True


def _compiled_artifact_columns(
    element: _ArtifactStorageComponentsAreSafe | _ArtifactMimeTypeIsSafe,
    compiler: Any,
    **kwargs: Any,
) -> tuple[str, ...]:
    return tuple(compiler.process(clause, **kwargs) for clause in element.clauses)


@compiles(_ArtifactStorageComponentsAreSafe, "sqlite")
def _compile_safe_artifact_components_sqlite(
    element: _ArtifactStorageComponentsAreSafe,
    compiler: Any,
    **kwargs: Any,
) -> str:
    def safe(component: str, max_length: int) -> str:
        return (
            f"length({component}) BETWEEN 1 AND {max_length} "
            f"AND {component} NOT IN ('.', '..') "
            f"AND instr({component}, '/') = 0 AND instr({component}, '\\') = 0 "
            f"AND instr({component}, char(0)) = 0 "
            f"AND {component} NOT GLOB '*[^ -~]*'"
        )

    return " AND ".join(
        f"({safe(component, max_length)})"
        for component, max_length in zip(
            _compiled_artifact_columns(element, compiler, **kwargs),
            (64, 64, 255),
            strict=True,
        )
    )


@compiles(_ArtifactMimeTypeIsSafe, "sqlite")
def _compile_safe_artifact_mime_type_sqlite(
    element: _ArtifactMimeTypeIsSafe,
    compiler: Any,
    **kwargs: Any,
) -> str:
    (mime_type,) = _compiled_artifact_columns(element, compiler, **kwargs)
    return (
        f"length({mime_type}) BETWEEN 1 AND 255 "
        f"AND {mime_type} = trim({mime_type}) "
        f"AND instr({mime_type}, char(0)) = 0 "
        f"AND {mime_type} NOT GLOB '*[^ -~]*'"
    )


@compiles(_ArtifactStorageComponentsAreSafe, "postgresql")
def _compile_safe_artifact_components_postgresql(
    element: _ArtifactStorageComponentsAreSafe,
    compiler: Any,
    **kwargs: Any,
) -> str:
    def safe(component: str, max_length: int) -> str:
        return (
            f"length({component}) BETWEEN 1 AND {max_length} "
            f"AND {component} NOT IN ('.', '..') "
            f"AND position('/' in {component}) = 0 "
            f"AND position(chr(92) in {component}) = 0 "
            f"AND {component} !~ '[^ -~]'"
        )

    return " AND ".join(
        f"({safe(component, max_length)})"
        for component, max_length in zip(
            _compiled_artifact_columns(element, compiler, **kwargs),
            (64, 64, 255),
            strict=True,
        )
    )


@compiles(_ArtifactMimeTypeIsSafe, "postgresql")
def _compile_safe_artifact_mime_type_postgresql(
    element: _ArtifactMimeTypeIsSafe,
    compiler: Any,
    **kwargs: Any,
) -> str:
    (mime_type,) = _compiled_artifact_columns(element, compiler, **kwargs)
    return (
        f"length({mime_type}) BETWEEN 1 AND 255 "
        f"AND {mime_type} = btrim({mime_type}) "
        f"AND {mime_type} !~ '[^ -~]'"
    )


@compiles(_ArtifactStorageComponentsAreSafe)
def _compile_safe_artifact_components_default(
    element: _ArtifactStorageComponentsAreSafe,
    compiler: Any,
    **kwargs: Any,
) -> str:
    del element, compiler, kwargs
    raise CompileError(
        "Artifact storage safety checks are unsupported for this database dialect"
    )


@compiles(_ArtifactMimeTypeIsSafe)
def _compile_safe_artifact_mime_type_default(
    element: _ArtifactMimeTypeIsSafe,
    compiler: Any,
    **kwargs: Any,
) -> str:
    del element, compiler, kwargs
    raise CompileError(
        "Artifact MIME safety checks are unsupported for this database dialect"
    )


class Base(DeclarativeBase):
    """Declarative metadata root."""


class EngagementRecord(Base):
    __tablename__ = "engagements"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    authorization_reference: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class LocalAuditJobRecord(Base):
    __tablename__ = "local_audit_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'queued', 'scanning', 'completed', 'failed', 'cancelled')",
            name="ck_local_audit_jobs_status",
        ),
        CheckConstraint(
            "state_version >= 1",
            name="ck_local_audit_jobs_state_version",
        ),
        CheckConstraint(
            "total_files >= 0 AND scanned_files >= 0 AND finding_count >= 0 "
            "AND scanned_files <= total_files",
            name="ck_local_audit_jobs_nonnegative_counts",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("source_identity_digest"),
            name="ck_local_audit_jobs_source_identity_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("snapshot_digest"),
            name="ck_local_audit_jobs_snapshot_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("manifest_digest"),
            name="ck_local_audit_jobs_manifest_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("inventory_digest"),
            name="ck_local_audit_jobs_inventory_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("detector_run_digest"),
            name="ck_local_audit_jobs_detector_run_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("report_digest"),
            name="ck_local_audit_jobs_report_digest",
        ),
        CheckConstraint(
            "(status = 'completed' AND source_identity_digest IS NOT NULL "
            "AND snapshot_digest IS NOT NULL AND manifest_digest IS NOT NULL "
            "AND inventory_digest IS NOT NULL AND detector_run_digest IS NOT NULL "
            "AND report_digest IS NOT NULL AND json_report IS NOT NULL "
            "AND markdown_report IS NOT NULL AND scanned_files = total_files "
            "AND failure_code IS NULL AND finished_at IS NOT NULL) OR status <> 'completed'",
            name="ck_local_audit_jobs_completed_result",
        ),
        CheckConstraint(
            "(status = 'failed' AND failure_code IS NOT NULL AND finished_at IS NOT NULL) "
            "OR status <> 'failed'",
            name="ck_local_audit_jobs_failed_result",
        ),
        CheckConstraint(
            "(status = 'cancelled' AND finished_at IS NOT NULL) OR status <> 'cancelled'",
            name="ck_local_audit_jobs_cancelled_result",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="ck_local_audit_jobs_timestamp_order",
        ),
        Index(
            "ix_local_audit_jobs_status_created_id",
            "status",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), primary_key=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    include_paths_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    exclude_paths_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_code: Mapped[str | None] = mapped_column(String(128))
    source_identity_digest: Mapped[str | None] = mapped_column(String(64))
    snapshot_digest: Mapped[str | None] = mapped_column(String(64))
    manifest_digest: Mapped[str | None] = mapped_column(String(64))
    inventory_digest: Mapped[str | None] = mapped_column(String(64))
    detector_run_digest: Mapped[str | None] = mapped_column(String(64))
    report_digest: Mapped[str | None] = mapped_column(String(64))
    total_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scanned_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    findings_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    json_report: Mapped[str | None] = mapped_column(Text)
    markdown_report: Mapped[str | None] = mapped_column(Text)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    queued_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RunRecord(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('general', 'pentest', 'code_audit')",
            name="ck_runs_kind",
        ),
        CheckConstraint(
            "(kind = 'pentest' AND pentest_admission_json IS NOT NULL) OR "
            "(kind <> 'pentest' AND pentest_admission_json IS NULL)",
            name="ck_runs_pentest_admission",
        ),
        Index(
            "uq_runs_id_engagement_kind",
            "id",
            "engagement_id",
            "kind",
            unique=True,
        ),
        Index(
            "uq_runs_id_engagement_kind_node",
            "id",
            "engagement_id",
            "kind",
            "node_id",
            unique=True,
        ),
        Index(
            "uq_runs_id_status",
            "id",
            "status",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    engagement_id: Mapped[str] = mapped_column(
        ForeignKey("engagements.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    success_criteria_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    entry_points_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    approval_mode: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    pentest_admission_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    model_profile: Mapped[str | None] = mapped_column(String(255))
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditProjectRecord(Base):
    __tablename__ = "audit_projects"
    __table_args__ = (
        CheckConstraint(
            "vcs_kind IN ('directory', 'git')",
            name="ck_audit_projects_vcs_kind",
        ),
        CheckConstraint(
            _lower_hex_digest_check("repository_identity_digest"),
            name="ck_audit_projects_repository_digest",
        ),
        CheckConstraint("state_version >= 1", name="ck_audit_projects_state_version"),
        CheckConstraint(
            "updated_at >= created_at",
            name="ck_audit_projects_timestamp_order",
        ),
        UniqueConstraint(
            "repository_identity_digest",
            name="uq_audit_projects_repository_digest",
        ),
        UniqueConstraint(
            "id",
            "engagement_id",
            name="uq_audit_projects_id_engagement",
        ),
        Index(
            "ix_audit_projects_engagement_created_id",
            "engagement_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), primary_key=True)
    engagement_id: Mapped[str] = mapped_column(
        ForeignKey("engagements.id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    vcs_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    repository_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    default_branch: Mapped[str | None] = mapped_column(String(1024))
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SourceSnapshotRecord(Base):
    __tablename__ = "source_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["parent_snapshot_id", "project_id"],
            ["source_snapshots.id", "source_snapshots.project_id"],
            name="fk_source_snapshots_parent_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "source_kind IN ('directory', 'revision', 'working_tree')",
            name="ck_source_snapshots_source_kind",
        ),
        CheckConstraint(
            "(source_kind IN ('directory', 'revision') "
            "AND working_tree_digest IS NULL) OR "
            "(source_kind = 'working_tree' AND working_tree_digest IS NOT NULL)",
            name="ck_source_snapshots_working_tree_digest",
        ),
        CheckConstraint(
            "(parent_snapshot_id IS NULL AND base_tree_digest IS NULL "
            "AND patch_digest IS NULL) OR "
            "(parent_snapshot_id IS NOT NULL AND base_tree_digest IS NOT NULL "
            "AND patch_digest IS NOT NULL)",
            name="ck_source_snapshots_retest_fields",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("base_tree_digest"),
            name="ck_source_snapshots_base_tree_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("patch_digest"),
            name="ck_source_snapshots_patch_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("working_tree_digest"),
            name="ck_source_snapshots_working_tree_sha256",
        ),
        CheckConstraint(
            "(source_kind = 'directory' AND commit_sha IS NULL) OR "
            "(source_kind IN ('revision', 'working_tree') AND commit_sha IS NOT NULL)",
            name="ck_source_snapshots_commit_presence",
        ),
        CheckConstraint(
            _optional_git_object_id_check("commit_sha"),
            name="ck_source_snapshots_commit_sha",
        ),
        CheckConstraint(
            _optional_git_object_id_check("base_commit_sha"),
            name="ck_source_snapshots_base_commit_sha",
        ),
        CheckConstraint(
            _lower_hex_digest_check("tree_digest"),
            name="ck_source_snapshots_tree_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("capture_policy_digest"),
            name="ck_source_snapshots_capture_policy_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("snapshot_digest"),
            name="ck_source_snapshots_snapshot_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("manifest_digest"),
            name="ck_source_snapshots_manifest_digest",
        ),
        CheckConstraint(
            "file_count >= 0 AND total_bytes >= 0",
            name="ck_source_snapshots_nonnegative_counts",
        ),
        CheckConstraint(
            "sealed_at >= created_at",
            name="ck_source_snapshots_timestamp_order",
        ),
        CheckConstraint(
            "parent_snapshot_id IS NULL OR parent_snapshot_id <> id",
            name="ck_source_snapshots_distinct_parent",
        ),
        CheckConstraint(
            "length(content_storage_key) BETWEEN 1 AND 4096 AND "
            "length(manifest_storage_key) BETWEEN 1 AND 4096",
            name="ck_source_snapshots_storage_keys",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            name="uq_source_snapshots_id_project",
        ),
        UniqueConstraint(
            "project_id",
            "snapshot_digest",
            name="uq_source_snapshots_project_digest",
        ),
        Index(
            "ix_source_snapshots_project_created_id",
            "project_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_source_snapshots_parent",
            "parent_snapshot_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("audit_projects.id", ondelete="RESTRICT"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_snapshot_id: Mapped[str | None] = mapped_column(String(AUDIT_ID_LENGTH))
    base_tree_digest: Mapped[str | None] = mapped_column(String(64))
    patch_digest: Mapped[str | None] = mapped_column(String(64))
    commit_sha: Mapped[str | None] = mapped_column(String(128))
    base_commit_sha: Mapped[str | None] = mapped_column(String(128))
    working_tree_digest: Mapped[str | None] = mapped_column(String(64))
    tree_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    capture_policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    materializer_schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_store_version: Mapped[str] = mapped_column(String(128), nullable=False)
    content_storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    sealed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SnapshotReferenceRecord(Base):
    __tablename__ = "snapshot_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["audit_id", "project_id"],
            ["audit_scans.id", "audit_scans.project_id"],
            name="fk_snapshot_references_audit_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "project_id"],
            ["source_snapshots.id", "source_snapshots.project_id"],
            name="fk_snapshot_references_snapshot_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "schema_version = 'riftx.snapshot-reference/v1'",
            name="ck_snapshot_references_schema_version",
        ),
        CheckConstraint(
            "role IN ('primary', 'base', 'baseline', 'finding_evidence', "
            "'retest_parent', 'distribution_revision')",
            name="ck_snapshot_references_role",
        ),
        CheckConstraint(
            _lower_hex_digest_check("reference_digest"),
            name="ck_snapshot_references_digest",
        ),
        Index(
            "ix_snapshot_references_snapshot_project_created",
            "snapshot_id",
            "project_id",
            "created_at",
        ),
        Index(
            "ix_snapshot_references_audit_created",
            "audit_id",
            "created_at",
        ),
    )

    audit_id: Mapped[str] = mapped_column(
        String(AUDIT_ID_LENGTH),
        primary_key=True,
    )
    snapshot_id: Mapped[str] = mapped_column(
        String(AUDIT_ID_LENGTH),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AuditContractRecord(Base):
    __tablename__ = "audit_contracts"
    __table_args__ = (
        CheckConstraint(
            "schema_version IN ('riftx.audit-contract/v1', 'riftx.audit-contract/v2')",
            name="ck_audit_contracts_schema_version",
        ),
        CheckConstraint(
            "length(canonical_contract_json) BETWEEN 2 AND 262144",
            name="ck_audit_contracts_canonical_size",
        ),
        CheckConstraint(
            _lower_hex_digest_check("contract_digest"),
            name="ck_audit_contracts_contract_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("source_target_digest"),
            name="ck_audit_contracts_source_target_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("source_ingest_backend_digest"),
            name="ck_audit_contracts_source_backend_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("source_prepare_proof_digest"),
            name="ck_audit_contracts_source_proof_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("snapshot_hydration_policy_digest"),
            name="ck_audit_contracts_hydration_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("preflight_plan_digest"),
            name="ck_audit_contracts_preflight_plan_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("security_context_bundle_digest"),
            name="ck_audit_contracts_security_context_digest",
        ),
        CheckConstraint(
            "(schema_version = 'riftx.audit-contract/v1' "
            "AND selected_node_id IS NOT NULL AND required_backend_id IS NOT NULL "
            "AND snapshot_hydration_policy_digest IS NOT NULL "
            "AND preflight_plan_id IS NULL AND preflight_plan_digest IS NULL "
            "AND security_context_bundle_id IS NULL "
            "AND security_context_bundle_digest IS NULL) OR "
            "(schema_version = 'riftx.audit-contract/v2' "
            "AND selected_node_id IS NULL AND required_backend_id IS NULL "
            "AND snapshot_hydration_policy_digest IS NULL "
            "AND preflight_plan_id IS NOT NULL AND preflight_plan_digest IS NOT NULL "
            "AND security_context_bundle_id = 'riftx.audit-empty-security-context/v1' "
            "AND security_context_bundle_digest IS NOT NULL)",
            name="ck_audit_contracts_version_shape",
        ),
        CheckConstraint(
            "sealed_at IS NULL OR sealed_at >= created_at",
            name="ck_audit_contracts_timestamp_order",
        ),
        CheckConstraint(
            "state_version >= 1",
            name="ck_audit_contracts_state_version",
        ),
        UniqueConstraint("audit_id", name="uq_audit_contracts_audit"),
        UniqueConstraint(
            "contract_id",
            "audit_id",
            "contract_digest",
            name="uq_audit_contracts_binding",
        ),
        Index("ix_audit_contracts_digest", "contract_digest"),
    )

    contract_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), primary_key=True)
    audit_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_contract_json: Mapped[str] = mapped_column(Text, nullable=False)
    contract_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_node_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    source_ingest_backend_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_prepare_proof_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_node_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    required_backend_id: Mapped[str | None] = mapped_column(String(AUDIT_TOKEN_LENGTH))
    snapshot_hydration_policy_digest: Mapped[str | None] = mapped_column(String(64))
    preflight_plan_id: Mapped[str | None] = mapped_column(String(AUDIT_ID_LENGTH))
    preflight_plan_digest: Mapped[str | None] = mapped_column(String(64))
    security_context_bundle_id: Mapped[str | None] = mapped_column(String(AUDIT_ID_LENGTH))
    security_context_bundle_digest: Mapped[str | None] = mapped_column(String(64))
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    sealed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditScanRecord(Base):
    __tablename__ = "audit_scans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "engagement_id", "run_kind", "selected_node_id"],
            ["runs.id", "runs.engagement_id", "runs.kind", "runs.node_id"],
            name="fk_audit_scans_run_owner_kind_node",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "run_terminal_status"],
            ["runs.id", "runs.status"],
            name="fk_audit_scans_run_terminal_status",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "engagement_id"],
            ["audit_projects.id", "audit_projects.engagement_id"],
            name="fk_audit_scans_project_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["contract_id", "id", "contract_digest"],
            [
                "audit_contracts.contract_id",
                "audit_contracts.audit_id",
                "audit_contracts.contract_digest",
            ],
            name="fk_audit_scans_contract_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "project_id"],
            ["source_snapshots.id", "source_snapshots.project_id"],
            name="fk_audit_scans_snapshot_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["base_snapshot_id", "project_id"],
            ["source_snapshots.id", "source_snapshots.project_id"],
            name="fk_audit_scans_base_snapshot_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["baseline_audit_id", "project_id"],
            ["audit_scans.id", "audit_scans.project_id"],
            name="fk_audit_scans_baseline_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["parent_audit_id", "project_id"],
            ["audit_scans.id", "audit_scans.project_id"],
            name="fk_audit_scans_parent_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "run_kind = 'code_audit'",
            name="ck_audit_scans_run_kind",
        ),
        CheckConstraint(
            "purpose IN ('primary', 'validation_followup', 'retest')",
            name="ck_audit_scans_purpose",
        ),
        CheckConstraint(
            "mode IN ('standard', 'deep', 'diff')",
            name="ck_audit_scans_mode",
        ),
        CheckConstraint(
            "analysis_profile IN ('deterministic', 'hybrid')",
            name="ck_audit_scans_analysis_profile",
        ),
        CheckConstraint(
            "lifecycle_status IN ('draft', 'queued', 'preflighting', 'snapshotting', "
            "'running', 'waiting_approval', 'pausing', 'paused', 'finalizing', "
            "'cancelling', 'failing', 'cleaning', 'sealing_core', 'reporting', "
            "'packaging', 'completed', 'completed_partial', 'failed', 'cancelled')",
            name="ck_audit_scans_lifecycle_status",
        ),
        CheckConstraint(
            "current_phase IN ('authorize_and_freeze', 'map_scope', "
            "'deterministic_probe', 'threat_model', 'agent_hunt', 'reconcile', "
            "'prove', 'compose_risk', 'compare_baseline', 'validate_closure', "
            "'cleanup', 'seal_core', 'generate_reports', 'package_and_publish')",
            name="ck_audit_scans_current_phase",
        ),
        CheckConstraint(
            "terminal_outcome IS NULL OR terminal_outcome IN "
            "('complete', 'partial', 'failed', 'cancelled')",
            name="ck_audit_scans_terminal_outcome",
        ),
        CheckConstraint(
            "closure_status IS NULL OR closure_status IN "
            "('complete_under_declared_scope', 'complete_with_policy_exclusions', "
            "'partial_capability', 'partial_budget', 'failed', 'cancelled')",
            name="ck_audit_scans_closure_status",
        ),
        CheckConstraint(
            "publication_status IN ('not_started', 'sealing_core', 'report_pending', "
            "'reporting', 'packaging', 'published', 'seal_failed', 'report_failed', "
            "'package_failed')",
            name="ck_audit_scans_publication_status",
        ),
        CheckConstraint(
            "run_terminal_status IS NULL OR run_terminal_status IN "
            "('completed', 'failed', 'cancelled')",
            name="ck_audit_scans_run_terminal_status",
        ),
        CheckConstraint(
            "mode <> 'deep' OR analysis_profile = 'hybrid'",
            name="ck_audit_scans_deep_profile",
        ),
        CheckConstraint(
            "(purpose = 'primary' AND parent_audit_id IS NULL) OR "
            "(purpose <> 'primary' AND parent_audit_id IS NOT NULL "
            "AND parent_audit_id <> id)",
            name="ck_audit_scans_parent_purpose",
        ),
        CheckConstraint(
            "baseline_audit_id IS NULL OR baseline_audit_id <> id",
            name="ck_audit_scans_distinct_baseline",
        ),
        CheckConstraint(
            "(mode = 'diff' AND ((snapshot_id IS NULL AND base_snapshot_id IS NULL) OR "
            "(snapshot_id IS NOT NULL AND base_snapshot_id IS NOT NULL "
            "AND snapshot_id <> base_snapshot_id))) OR "
            "(mode <> 'diff' AND base_snapshot_id IS NULL)",
            name="ck_audit_scans_snapshot_mode",
        ),
        CheckConstraint(
            "terminal_outcome NOT IN ('complete', 'partial') OR snapshot_id IS NOT NULL",
            name="ck_audit_scans_outcome_snapshot",
        ),
        CheckConstraint(
            "(cleanup_proof_digest IS NULL AND run_terminal_status IS NULL) OR "
            "(cleanup_proof_digest IS NOT NULL AND run_terminal_status IS NOT NULL)",
            name="ck_audit_scans_cleanup_pair",
        ),
        CheckConstraint(
            "cleanup_proof_digest IS NULL OR "
            "(terminal_outcome IN ('complete', 'partial') "
            "AND run_terminal_status = 'completed') OR "
            "(terminal_outcome = 'failed' AND run_terminal_status = 'failed') OR "
            "(terminal_outcome = 'cancelled' AND run_terminal_status = 'cancelled')",
            name="ck_audit_scans_cleanup_outcome",
        ),
        CheckConstraint(
            "closure_status IS NULL OR cleanup_proof_digest IS NOT NULL",
            name="ck_audit_scans_closure_cleanup",
        ),
        CheckConstraint(
            "(core_seal_root IS NULL AND sealed_at IS NULL) OR "
            "(core_seal_root IS NOT NULL AND sealed_at IS NOT NULL)",
            name="ck_audit_scans_core_seal_pair",
        ),
        CheckConstraint(
            "(publication_status IN ('not_started', 'sealing_core', 'seal_failed') "
            "AND core_seal_root IS NULL) OR "
            "(publication_status NOT IN ('not_started', 'sealing_core', 'seal_failed') "
            "AND core_seal_root IS NOT NULL)",
            name="ck_audit_scans_publication_core",
        ),
        CheckConstraint(
            "(initial_distribution_revision_id IS NULL "
            "AND latest_distribution_revision_id IS NULL "
            "AND publication_finished_at IS NULL) OR "
            "(initial_distribution_revision_id IS NOT NULL "
            "AND latest_distribution_revision_id IS NOT NULL "
            "AND publication_finished_at IS NOT NULL)",
            name="ck_audit_scans_distribution_pair",
        ),
        CheckConstraint(
            "(publication_status = 'published' "
            "AND initial_distribution_revision_id IS NOT NULL) OR "
            "(publication_status <> 'published' "
            "AND initial_distribution_revision_id IS NULL)",
            name="ck_audit_scans_published_revision",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("cleanup_proof_digest"),
            name="ck_audit_scans_cleanup_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("core_seal_root"),
            name="ck_audit_scans_core_seal_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("policy_digest"),
            name="ck_audit_scans_policy_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("budget_digest"),
            name="ck_audit_scans_budget_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("config_digest"),
            name="ck_audit_scans_config_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("contract_digest"),
            name="ck_audit_scans_contract_digest",
        ),
        CheckConstraint("state_version >= 1", name="ck_audit_scans_state_version"),
        CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name="ck_audit_scans_started_order",
        ),
        CheckConstraint(
            "analysis_finished_at IS NULL OR analysis_finished_at >= created_at",
            name="ck_audit_scans_analysis_order",
        ),
        CheckConstraint(
            "analysis_finished_at IS NULL OR started_at IS NULL "
            "OR analysis_finished_at >= started_at",
            name="ck_audit_scans_analysis_started_order",
        ),
        CheckConstraint(
            "sealed_at IS NULL OR analysis_finished_at IS NULL "
            "OR sealed_at >= analysis_finished_at",
            name="ck_audit_scans_sealed_order",
        ),
        CheckConstraint(
            "publication_finished_at IS NULL OR sealed_at IS NULL "
            "OR publication_finished_at >= sealed_at",
            name="ck_audit_scans_publication_order",
        ),
        UniqueConstraint("run_id", name="uq_audit_scans_run"),
        UniqueConstraint(
            "temporal_workflow_id",
            name="uq_audit_scans_temporal_workflow",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            name="uq_audit_scans_id_project",
        ),
        UniqueConstraint(
            "id",
            "run_id",
            "contract_digest",
            "temporal_workflow_id",
            name="uq_audit_scans_start_binding",
        ),
        Index(
            "ix_audit_scans_project_lifecycle_created_id",
            "project_id",
            "lifecycle_status",
            "created_at",
            "id",
        ),
        Index(
            "ix_audit_scans_lifecycle_phase_created_id",
            "lifecycle_status",
            "current_phase",
            "created_at",
            "id",
        ),
        Index(
            "ix_audit_scans_publication_created_id",
            "publication_status",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    engagement_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_kind: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    project_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    contract_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(String(AUDIT_ID_LENGTH))
    base_snapshot_id: Mapped[str | None] = mapped_column(String(AUDIT_ID_LENGTH))
    baseline_audit_id: Mapped[str | None] = mapped_column(String(AUDIT_ID_LENGTH))
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_audit_id: Mapped[str | None] = mapped_column(String(AUDIT_ID_LENGTH))
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    analysis_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_phase: Mapped[str] = mapped_column(String(32), nullable=False)
    terminal_outcome: Mapped[str | None] = mapped_column(String(32))
    cleanup_proof_digest: Mapped[str | None] = mapped_column(String(64))
    run_terminal_status: Mapped[str | None] = mapped_column(String(32))
    closure_status: Mapped[str | None] = mapped_column(String(64))
    publication_status: Mapped[str] = mapped_column(String(32), nullable=False)
    core_seal_root: Mapped[str | None] = mapped_column(String(64))
    initial_distribution_revision_id: Mapped[str | None] = mapped_column(String(AUDIT_ID_LENGTH))
    latest_distribution_revision_id: Mapped[str | None] = mapped_column(String(AUDIT_ID_LENGTH))
    model_profile: Mapped[str | None] = mapped_column(String(255))
    selected_node_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    required_backend_id: Mapped[str | None] = mapped_column(String(AUDIT_TOKEN_LENGTH))
    policy_digest: Mapped[str | None] = mapped_column(String(64))
    budget_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    config_digest: Mapped[str | None] = mapped_column(String(64))
    contract_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    temporal_workflow_id: Mapped[str] = mapped_column(String(AUDIT_TOKEN_LENGTH), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    analysis_finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    publication_finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sealed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditClientRequestRecord(Base):
    __tablename__ = "audit_client_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["audit_id", "run_id", "contract_digest", "temporal_workflow_id"],
            [
                "audit_scans.id",
                "audit_scans.run_id",
                "audit_scans.contract_digest",
                "audit_scans.temporal_workflow_id",
            ],
            name="fk_audit_client_requests_scan_start_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["audit_id", "project_id"],
            ["audit_scans.id", "audit_scans.project_id"],
            name="fk_audit_client_requests_scan_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "engagement_id"],
            ["audit_projects.id", "audit_projects.engagement_id"],
            name="fk_audit_client_requests_project_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["contract_id", "audit_id", "contract_digest"],
            [
                "audit_contracts.contract_id",
                "audit_contracts.audit_id",
                "audit_contracts.contract_digest",
            ],
            name="fk_audit_client_requests_contract_binding",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            _canonical_uuid_check("client_request_id"),
            name="ck_audit_client_requests_canonical_id",
        ),
        CheckConstraint(
            "operation = 'create_draft'",
            name="ck_audit_client_requests_operation",
        ),
        CheckConstraint(
            "request_schema_version IN ('riftx.audit-create-draft-request/v1', "
            "'riftx.audit-create-draft-request/v2')",
            name="ck_audit_client_requests_schema",
        ),
        CheckConstraint(
            _lower_hex_digest_check("request_digest"),
            name="ck_audit_client_requests_request_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("contract_digest"),
            name="ck_audit_client_requests_contract_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("preflight_plan_digest"),
            name="ck_audit_client_requests_preflight_plan_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("security_context_digest"),
            name="ck_audit_client_requests_security_context_digest",
        ),
        CheckConstraint(
            "(request_schema_version = 'riftx.audit-create-draft-request/v1' "
            "AND preflight_plan_id IS NULL AND preflight_plan_digest IS NULL "
            "AND security_context_id IS NULL AND security_context_digest IS NULL "
            "AND contract_stage IS NULL) OR "
            "(request_schema_version = 'riftx.audit-create-draft-request/v2' "
            "AND preflight_plan_id IS NOT NULL AND preflight_plan_digest IS NOT NULL "
            "AND security_context_id = 'riftx.audit-empty-security-context/v1' "
            "AND security_context_digest IS NOT NULL "
            "AND contract_stage = 'preflight_bound_draft')",
            name="ck_audit_client_requests_version_shape",
        ),
        UniqueConstraint("audit_id", name="uq_audit_client_requests_audit"),
        UniqueConstraint("run_id", name="uq_audit_client_requests_run"),
        UniqueConstraint("contract_id", name="uq_audit_client_requests_contract"),
        Index(
            "ix_audit_client_requests_project_created_id",
            "project_id",
            "created_at",
            "client_request_id",
        ),
    )

    client_request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    request_schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    preflight_plan_id: Mapped[str | None] = mapped_column(String(AUDIT_ID_LENGTH))
    preflight_plan_digest: Mapped[str | None] = mapped_column(String(64))
    security_context_id: Mapped[str | None] = mapped_column(String(AUDIT_ID_LENGTH))
    security_context_digest: Mapped[str | None] = mapped_column(String(64))
    contract_stage: Mapped[str | None] = mapped_column(String(64))
    audit_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    project_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    engagement_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    contract_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    contract_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    temporal_workflow_id: Mapped[str] = mapped_column(String(AUDIT_TOKEN_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AuditSecurityContextBindingRecord(Base):
    __tablename__ = "audit_security_context_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["audit_id"],
            ["audit_scans.id"],
            name="fk_audit_security_context_bindings_audit",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "preflight_plan_id",
                "preflight_plan_digest",
                "operator_principal_id",
                "authorization_scope_digest",
                "security_context_bundle_id",
                "security_context_bundle_digest",
                "audit_id",
            ],
            [
                "audit_preflight_plans.id",
                "audit_preflight_plans.plan_digest",
                "audit_preflight_plans.operator_principal_id",
                "audit_preflight_plans.authorization_scope_digest",
                "audit_preflight_plans.security_context_id",
                "audit_preflight_plans.security_context_digest",
                "audit_preflight_plans.reserved_audit_id",
            ],
            name="fk_audit_security_context_bindings_plan",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "schema_version = 'riftx.audit-security-context-binding/v2'",
            name="ck_audit_security_context_bindings_schema",
        ),
        CheckConstraint(
            "security_context_bundle_id = 'riftx.audit-empty-security-context/v1'",
            name="ck_audit_security_context_bindings_empty_context",
        ),
        CheckConstraint(
            _lower_hex_digest_check("preflight_plan_digest"),
            name="ck_audit_security_context_bindings_plan_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("authorization_scope_digest"),
            name="ck_audit_security_context_bindings_authorization_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("security_context_bundle_digest"),
            name="ck_audit_security_context_bindings_context_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("binding_digest"),
            name="ck_audit_security_context_bindings_binding_digest",
        ),
        UniqueConstraint(
            "preflight_plan_id",
            name="uq_audit_security_context_bindings_plan",
        ),
    )

    audit_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    preflight_plan_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    preflight_plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_principal_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    authorization_scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    security_context_bundle_id: Mapped[str] = mapped_column(
        String(AUDIT_ID_LENGTH), nullable=False
    )
    security_context_bundle_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AuditStartIntentRecord(Base):
    __tablename__ = "audit_start_intents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["audit_id", "run_id", "contract_digest", "workflow_id"],
            [
                "audit_scans.id",
                "audit_scans.run_id",
                "audit_scans.contract_digest",
                "audit_scans.temporal_workflow_id",
            ],
            name="fk_audit_start_intents_scan_binding",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'started', 'retryable', "
            "'outcome_unknown', 'cancelled')",
            name="ck_audit_start_intents_status",
        ),
        CheckConstraint(
            "attempt >= 0 AND state_version >= 1",
            name="ck_audit_start_intents_counters",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_audit_start_intents_lease_pair",
        ),
        CheckConstraint(
            "(status = 'claimed' AND lease_owner IS NOT NULL) OR "
            "(status <> 'claimed' AND lease_owner IS NULL)",
            name="ck_audit_start_intents_claim_lease",
        ),
        CheckConstraint(
            "(status = 'retryable' AND next_attempt_at IS NOT NULL) OR "
            "status IN ('pending', 'outcome_unknown') OR "
            "(status NOT IN ('retryable', 'pending', 'outcome_unknown') "
            "AND next_attempt_at IS NULL)",
            name="ck_audit_start_intents_retry_time",
        ),
        CheckConstraint(
            "(status = 'started' AND started_at IS NOT NULL) OR "
            "(status <> 'started' AND started_at IS NULL)",
            name="ck_audit_start_intents_started_time",
        ),
        CheckConstraint(
            _lower_hex_digest_check("contract_digest"),
            name="ck_audit_start_intents_contract_digest",
        ),
        CheckConstraint(
            "status NOT IN ('claimed', 'started', 'retryable', 'outcome_unknown') OR attempt >= 1",
            name="ck_audit_start_intents_attempted_status",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="ck_audit_start_intents_timestamp_order",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > updated_at",
            name="ck_audit_start_intents_lease_order",
        ),
        CheckConstraint(
            "next_attempt_at IS NULL OR next_attempt_at >= updated_at",
            name="ck_audit_start_intents_retry_order",
        ),
        CheckConstraint(
            "started_at IS NULL OR (started_at >= created_at AND started_at <= updated_at)",
            name="ck_audit_start_intents_started_order",
        ),
        UniqueConstraint("audit_id", name="uq_audit_start_intents_audit"),
        UniqueConstraint(
            "start_request_id",
            name="uq_audit_start_intents_start_request",
        ),
        UniqueConstraint("workflow_id", name="uq_audit_start_intents_workflow"),
        Index(
            "ix_audit_start_intents_dispatch",
            "status",
            "next_attempt_at",
            "lease_expires_at",
            "created_at",
            "intent_id",
        ),
    )

    intent_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), primary_key=True)
    audit_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    start_request_id: Mapped[str] = mapped_column(String(AUDIT_TOKEN_LENGTH), nullable=False)
    contract_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(AUDIT_TOKEN_LENGTH), nullable=False)
    task_queue: Mapped[str] = mapped_column(String(AUDIT_TOKEN_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(AUDIT_TOKEN_LENGTH))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error_code: Mapped[str | None] = mapped_column(String(AUDIT_TOKEN_LENGTH))
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditPhaseRunRecord(Base):
    __tablename__ = "audit_phase_runs"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('authorize_and_freeze', 'map_scope', 'deterministic_probe', "
            "'threat_model', 'agent_hunt', 'reconcile', 'prove', 'compose_risk', "
            "'compare_baseline', 'validate_closure', 'cleanup', 'seal_core', "
            "'generate_reports', 'package_and_publish')",
            name="ck_audit_phase_runs_phase",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'deferred', "
            "'cancelled', 'not_applicable')",
            name="ck_audit_phase_runs_status",
        ),
        CheckConstraint(
            "attempt >= 1 AND state_version >= 1",
            name="ck_audit_phase_runs_counters",
        ),
        CheckConstraint(
            _lower_hex_digest_check("input_digest"),
            name="ck_audit_phase_runs_input_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("config_digest"),
            name="ck_audit_phase_runs_config_digest",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="ck_audit_phase_runs_timestamp_order",
        ),
        CheckConstraint(
            "started_at IS NULL OR (started_at >= created_at AND started_at <= updated_at)",
            name="ck_audit_phase_runs_started_order",
        ),
        CheckConstraint(
            "finished_at IS NULL OR (finished_at >= created_at AND finished_at <= updated_at)",
            name="ck_audit_phase_runs_finished_order",
        ),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="ck_audit_phase_runs_runtime_order",
        ),
        CheckConstraint(
            "(error_code IS NULL AND error_summary IS NULL) OR "
            "(error_code IS NOT NULL AND error_summary IS NOT NULL)",
            name="ck_audit_phase_runs_error_pair",
        ),
        CheckConstraint(
            "error_summary IS NULL OR length(error_summary) BETWEEN 1 AND 4096",
            name="ck_audit_phase_runs_error_summary_size",
        ),
        CheckConstraint(
            "(status = 'queued' AND started_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status NOT IN ('queued', 'running') AND finished_at IS NOT NULL)",
            name="ck_audit_phase_runs_status_time",
        ),
        CheckConstraint(
            "status NOT IN ('queued', 'running') OR "
            "(json_array_length(output_artifact_ids_json) = 0 AND "
            "json_array_length(summary_counts_json) = 0)",
            name="ck_audit_phase_runs_active_outputs",
        ),
        CheckConstraint(
            "status <> 'completed' OR started_at IS NOT NULL",
            name="ck_audit_phase_runs_completed_start",
        ),
        CheckConstraint(
            "(status IN ('failed', 'deferred', 'not_applicable') "
            "AND error_code IS NOT NULL) OR "
            "(status NOT IN ('failed', 'deferred', 'not_applicable'))",
            name="ck_audit_phase_runs_error_status",
        ),
        CheckConstraint(
            "status NOT IN ('queued', 'running', 'completed') OR error_code IS NULL",
            name="ck_audit_phase_runs_nonerror_status",
        ),
        UniqueConstraint(
            "audit_id",
            "phase",
            "idempotency_key",
            name="uq_audit_phase_runs_idempotency",
        ),
        Index(
            "ix_audit_phase_runs_audit_phase_status_created_id",
            "audit_id",
            "phase",
            "status",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), primary_key=True)
    audit_id: Mapped[str] = mapped_column(
        ForeignKey("audit_scans.id", ondelete="RESTRICT"), nullable=False
    )
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(AUDIT_TOKEN_LENGTH), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    output_artifact_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    summary_counts_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    error_code: Mapped[str | None] = mapped_column(String(AUDIT_TOKEN_LENGTH))
    error_summary: Mapped[str | None] = mapped_column(Text)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditScopeUnitRecord(Base):
    __tablename__ = "audit_scope_units"
    __table_args__ = (
        ForeignKeyConstraint(
            ["audit_id", "project_id"],
            ["audit_scans.id", "audit_scans.project_id"],
            name="fk_audit_scope_units_audit_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "project_id"],
            ["source_snapshots.id", "source_snapshots.project_id"],
            name="fk_audit_scope_units_snapshot_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "kind IN ('file', 'symbol', 'diff_hunk', 'dependency', 'endpoint', "
            "'configuration', 'trust_boundary')",
            name="ck_audit_scope_units_kind",
        ),
        CheckConstraint(
            "risk_tier IN ('low', 'medium', 'high', 'critical')",
            name="ck_audit_scope_units_risk_tier",
        ),
        CheckConstraint(
            "status IN ('included', 'analyzed', 'excluded', 'deferred', 'failed')",
            name="ck_audit_scope_units_status",
        ),
        CheckConstraint(
            _lower_hex_digest_check("stable_key"),
            name="ck_audit_scope_units_stable_key",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("blob_digest"),
            name="ck_audit_scope_units_blob_digest",
        ),
        CheckConstraint(
            "receipt_count >= 0 AND state_version >= 1",
            name="ck_audit_scope_units_counters",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="ck_audit_scope_units_timestamp_order",
        ),
        CheckConstraint(
            "(closure_code IS NULL AND closure_reason IS NULL) OR "
            "(closure_code IS NOT NULL AND closure_reason IS NOT NULL)",
            name="ck_audit_scope_units_closure_pair",
        ),
        CheckConstraint(
            "relative_path IS NULL OR length(relative_path) BETWEEN 1 AND 4096",
            name="ck_audit_scope_units_relative_path_size",
        ),
        CheckConstraint(
            "closure_reason IS NULL OR length(closure_reason) BETWEEN 1 AND 4096",
            name="ck_audit_scope_units_closure_reason_size",
        ),
        CheckConstraint(
            "(status = 'included' AND closure_code IS NULL) OR "
            "(status <> 'included' AND closure_code IS NOT NULL)",
            name="ck_audit_scope_units_status_closure",
        ),
        CheckConstraint(
            "kind NOT IN ('file', 'symbol', 'diff_hunk', 'configuration') "
            "OR relative_path IS NOT NULL",
            name="ck_audit_scope_units_path_kind",
        ),
        CheckConstraint(
            "kind <> 'symbol' OR symbol_anchor IS NOT NULL",
            name="ck_audit_scope_units_symbol_anchor",
        ),
        UniqueConstraint(
            "id",
            "audit_id",
            name="uq_audit_scope_units_id_audit",
        ),
        UniqueConstraint(
            "audit_id",
            "snapshot_id",
            "kind",
            "stable_key",
            name="uq_audit_scope_units_stable_key",
        ),
        Index(
            "ix_audit_scope_units_audit_kind_status_risk_id",
            "audit_id",
            "kind",
            "status",
            "risk_tier",
            "id",
        ),
        Index(
            "ix_audit_scope_units_audit_snapshot_path",
            "audit_id",
            "snapshot_id",
            "relative_path",
        ),
    )

    id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), primary_key=True)
    audit_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    project_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    stable_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    relative_path: Mapped[str | None] = mapped_column(Text)
    blob_digest: Mapped[str | None] = mapped_column(String(64))
    symbol_anchor: Mapped[str | None] = mapped_column(String(2048))
    risk_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    required_analyses_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    closure_code: Mapped[str | None] = mapped_column(String(AUDIT_TOKEN_LENGTH))
    closure_reason: Mapped[str | None] = mapped_column(Text)
    receipt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AuditWorkItemRecord(Base):
    __tablename__ = "audit_work_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["primary_scope_unit_id", "audit_id"],
            ["audit_scope_units.id", "audit_scope_units.audit_id"],
            name="fk_audit_work_items_primary_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "phase IN ('authorize_and_freeze', 'map_scope', 'deterministic_probe', "
            "'threat_model', 'agent_hunt', 'reconcile', 'prove', 'compose_risk', "
            "'compare_baseline', 'validate_closure', 'cleanup', 'seal_core', "
            "'generate_reports', 'package_and_publish')",
            name="ck_audit_work_items_phase",
        ),
        CheckConstraint(
            "risk_tier IN ('low', 'medium', 'high', 'critical')",
            name="ck_audit_work_items_risk_tier",
        ),
        CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'completed', 'failed', "
            "'deferred', 'cancelled', 'outcome_unknown')",
            name="ck_audit_work_items_status",
        ),
        CheckConstraint(
            "epoch >= 0 AND attempt >= 0 AND state_version >= 1",
            name="ck_audit_work_items_counters",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_audit_work_items_lease_pair",
        ),
        CheckConstraint(
            "(status IN ('leased', 'running') AND lease_owner IS NOT NULL) OR "
            "(status NOT IN ('leased', 'running') AND lease_owner IS NULL)",
            name="ck_audit_work_items_active_lease",
        ),
        CheckConstraint(
            _lower_hex_digest_check("input_digest"),
            name="ck_audit_work_items_input_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("required_coverage_plan_digest"),
            name="ck_audit_work_items_plan_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("stable_key"),
            name="ck_audit_work_items_stable_key",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="ck_audit_work_items_timestamp_order",
        ),
        CheckConstraint(
            "status NOT IN ('leased', 'running', 'completed', 'failed', "
            "'outcome_unknown') OR attempt >= 1",
            name="ck_audit_work_items_attempted_status",
        ),
        CheckConstraint(
            "(status = 'completed' AND receipt_id IS NOT NULL) OR "
            "(status <> 'completed' AND receipt_id IS NULL)",
            name="ck_audit_work_items_receipt_status",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > updated_at",
            name="ck_audit_work_items_lease_order",
        ),
        UniqueConstraint(
            "audit_id",
            "phase",
            "epoch",
            "stable_key",
            name="uq_audit_work_items_stable_key",
        ),
        Index(
            "ix_audit_work_items_audit_phase_status_lease_epoch_id",
            "audit_id",
            "phase",
            "status",
            "lease_expires_at",
            "epoch",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), primary_key=True)
    audit_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    primary_scope_unit_id: Mapped[str] = mapped_column(String(AUDIT_ID_LENGTH), nullable=False)
    strategy: Mapped[str] = mapped_column(String(AUDIT_TOKEN_LENGTH), nullable=False)
    stable_key: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(AUDIT_TOKEN_LENGTH))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    required_coverage_plan_artifact_id: Mapped[str] = mapped_column(
        String(AUDIT_ID_LENGTH), nullable=False
    )
    required_coverage_plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_id: Mapped[str | None] = mapped_column(String(AUDIT_ID_LENGTH))
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AgentMessageRecord(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_agent_messages_session_sequence"),
        Index("ix_agent_messages_session_created", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    parent_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_messages.id", ondelete="SET NULL"), index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content: Mapped[str | None] = mapped_column(Text)
    structured_content_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    tool_call_id: Mapped[str | None] = mapped_column(String(255), index=True)
    execution_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    artifact_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    compacted_by_checkpoint_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    token_count: Mapped[int | None] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AgentCheckpointRecord(Base):
    __tablename__ = "agent_checkpoints"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sdk_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ToolCallRecord(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (UniqueConstraint("run_id", "sdk_call_id", name="uq_tool_calls_run_sdk_call"),)

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    sdk_call_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_step_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    tool_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    skill_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    approval_status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ExecutionRecord(Base):
    __tablename__ = "executions"
    __table_args__ = (
        CheckConstraint(
            "(audit_id IS NULL AND plan_digest IS NULL) OR "
            "(audit_id IS NOT NULL AND plan_digest IS NOT NULL)",
            name="ck_executions_audit_plan_pair",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("plan_digest"),
            name="ck_executions_plan_digest",
        ),
        CheckConstraint(
            "(runner_command_id IS NULL AND runner_effect_binding_id IS NULL "
            "AND runner_binding_digest IS NULL AND runner_envelope_digest IS NULL) OR "
            "(runner_command_id IS NOT NULL AND runner_effect_binding_id IS NOT NULL "
            "AND runner_binding_digest IS NOT NULL AND runner_envelope_digest IS NOT NULL)",
            name="ck_executions_runner_binding_shape",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("runner_binding_digest"),
            name="ck_executions_runner_binding_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("runner_envelope_digest"),
            name="ck_executions_runner_envelope_digest",
        ),
        UniqueConstraint("runner_command_id", name="uq_executions_runner_command"),
        Index(
            "ix_executions_run_tool_created_id",
            "run_id",
            "tool_call_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    execution_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    launch_fingerprint: Mapped[str | None] = mapped_column(String(80))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    audit_id: Mapped[str | None] = mapped_column(
        String(AUDIT_ID_LENGTH),
        ForeignKey("audit_scans.id", ondelete="RESTRICT"),
        index=True,
    )
    plan_digest: Mapped[str | None] = mapped_column(String(64))
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="SET NULL"), index=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(TOOL_CALL_INTENT_ID_LENGTH), index=True)
    attempt_group: Mapped[str | None] = mapped_column(String(64), index=True)
    node_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    owner_runner_instance_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    owner_runner_epoch: Mapped[int | None] = mapped_column(BigInteger)
    runner_command_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("runner_commands.id", ondelete="RESTRICT"),
        index=True,
    )
    runner_effect_binding_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("runner_effect_bindings.id", ondelete="RESTRICT"),
        index=True,
    )
    runner_binding_digest: Mapped[str | None] = mapped_column(String(64))
    runner_envelope_digest: Mapped[str | None] = mapped_column(String(64))
    executor_type: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    argv_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    command_text: Mapped[str | None] = mapped_column(Text)
    tool_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    tool_version: Mapped[str | None] = mapped_column(Text)
    executable_path: Mapped[str | None] = mapped_column(Text)
    cwd: Mapped[str] = mapped_column(Text, nullable=False)
    env_diff_json: Mapped[dict[str, str | None]] = mapped_column(JSON, nullable=False, default=dict)
    platform_system: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    platform_release: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    platform_architecture: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    pid: Mapped[int | None] = mapped_column(Integer)
    process_group_id: Mapped[int | None] = mapped_column(Integer)
    containment_id: Mapped[str | None] = mapped_column(String(255))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout_path: Mapped[str] = mapped_column(Text, nullable=False)
    stderr_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    process_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    physical_stop_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class TerminalSessionRecord(Base):
    __tablename__ = "terminal_sessions"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    runner_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, default="")
    shell: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cwd: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    owner: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    cols: Mapped[int] = mapped_column(Integer, nullable=False)
    rows: Mapped[int] = mapped_column(Integer, nullable=False)
    output_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    takeover_cursor: Mapped[int | None] = mapped_column(Integer)
    takeover_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    transcript_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ApprovalRecord(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_call_id: Mapped[str] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    command_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    cwd: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    env_diff_json: Mapped[dict[str, str | None]] = mapped_column(JSON, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decision: Mapped[str | None] = mapped_column(String(STATUS_LENGTH))
    decision_feedback: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ApprovalGrantRecord(Base):
    __tablename__ = "approval_grants"
    __table_args__ = (UniqueConstraint("run_id", "tool_id", name="uq_approval_grants_run_tool"),)

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunEventRecord(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        Index("ix_run_events_run_created", "run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            "access_class IN ('public_export', 'audit_internal', "
            "'restricted_sensitive')",
            name="ck_artifacts_access_class",
        ),
        CheckConstraint(
            "content_trust IN ('generated', 'untrusted_source', "
            "'untrusted_tool_output')",
            name="ck_artifacts_content_trust",
        ),
        CheckConstraint(
            "audit_id IS NOT NULL OR access_class = 'public_export'",
            name="ck_artifacts_owner_access",
        ),
        CheckConstraint(
            "storage_key = 'runs/' || run_id || '/artifacts/' || id || '/' || name",
            name="ck_artifacts_canonical_storage_key",
        ),
        CheckConstraint(
            _ArtifactStorageComponentsAreSafe(
                column("run_id"),
                column("id"),
                column("name"),
            ),
            name="ck_artifacts_safe_storage_components",
        ),
        CheckConstraint(
            _ArtifactMimeTypeIsSafe(column("mime_type")),
            name="ck_artifacts_safe_mime_type",
        ),
        CheckConstraint(
            _lower_hex_digest_check("sha256"),
            name="ck_artifacts_sha256",
        ),
        CheckConstraint("size >= 0", name="ck_artifacts_nonnegative_size"),
        Index(
            "ix_artifacts_public_run_created_id",
            "run_id",
            "access_class",
            "created_at",
            "id",
        ),
        Index(
            "ix_artifacts_audit_run_execution_created_id",
            "audit_id",
            "run_id",
            "execution_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "executions.id",
            name="fk_artifacts_execution",
            ondelete="RESTRICT",
        ),
        index=True,
    )
    audit_id: Mapped[str | None] = mapped_column(
        String(AUDIT_ID_LENGTH),
        ForeignKey(
            "audit_scans.id",
            name="fk_artifacts_audit",
            ondelete="RESTRICT",
        ),
    )
    access_class: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=_default_artifact_access_class,
    )
    content_trust: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="untrusted_tool_output",
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=_default_artifact_storage_key,
    )
    ingest_provenance_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=_default_artifact_ingest_provenance,
    )
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class FindingRecord(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    severity: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    affected_assets_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    reproduction_steps_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    impact: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recommendation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ReportRecord(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    format: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    finding_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class NodeRecord(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    architecture: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    labels_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    current_runner_instance_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    current_runner_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunnerCredentialRecord(Base):
    __tablename__ = "runner_credentials"
    __table_args__ = (
        UniqueConstraint(
            "node_id",
            "runner_epoch",
            name="uq_runner_credentials_node_epoch",
        ),
        UniqueConstraint(
            "node_id",
            "token_hash",
            name="uq_runner_credentials_node_token_hash",
        ),
    )

    runner_instance_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    runner_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    protocol_capabilities_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    rotated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RunnerCommandRecord(Base):
    __tablename__ = "runner_commands"
    __table_args__ = (
        CheckConstraint("state_version >= 0", name="ck_runner_commands_state_version"),
        UniqueConstraint(
            "node_id",
            "idempotency_key",
            name="uq_runner_commands_node_idempotency",
        ),
        Index(
            "ix_runner_commands_target_poll",
            "node_id",
            "target_runner_instance_id",
            "target_runner_epoch",
            "status",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    target_runner_instance_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    target_runner_epoch: Mapped[int | None] = mapped_column(BigInteger)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RunnerEffectBindingRecord(Base):
    __tablename__ = "runner_effect_bindings"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.runner-effect-binding/v1'",
            name="ck_runner_effect_bindings_schema",
        ),
        CheckConstraint(
            "run_kind IN ('general', 'code_audit')",
            name="ck_runner_effect_bindings_run_kind",
        ),
        CheckConstraint(
            "origin IN "
            "('application_service', 'temporal_worker', "
            "'control_plane_reconciler', 'worker_reconciler', 'safety_reconciler')",
            name="ck_runner_effect_bindings_origin",
        ),
        CheckConstraint(
            "operation_family IN "
            "('execution', 'terminal', 'browser', 'target_http', 'connector', "
            "'safety_stop')",
            name="ck_runner_effect_bindings_family",
        ),
        CheckConstraint(
            "resource_kind IN "
            "('execution', 'terminal_session', 'browser_session', "
            "'target_http_intent', 'connector_session')",
            name="ck_runner_effect_bindings_resource_kind",
        ),
        CheckConstraint(
            "(run_kind = 'general' AND audit_id IS NULL AND plan_digest IS NULL) OR "
            "(run_kind = 'code_audit' AND audit_id IS NOT NULL AND plan_digest IS NOT NULL)",
            name="ck_runner_effect_bindings_run_owner_shape",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("plan_digest"),
            name="ck_runner_effect_bindings_plan_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("binding_digest"),
            name="ck_runner_effect_bindings_binding_digest",
        ),
        CheckConstraint(
            "resource_kind <> 'execution' OR "
            "(execution_id IS NOT NULL AND resource_id = execution_id)",
            name="ck_runner_effect_bindings_execution_identity",
        ),
        Index(
            "ix_runner_effect_bindings_family_resource",
            "operation_family",
            "resource_kind",
            "resource_id",
        ),
        Index(
            "ix_runner_effect_bindings_run_execution",
            "run_id",
            "execution_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    run_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    node_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("nodes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    target_runner_instance_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("runner_credentials.runner_instance_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    target_runner_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_family: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(128), index=True)
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    audit_id: Mapped[str | None] = mapped_column(
        String(AUDIT_ID_LENGTH),
        ForeignKey("audit_scans.id", ondelete="RESTRICT"),
        index=True,
    )
    plan_digest: Mapped[str | None] = mapped_column(String(64))
    binding_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunnerCommandOwnershipRecord(Base):
    __tablename__ = "runner_command_ownerships"
    __table_args__ = (
        CheckConstraint(
            "verification_state IN ('verified', 'quarantined')",
            name="ck_runner_command_ownerships_state",
        ),
        CheckConstraint(
            "operation_family IS NULL OR operation_family IN "
            "('execution', 'terminal', 'browser', 'target_http', 'connector', "
            "'safety_stop')",
            name="ck_runner_command_ownerships_family",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("payload_digest"),
            name="ck_runner_command_ownerships_payload_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("output_contract_digest"),
            name="ck_runner_command_ownerships_output_contract_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("envelope_digest"),
            name="ck_runner_command_ownerships_envelope_digest",
        ),
        CheckConstraint(
            "(verification_state = 'verified' "
            "AND schema_version = 'riftx.runner-command-ownership/v1' "
            "AND effect_binding_id IS NOT NULL AND operation IS NOT NULL "
            "AND operation_family IS NOT NULL AND payload_digest IS NOT NULL "
            "AND output_contract_json IS NOT NULL AND output_contract_digest IS NOT NULL "
            "AND envelope_digest IS NOT NULL AND quarantine_reason IS NULL "
            "AND quarantined_at IS NULL AND reconciliation_state IS NULL) OR "
            "(verification_state = 'quarantined' AND quarantine_reason IS NOT NULL "
            "AND quarantined_at IS NOT NULL AND reconciliation_state IN "
            "('untouched', 'pending', 'replaced', 'manual'))",
            name="ck_runner_command_ownerships_verification_shape",
        ),
        Index(
            "ix_runner_command_ownerships_state_family",
            "verification_state",
            "operation_family",
        ),
        Index(
            "ix_runner_command_ownerships_reconciliation",
            "reconciliation_state",
            "quarantined_at",
        ),
        UniqueConstraint(
            "effect_binding_id",
            name="uq_runner_command_ownerships_effect_binding",
        ),
    )

    command_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("runner_commands.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    verification_state: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str | None] = mapped_column(String(64))
    effect_binding_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("runner_effect_bindings.id", ondelete="RESTRICT"),
        index=True,
    )
    operation: Mapped[str | None] = mapped_column(String(32))
    operation_family: Mapped[str | None] = mapped_column(String(32))
    payload_digest: Mapped[str | None] = mapped_column(String(64))
    output_contract_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_contract_digest: Mapped[str | None] = mapped_column(String(64))
    envelope_digest: Mapped[str | None] = mapped_column(String(64), index=True)
    quarantine_reason: Mapped[str | None] = mapped_column(String(255))
    quarantined_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reconciliation_state: Mapped[str | None] = mapped_column(String(32), index=True)
    replacement_command_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("runner_commands.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunnerStopReceiptRecord(Base):
    __tablename__ = "runner_stop_receipts"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.runner-stop-receipt/v1'",
            name="ck_runner_stop_receipts_schema",
        ),
        CheckConstraint(_lower_hex_digest_check("envelope_digest"), name="ck_stop_envelope"),
        CheckConstraint(_lower_hex_digest_check("binding_digest"), name="ck_stop_binding"),
        CheckConstraint(_lower_hex_digest_check("ack_digest"), name="ck_stop_ack"),
        UniqueConstraint("command_id", name="uq_runner_stop_receipts_command"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    command_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("runner_commands.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    effect_binding_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("runner_effect_bindings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    envelope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_family: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    execution_id: Mapped[str | None] = mapped_column(String(128), index=True)
    node_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    principal_instance_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    principal_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ack_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    ack_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunnerStopProjectionRecord(Base):
    __tablename__ = "runner_stop_projections"
    __table_args__ = (
        CheckConstraint(
            "projection_state IN ('pending', 'applied', 'manual')",
            name="ck_runner_stop_projections_state",
        ),
        CheckConstraint("state_version >= 0", name="ck_runner_stop_projections_version"),
    )

    receipt_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("runner_stop_receipts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    projection_state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ToolStateRecord(Base):
    __tablename__ = "tool_states"
    __table_args__ = (UniqueConstraint("node_id", "tool_id", name="uq_tool_states_node_tool"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    availability: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    resolved_command: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AgentSessionRecord(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    model_profile: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    latest_checkpoint_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    provider_state_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AgentCycleRecord(Base):
    __tablename__ = "agent_cycles"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_agent_cycles_session_sequence"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    yield_reason: Mapped[str | None] = mapped_column(String(64))
    waiting_object_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    model_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AgentRuntimeStepRecord(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("cycle_id", "sequence", name="uq_agent_steps_cycle_sequence"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("agent_cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    input_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    output_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ProviderStateRecord(Base):
    __tablename__ = "provider_states"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    engine_type: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    previous_response_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class WorkingMemoryRecord(Base):
    __tablename__ = "working_memories"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class TaskGraphRecord(Base):
    __tablename__ = "task_graphs"
    __table_args__ = (CheckConstraint("version >= 1", name="ck_task_graphs_version"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'blocked', 'completed', 'failed', 'cancelled')",
            name="ck_tasks_status",
        ),
        CheckConstraint("sequence >= 1", name="ck_tasks_sequence"),
        CheckConstraint("version >= 1", name="ck_tasks_version"),
        CheckConstraint("parent_task_id IS NULL OR parent_task_id <> id", name="ck_tasks_parent"),
        CheckConstraint(
            "(status = 'blocked' AND blocked_reason IS NOT NULL) OR status <> 'blocked'",
            name="ck_tasks_blocked_reason",
        ),
        CheckConstraint(
            "(status = 'completed' AND completion_summary IS NOT NULL "
            "AND completed_at IS NOT NULL) "
            "OR (status <> 'completed' AND completed_at IS NULL)",
            name="ck_tasks_completion_shape",
        ),
        ForeignKeyConstraint(
            ["run_id", "parent_task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "id", name="uq_tasks_run_id_id"),
        UniqueConstraint("run_id", "sequence", name="uq_tasks_run_sequence"),
        Index("ix_tasks_run_status_sequence", "run_id", "status", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("task_graphs.run_id", ondelete="CASCADE"), nullable=False
    )
    parent_task_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    input_scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    expected_output_schema_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    required_capability_ids_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    workspace_owner: Mapped[str | None] = mapped_column(String(128))
    session_owner_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    stop_condition: Mapped[str | None] = mapped_column(Text)
    completion_summary: Mapped[str | None] = mapped_column(Text)
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    reopen_history_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class TaskDependencyRecord(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (
        CheckConstraint("task_id <> depends_on_task_id", name="ck_task_dependencies_not_self"),
        ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["run_id", "depends_on_task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="CASCADE",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    depends_on_task_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class TaskAttemptRecord(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name="ck_task_attempts_status",
        ),
        CheckConstraint("sequence >= 1", name="ck_task_attempts_sequence"),
        CheckConstraint(
            "retry_of_attempt_id IS NULL OR retry_of_attempt_id <> id",
            name="ck_task_attempts_retry",
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND failure_summary IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND failure_summary IS NOT NULL) OR "
            "(status IN ('succeeded', 'cancelled') AND finished_at IS NOT NULL "
            "AND failure_summary IS NULL)",
            name="ck_task_attempts_lifecycle",
        ),
        ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["run_id", "task_id", "retry_of_attempt_id"],
            ["task_attempts.run_id", "task_attempts.task_id", "task_attempts.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "task_id", "id", name="uq_task_attempts_owner_id"),
        UniqueConstraint("run_id", "task_id", "sequence", name="uq_task_attempts_owner_sequence"),
        Index("ix_task_attempts_run_status", "run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    retry_of_attempt_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class TaskBudgetRecord(Base):
    __tablename__ = "task_budgets"
    __table_args__ = (
        CheckConstraint(
            "max_model_calls IS NOT NULL OR max_tool_calls IS NOT NULL OR "
            "max_tokens IS NOT NULL OR max_duration_seconds IS NOT NULL",
            name="ck_task_budgets_nonempty",
        ),
        CheckConstraint(
            "max_model_calls IS NULL OR max_model_calls >= 1",
            name="ck_task_budgets_model_calls",
        ),
        CheckConstraint(
            "max_tool_calls IS NULL OR max_tool_calls >= 1",
            name="ck_task_budgets_tool_calls",
        ),
        CheckConstraint("max_tokens IS NULL OR max_tokens >= 1", name="ck_task_budgets_tokens"),
        CheckConstraint(
            "max_duration_seconds IS NULL OR max_duration_seconds > 0",
            name="ck_task_budgets_duration",
        ),
        ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="CASCADE",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    max_model_calls: Mapped[int | None] = mapped_column(Integer)
    max_tool_calls: Mapped[int | None] = mapped_column(Integer)
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    max_duration_seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class TaskEvidenceRequirementRecord(Base):
    __tablename__ = "task_evidence_requirements"
    __table_args__ = (
        CheckConstraint("minimum_count >= 1", name="ck_task_evidence_minimum_count"),
        CheckConstraint(
            "success_criterion_index IS NULL OR success_criterion_index >= 0",
            name="ck_task_evidence_success_criterion_index",
        ),
        ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("run_id", "task_id", "id", name="uq_task_evidence_requirements_owner_id"),
        Index("ix_task_evidence_requirements_run_task", "run_id", "task_id"),
        Index(
            "ix_task_evidence_requirements_run_criterion",
            "run_id",
            "success_criterion_index",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    minimum_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    success_criterion_index: Mapped[int | None] = mapped_column(Integer)
    evidence_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class EvidenceRecord(Base):
    __tablename__ = "evidence_ledger"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.evidence/v1'",
            name="ck_evidence_schema_version",
        ),
        CheckConstraint(
            "kind IN ('execution_output', 'artifact_span', 'http_request_response', "
            "'browser_observation', 'code_location', 'code_flow', 'scanner_signal', "
            "'user_decision', 'deterministic_parser_result', 'external_research_source')",
            name="ck_evidence_kind",
        ),
        CheckConstraint(
            "creator_type IN ('agent', 'operator', 'system', 'tool', 'parser', 'scanner')",
            name="ck_evidence_creator_type",
        ),
        CheckConstraint(
            "trust_class IN ('generated', 'user_provided', 'untrusted_source', "
            "'untrusted_tool_output')",
            name="ck_evidence_trust_class",
        ),
        CheckConstraint(
            "redaction_status IN ('not_required', 'redacted', 'restricted', 'metadata_only')",
            name="ck_evidence_redaction_status",
        ),
        CheckConstraint(
            "(redaction_status = 'redacted' AND redaction_policy_ref IS NOT NULL) OR "
            "(redaction_status = 'not_required' AND redaction_policy_ref IS NULL) OR "
            "redaction_status IN ('restricted', 'metadata_only')",
            name="ck_evidence_redaction_shape",
        ),
        CheckConstraint(_lower_hex_digest_check("digest"), name="ck_evidence_digest"),
        CheckConstraint(
            _lower_hex_digest_check("ledger_digest"),
            name="ck_evidence_ledger_digest",
        ),
        ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "id", name="uq_evidence_owner_id"),
        Index("ix_evidence_run_created_id", "run_id", "created_at", "id"),
        Index("ix_evidence_run_task_created", "run_id", "task_id", "created_at"),
        Index("ix_evidence_run_kind_created", "run_id", "kind", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="RESTRICT"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ledger_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    creator_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    trust_class: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    redaction_status: Mapped[str] = mapped_column(String(32), nullable=False)
    redaction_policy_ref: Mapped[str | None] = mapped_column(Text)
    replay_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    locator_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ReasoningGraphRecord(Base):
    __tablename__ = "reasoning_graphs"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.reasoning-graph/v1'",
            name="ck_reasoning_graphs_schema_version",
        ),
        CheckConstraint("version >= 1", name="ck_reasoning_graphs_version"),
        Index("ix_reasoning_graphs_updated_at", "updated_at"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ReasoningNodeRecord(Base):
    __tablename__ = "reasoning_nodes"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.reasoning-graph/v1'",
            name="ck_reasoning_nodes_schema_version",
        ),
        CheckConstraint(
            "kind IN ('observation', 'fact_candidate', 'confirmed_fact', 'hypothesis', "
            "'vulnerability_candidate', 'finding', 'proof', 'negative_result')",
            name="ck_reasoning_nodes_kind",
        ),
        CheckConstraint(
            "status IN ('recorded', 'candidate', 'promoted', 'confirmed', 'unverified', "
            "'investigating', 'supported', 'rejected', 'invalidated', 'resolved', "
            "'false_positive', 'validated', 'failed')",
            name="ck_reasoning_nodes_status",
        ),
        CheckConstraint(
            "creator_type IN ('agent', 'operator', 'system', 'reducer', 'tool', "
            "'parser', 'scanner')",
            name="ck_reasoning_nodes_creator_type",
        ),
        CheckConstraint("version >= 1", name="ck_reasoning_nodes_version"),
        CheckConstraint(
            "(kind = 'finding') OR reproduction_contract_json IS NULL",
            name="ck_reasoning_nodes_reproduction_kind",
        ),
        CheckConstraint(
            "kind <> 'finding' OR status <> 'confirmed' OR "
            "reproduction_contract_json IS NOT NULL",
            name="ck_reasoning_nodes_confirmed_finding",
        ),
        ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "id", name="uq_reasoning_nodes_owner_id"),
        Index("ix_reasoning_nodes_run_kind_status", "run_id", "kind", "status"),
        Index("ix_reasoning_nodes_run_updated", "run_id", "updated_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("reasoning_graphs.run_id", ondelete="CASCADE"), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="RESTRICT"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    structured_data_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reproduction_contract_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    creator_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ReasoningEdgeRecord(Base):
    __tablename__ = "reasoning_edges"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.reasoning-graph/v1'",
            name="ck_reasoning_edges_schema_version",
        ),
        CheckConstraint(
            "relation_type IN ('supports', 'contradicts', 'derived_from', "
            "'discovered_on', 'validates', 'exploits', 'invalidates', 'depends_on')",
            name="ck_reasoning_edges_relation_type",
        ),
        CheckConstraint(
            "creator_type IN ('agent', 'operator', 'system', 'reducer', 'tool', "
            "'parser', 'scanner')",
            name="ck_reasoning_edges_creator_type",
        ),
        CheckConstraint("source_node_id <> target_node_id", name="ck_reasoning_edges_self"),
        ForeignKeyConstraint(
            ["run_id", "source_node_id"],
            ["reasoning_nodes.run_id", "reasoning_nodes.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["run_id", "target_node_id"],
            ["reasoning_nodes.run_id", "reasoning_nodes.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "run_id",
            "source_node_id",
            "target_node_id",
            "relation_type",
            name="uq_reasoning_edges_structure",
        ),
        UniqueConstraint("run_id", "id", name="uq_reasoning_edges_owner_id"),
        Index("ix_reasoning_edges_run_source", "run_id", "source_node_id"),
        Index("ix_reasoning_edges_run_target", "run_id", "target_node_id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("reasoning_graphs.run_id", ondelete="CASCADE"), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_node_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    target_node_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    creator_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ReasoningNodeEvidenceRecord(Base):
    __tablename__ = "reasoning_node_evidence"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_reasoning_node_evidence_ordinal"),
        ForeignKeyConstraint(
            ["run_id", "node_id"],
            ["reasoning_nodes.run_id", "reasoning_nodes.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["run_id", "evidence_id"],
            ["evidence_ledger.run_id", "evidence_ledger.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "run_id",
            "node_id",
            "ordinal",
            name="uq_reasoning_node_evidence_ordinal",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class ReasoningEdgeEvidenceRecord(Base):
    __tablename__ = "reasoning_edge_evidence"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_reasoning_edge_evidence_ordinal"),
        ForeignKeyConstraint(
            ["run_id", "edge_id"],
            ["reasoning_edges.run_id", "reasoning_edges.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["run_id", "evidence_id"],
            ["evidence_ledger.run_id", "evidence_ledger.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "run_id",
            "edge_id",
            "ordinal",
            name="uq_reasoning_edge_evidence_ordinal",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    edge_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class ContextCompilationRecord(Base):
    __tablename__ = "context_compilations"
    __table_args__ = (
        Index("ix_context_compilations_session_created", "session_id", "created_at"),
        Index("ix_context_compilations_run_created", "run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    model_profile: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_input_tokens: Mapped[int | None] = mapped_column(Integer)
    actual_output_tokens: Mapped[int | None] = mapped_column(Integer)
    loaded_memory_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    checkpoint_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class ContextCheckpointRecord(Base):
    __tablename__ = "context_checkpoints"
    __table_args__ = (
        Index("ix_context_checkpoints_session_created", "session_id", "created_at"),
        Index("ix_context_checkpoints_run_created", "run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checkpoint_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    compaction_stage: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_profile: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    working_memory_version: Mapped[int | None] = mapped_column(Integer)
    # A checkpoint must remain readable even if a provider-native state expires
    # or is deleted. The canonical snapshot therefore keeps this as a soft ID.
    provider_state_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    context_compilation_id: Mapped[str | None] = mapped_column(
        ForeignKey("context_compilations.id", ondelete="SET NULL"), index=True
    )
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class MemoryRecordRow(Base):
    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_scope_status", "scope_type", "scope_id", "status"),
        Index("ix_memories_type_status", "memory_type", "status"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope_id: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_keywords_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False)
    source_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    supersedes: Mapped[str | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class EngagementFactRecord(Base):
    __tablename__ = "engagement_facts"
    __table_args__ = (
        Index("ix_engagement_facts_identity", "engagement_id", "subject", "predicate"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    engagement_id: Mapped[str] = mapped_column(
        ForeignKey("engagements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    predicate: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    natural_language: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_run_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_session_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_execution_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    artifact_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    supersedes_fact_id: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_facts.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class FactRelationRecord(Base):
    __tablename__ = "fact_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_fact_id",
            "target_fact_id",
            "relation_type",
            name="uq_fact_relation_edge",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    engagement_id: Mapped[str] = mapped_column(
        ForeignKey("engagements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_fact_id: Mapped[str] = mapped_column(
        ForeignKey("engagement_facts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_fact_id: Mapped[str] = mapped_column(
        ForeignKey("engagement_facts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evidence_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    source_session_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    source_execution_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    artifact_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class WebDocumentRecord(Base):
    __tablename__ = "web_documents"
    __table_args__ = (
        Index("ix_web_documents_run_requested", "run_id", "requested_url", "fetched_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    site_name: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    language: Mapped[str | None] = mapped_column(String(64))
    raw_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    normalized_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    text_length: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_class: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_class: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class WebDocumentChunkRecord(Base):
    __tablename__ = "web_document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "sequence", name="uq_web_document_chunk_sequence"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("web_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_json: Mapped[list[float] | None] = mapped_column(JSON)


class SourceReferenceRecord(Base):
    __tablename__ = "source_references"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("web_documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    author: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class WebSearchQueryRecord(Base):
    __tablename__ = "web_search_queries"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    search_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    options_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class WebSearchResultRecord(Base):
    __tablename__ = "web_search_results"
    __table_args__ = (
        UniqueConstraint("query_id", "normalized_url", name="uq_web_search_result_url"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    query_id: Mapped[str] = mapped_column(
        ForeignKey("web_search_queries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    snippet: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class WebResearchNoteRecord(Base):
    __tablename__ = "web_research_notes"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("web_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("source_references.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    key_points_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    evidence_spans_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    missing_information_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    model_profile: Mapped[str | None] = mapped_column(String(255))
    content_trust: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class WebResearchPacketRecord(Base):
    __tablename__ = "web_research_packets"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    claims_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    source_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    disagreements_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    unresolved_questions_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    search_query_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    document_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    artifact_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    content_trust: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class TargetHttpRequestRecord(Base):
    __tablename__ = "target_http_requests"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    execution_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    tool_call_id: Mapped[str] = mapped_column(
        String(TOOL_CALL_INTENT_ID_LENGTH), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    request_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    response_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class BrowserSessionRecord(Base):
    __tablename__ = "browser_sessions"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    owner: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    browser_type: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_id: Mapped[str | None] = mapped_column(String(128), index=True)
    profile_path: Mapped[str | None] = mapped_column(Text)
    cdp_endpoint: Mapped[str | None] = mapped_column(Text)
    current_page_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    page_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    takeover_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    takeover_observation_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class BrowserPageRecord(Base):
    __tablename__ = "browser_pages"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    browser_session_id: Mapped[str] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    last_observation_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class BrowserObservationRecord(Base):
    __tablename__ = "browser_observations"
    __table_args__ = (
        UniqueConstraint(
            "browser_session_id",
            "observation_version",
            name="uq_browser_observations_session_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    browser_session_id: Mapped[str] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_id: Mapped[str] = mapped_column(
        ForeignKey("browser_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    visible_text_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    headings_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    interactive_elements_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    forms_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    alerts_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    console_errors_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    network_summary_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    screenshot_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    network_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    dom_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    observation_version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    content_trust: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class BrowserActionRecord(Base):
    __tablename__ = "browser_actions"
    __table_args__ = (
        UniqueConstraint("browser_session_id", "action_key", name="uq_browser_actions_session_key"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    action_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    browser_session_id: Mapped[str] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_id: Mapped[str] = mapped_column(
        ForeignKey("browser_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    element_ref: Mapped[str | None] = mapped_column(String(64))
    value: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    options_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    result_observation_id: Mapped[str | None] = mapped_column(
        ForeignKey("browser_observations.id", ondelete="SET NULL"), index=True
    )
    download_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class BrowserTakeoverSummaryRecord(Base):
    __tablename__ = "browser_takeover_summaries"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    browser_session_id: Mapped[str] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    released_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class ConnectorSubmissionRecord(Base):
    __tablename__ = "connector_submissions"
    __table_args__ = (
        UniqueConstraint("source", "capture_id", name="uq_connector_submissions_source_capture"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    capture_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    request_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    response_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    manifest_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class ToolCallIntentRecord(Base):
    __tablename__ = "tool_call_intents"
    __table_args__ = (
        CheckConstraint(
            "(claimed_execution_key IS NULL) = (claimed_attempt_group IS NULL)",
            name="ck_tool_call_intents_execution_claim_pair",
        ),
        Index("ix_tool_call_intents_run_created_id", "run_id", "created_at", "id"),
        Index(
            "ix_tool_call_intents_execution_claim",
            "claimed_execution_key",
            "claimed_attempt_group",
        ),
    )

    id: Mapped[str] = mapped_column(String(TOOL_CALL_INTENT_ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("agent_cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[str] = mapped_column(
        ForeignKey("agent_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    skill_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    command_preview: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_summary: Mapped[str | None] = mapped_column(Text)
    approval_level: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    claimed_execution_key: Mapped[str | None] = mapped_column(String(255))
    claimed_attempt_group: Mapped[str | None] = mapped_column(String(64))
    engine_call_id: Mapped[str | None] = mapped_column(String(255), index=True)
    execution_spec_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RuntimeApprovalRequestRecord(Base):
    __tablename__ = "runtime_approval_requests"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("agent_cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_call_intent_id: Mapped[str] = mapped_column(
        String(TOOL_CALL_INTENT_ID_LENGTH),
        ForeignKey("tool_call_intents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    context_compilation_id: Mapped[str | None] = mapped_column(
        ForeignKey("context_compilations.id", ondelete="SET NULL"), index=True
    )
    working_memory_version: Mapped[int | None] = mapped_column(Integer)
    provider_state_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_states.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    decision: Mapped[str | None] = mapped_column(String(STATUS_LENGTH))
    feedback: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class UserInputRequestRecord(Base):
    __tablename__ = "user_input_requests"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("agent_cycles.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    context_compilation_id: Mapped[str | None] = mapped_column(
        ForeignKey("context_compilations.id", ondelete="SET NULL"), index=True
    )
    working_memory_version: Mapped[int | None] = mapped_column(Integer)
    provider_state_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_states.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    response_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_messages.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RunLeaseRecord(Base):
    __tablename__ = "run_leases"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
