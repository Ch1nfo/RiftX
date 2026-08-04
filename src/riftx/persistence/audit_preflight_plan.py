"""Restricted durable persistence for Code Audit Preflight Plans.

The table stores one canonical immutable Plan payload plus redundant bounded
owner/digest columns.  Token verifier material and lifecycle facts live in
separate columns so token-hash admission never has to materialize repository
paths or the canonical Plan body.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from riftx.application.errors import (
    RepositoryConflictError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
)
from riftx.application.ports.audit_preflight import (
    AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION,
)
from riftx.application.ports.audit_preflight_plan import (
    AuditPreflightPlanOwnerBinding,
    AuditPreflightPlanTokenBinding,
)
from riftx.domain.audit_preflight import (
    AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID,
    AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION,
    AUDIT_PREFLIGHT_RESULT_SCHEMA_VERSION,
    AuditPreflightJobStatus,
)
from riftx.domain.audit_preflight_plan import (
    AUDIT_PREFLIGHT_PLAN_SCHEMA_VERSION,
    AUDIT_PREFLIGHT_TOKEN_VERIFIER_SCHEMA_VERSION,
    MAX_AUDIT_PREFLIGHT_PLAN_BYTES,
    MAX_PLAN_COUNTER,
    AuditPreflightPlan,
    AuditPreflightPlanStatus,
    AuditPreflightTokenVerifier,
)

from .audit_preflight import AuditPreflightJobRecord
from .orm import Base
from .transactions import serialized_write
from .types import UTCDateTime

SessionFactory = async_sessionmaker[AsyncSession]
_JSON_MAPPING_ADAPTER = TypeAdapter(dict[str, Any])

_IMMUTABLE_PLAN_EXCLUDES = frozenset(
    {
        "token_verifier",
        "status",
        "state_version",
        "reserved_audit_id",
        "reserved_client_request_id",
        "reserved_at",
        "consumed_audit_id",
        "consumed_start_request_id",
        "consumed_at",
        "revocation_reason",
        "revoked_at",
        "updated_at",
    }
)


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


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


def _optional_canonical_uuid_check(column: str) -> str:
    return f"{column} IS NULL OR ({_canonical_uuid_check(column)})"


class AuditPreflightPlanRecord(Base):
    __tablename__ = "audit_preflight_plans"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.audit-preflight-plan/v1'",
            name="ck_audit_preflight_plans_schema",
        ),
        CheckConstraint(
            "request_schema_version = 'riftx.audit-preflight-request/v1'",
            name="ck_audit_preflight_plans_request_schema",
        ),
        CheckConstraint(
            "result_schema_version = 'riftx.audit-preflight-result/v1'",
            name="ck_audit_preflight_plans_result_schema",
        ),
        CheckConstraint(
            "token_verifier_schema_version = "
            "'riftx.audit-preflight-token-verifier/v1'",
            name="ck_audit_preflight_plans_token_verifier_schema",
        ),
        CheckConstraint(
            "source_node_id = 'local'",
            name="ck_audit_preflight_plans_local_node",
        ),
        CheckConstraint(
            "security_context_id = 'riftx.audit-empty-security-context/v1'",
            name="ck_audit_preflight_plans_empty_context",
        ),
        CheckConstraint(
            "status IN ('available', 'reserved', 'consumed', 'revoked')",
            name="ck_audit_preflight_plans_status",
        ),
        CheckConstraint(
            f"state_version BETWEEN 1 AND {MAX_PLAN_COUNTER}",
            name="ck_audit_preflight_plans_state_version",
        ),
        CheckConstraint(
            f"length(canonical_json) BETWEEN 2 AND {MAX_AUDIT_PREFLIGHT_PLAN_BYTES}",
            name="ck_audit_preflight_plans_canonical_size",
        ),
        CheckConstraint(
            "length(token_key_id) BETWEEN 1 AND 64 "
            "AND token_key_id = trim(token_key_id)",
            name="ck_audit_preflight_plans_token_key_id",
        ),
        CheckConstraint(
            "length(token_nonce) = 43",
            name="ck_audit_preflight_plans_token_nonce",
        ),
        CheckConstraint(
            _canonical_uuid_check("preflight_client_request_id"),
            name="ck_audit_preflight_plans_preflight_request_id",
        ),
        CheckConstraint(
            _optional_canonical_uuid_check("reserved_client_request_id"),
            name="ck_audit_preflight_plans_reserved_request_id",
        ),
        CheckConstraint(
            _optional_canonical_uuid_check("consumed_start_request_id"),
            name="ck_audit_preflight_plans_consumed_request_id",
        ),
        CheckConstraint(
            "(reserved_audit_id IS NULL AND reserved_client_request_id IS NULL "
            "AND reserved_at IS NULL) OR "
            "(reserved_audit_id IS NOT NULL AND reserved_client_request_id IS NOT NULL "
            "AND reserved_at IS NOT NULL)",
            name="ck_audit_preflight_plans_reservation_shape",
        ),
        CheckConstraint(
            "(consumed_audit_id IS NULL AND consumed_start_request_id IS NULL "
            "AND consumed_at IS NULL) OR "
            "(consumed_audit_id IS NOT NULL AND consumed_start_request_id IS NOT NULL "
            "AND consumed_at IS NOT NULL)",
            name="ck_audit_preflight_plans_consumption_shape",
        ),
        CheckConstraint(
            "(revocation_reason IS NULL AND revoked_at IS NULL) OR "
            "(revocation_reason IS NOT NULL AND revoked_at IS NOT NULL)",
            name="ck_audit_preflight_plans_revocation_shape",
        ),
        CheckConstraint(
            "(status = 'available' AND state_version = 1 "
            "AND reserved_audit_id IS NULL AND consumed_audit_id IS NULL "
            "AND revocation_reason IS NULL AND updated_at = created_at) OR "
            "(status = 'reserved' AND state_version = 2 "
            "AND reserved_audit_id IS NOT NULL AND consumed_audit_id IS NULL "
            "AND revocation_reason IS NULL AND updated_at = reserved_at) OR "
            "(status = 'consumed' AND state_version = 3 "
            "AND reserved_audit_id IS NOT NULL AND consumed_audit_id IS NOT NULL "
            "AND consumed_audit_id = reserved_audit_id "
            "AND revocation_reason IS NULL AND updated_at = consumed_at) OR "
            "(status = 'revoked' AND consumed_audit_id IS NULL "
            "AND revocation_reason IS NOT NULL AND updated_at = revoked_at "
            "AND ((reserved_audit_id IS NULL AND state_version = 2) OR "
            "(reserved_audit_id IS NOT NULL AND state_version = 3)))",
            name="ck_audit_preflight_plans_lifecycle",
        ),
        CheckConstraint(
            "preflight_completed_at <= created_at AND created_at < expires_at "
            "AND updated_at >= created_at "
            "AND (reserved_at IS NULL OR "
            "(reserved_at >= created_at AND reserved_at < expires_at)) "
            "AND (consumed_at IS NULL OR "
            "(consumed_at >= reserved_at AND consumed_at < expires_at)) "
            "AND (revoked_at IS NULL OR "
            "(revoked_at >= created_at AND "
            "(reserved_at IS NULL OR revoked_at >= reserved_at)))",
            name="ck_audit_preflight_plans_timestamps",
        ),
        *(
            CheckConstraint(
                _lower_hex_digest_check(column),
                name=f"ck_audit_preflight_plans_{column}",
            )
            for column in (
                "plan_digest",
                "authorization_scope_digest",
                "request_digest",
                "result_digest",
                "effect_owner_digest",
                "source_root_identity_digest",
                "repository_identity_digest",
                "content_identity_digest",
                "image_digest",
                "policy_digest",
                "capsule_prepare_proof_digest",
                "target_digest",
                "scope_digest",
                "capability_matrix_digest",
                "minimum_feasible_budget_digest",
                "security_context_digest",
                "token_hash",
            )
        ),
        UniqueConstraint(
            "preflight_job_id",
            name="uq_audit_preflight_plans_preflight_job",
        ),
        UniqueConstraint(
            "plan_digest",
            name="uq_audit_preflight_plans_plan_digest",
        ),
        UniqueConstraint(
            "token_hash",
            name="uq_audit_preflight_plans_token_hash",
        ),
        UniqueConstraint(
            "reserved_audit_id",
            name="uq_audit_preflight_plans_reserved_audit",
        ),
        UniqueConstraint(
            "consumed_audit_id",
            name="uq_audit_preflight_plans_consumed_audit",
        ),
        UniqueConstraint(
            "id",
            "plan_digest",
            "operator_principal_id",
            "authorization_scope_digest",
            "security_context_id",
            "security_context_digest",
            "reserved_audit_id",
            name="uq_audit_preflight_plans_context_binding",
        ),
        Index(
            "ix_audit_preflight_plans_owner",
            "operator_principal_id",
            "authorization_scope_digest",
            "status",
            "expires_at",
            "id",
        ),
        Index(
            "ix_audit_preflight_plans_key_lifecycle",
            "token_key_id",
            "status",
            "expires_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    preflight_job_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "audit_preflight_jobs.id",
            name="fk_audit_preflight_plans_preflight_job",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    preflight_client_request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operator_principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    authorization_scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    result_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_owner_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_root_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    repository_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    content_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    backend_id: Mapped[str] = mapped_column(String(128), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    capsule_prepare_proof_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_matrix_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    minimum_feasible_budget_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    security_context_id: Mapped[str] = mapped_column(String(128), nullable=False)
    security_context_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    preflight_completed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    token_verifier_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    token_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    token_nonce: Mapped[str] = mapped_column(String(43), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_audit_id: Mapped[str | None] = mapped_column(String(128))
    reserved_client_request_id: Mapped[str | None] = mapped_column(String(36))
    reserved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    consumed_audit_id: Mapped[str | None] = mapped_column(String(128))
    consumed_start_request_id: Mapped[str | None] = mapped_column(String(36))
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revocation_reason: Mapped[str | None] = mapped_column(String(128))
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SQLAlchemyAuditPreflightPlanRepository:
    """Strict create/replay and lifecycle-CAS repository for Preflight Plans."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        plan: AuditPreflightPlan,
    ) -> tuple[AuditPreflightPlan, bool]:
        if (
            plan.status is not AuditPreflightPlanStatus.AVAILABLE
            or plan.state_version != 1
        ):
            raise ValueError("new Audit Preflight Plan must be available at version one")
        try:
            async with serialized_write(self._session_factory) as session:
                await _require_plan_issuance_job(session, plan)
                existing_id = await session.scalar(
                    select(AuditPreflightPlanRecord.id).where(
                        AuditPreflightPlanRecord.preflight_job_id == plan.preflight_job_id
                    )
                )
                if existing_id is not None:
                    existing = await load_validated_audit_preflight_plan(session, existing_id)
                    return _require_exact_create_replay(existing, plan), False
                session.add(_plan_to_record(plan))
                await session.flush()
                return plan, True
        except IntegrityError as exc:
            try:
                async with self._session_factory() as session:
                    existing_id = await session.scalar(
                        select(AuditPreflightPlanRecord.id).where(
                            AuditPreflightPlanRecord.preflight_job_id == plan.preflight_job_id
                        )
                    )
                    if existing_id is not None:
                        existing = await load_validated_audit_preflight_plan(
                            session,
                            existing_id,
                        )
                        return _require_exact_create_replay(existing, plan), False
            except RepositoryIntegrityError:
                raise
            except SQLAlchemyError as read_exc:
                raise RepositoryUnavailableError(
                    "Audit Preflight Plan replay lookup is unavailable"
                ) from read_exc
            raise RepositoryConflictError(
                "Audit Preflight Plan creation conflicts with durable ownership"
            ) from exc
        except RepositoryConflictError:
            raise
        except RepositoryIntegrityError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError(
                "Audit Preflight Plan creation is unavailable"
            ) from exc

    async def get_owner_binding(
        self,
        plan_id: str,
    ) -> AuditPreflightPlanOwnerBinding | None:
        return await self._get_owner_binding(
            AuditPreflightPlanRecord.id == plan_id,
            integrity_id=plan_id,
        )

    async def get_owner_binding_for_job(
        self,
        preflight_job_id: str,
    ) -> AuditPreflightPlanOwnerBinding | None:
        return await self._get_owner_binding(
            AuditPreflightPlanRecord.preflight_job_id == preflight_job_id,
            integrity_id=preflight_job_id,
        )

    async def _get_owner_binding(
        self,
        predicate: object,
        *,
        integrity_id: str,
    ) -> AuditPreflightPlanOwnerBinding | None:
        try:
            async with self._session_factory() as session:
                row = (
                    (
                        await session.execute(
                            _owner_projection().where(predicate)  # type: ignore[arg-type]
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if row is None:
                return None
            return _owner_binding_from_row(row)
        except RepositoryIntegrityError:
            raise
        except (TypeError, ValueError):
            raise RepositoryIntegrityError(
                "AuditPreflightPlan",
                integrity_id,
                reason_code="invalid_owner_projection",
            ) from None
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError(
                "Audit Preflight Plan owner lookup is unavailable"
            ) from exc

    async def get_token_binding(
        self,
        token_hash: str,
    ) -> AuditPreflightPlanTokenBinding | None:
        _require_digest(token_hash, label="token hash")
        try:
            async with self._session_factory() as session:
                row = (
                    (
                        await session.execute(
                            _token_projection().where(
                                AuditPreflightPlanRecord.token_hash == token_hash
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if row is None:
                return None
            return _token_binding_from_row(row)
        except RepositoryIntegrityError:
            raise
        except (TypeError, ValueError):
            raise RepositoryIntegrityError(
                "AuditPreflightPlan",
                "token-binding",
                reason_code="invalid_token_projection",
            ) from None
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError(
                "Audit Preflight Plan token lookup is unavailable"
            ) from exc

    async def get(self, plan_id: str) -> AuditPreflightPlan | None:
        try:
            async with self._session_factory() as session:
                return await load_validated_audit_preflight_plan(session, plan_id)
        except RepositoryIntegrityError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError(
                "Audit Preflight Plan lookup is unavailable"
            ) from exc

    async def compare_and_set(
        self,
        *,
        previous: AuditPreflightPlan,
        updated: AuditPreflightPlan,
    ) -> AuditPreflightPlan:
        _validate_cas_request(previous, updated)
        try:
            async with serialized_write(self._session_factory) as session:
                persisted, _ = await compare_and_set_audit_preflight_plan(
                    session,
                    previous=previous,
                    updated=updated,
                )
                return persisted
        except RepositoryConflictError:
            raise
        except RepositoryIntegrityError:
            raise
        except IntegrityError as exc:
            raise RepositoryConflictError(
                "Audit Preflight Plan compare-and-set conflicts with durable facts"
            ) from exc
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError(
                "Audit Preflight Plan compare-and-set is unavailable"
            ) from exc


async def load_validated_audit_preflight_plan(
    session: AsyncSession,
    plan_id: str,
    *,
    for_update: bool = False,
) -> AuditPreflightPlan | None:
    """Load the restricted canonical payload and prove every redundant column."""

    statement = (
        select(
            AuditPreflightPlanRecord,
            AuditPreflightJobRecord,
        )
        .join(
            AuditPreflightJobRecord,
            AuditPreflightJobRecord.id == AuditPreflightPlanRecord.preflight_job_id,
        )
        .where(AuditPreflightPlanRecord.id == plan_id)
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        return None
    record, preflight_job_record = row
    try:
        immutable = _strict_json_mapping(record.canonical_json)
        if _IMMUTABLE_PLAN_EXCLUDES.intersection(immutable):
            raise ValueError("canonical immutable Plan contains lifecycle fields")
        payload: dict[str, object] = {
            **immutable,
            "token_verifier": {
                "schema_version": record.token_verifier_schema_version,
                "key_id": record.token_key_id,
                "nonce": record.token_nonce,
                "token_hash": record.token_hash,
            },
            "status": record.status,
            "state_version": record.state_version,
            "reserved_audit_id": record.reserved_audit_id,
            "reserved_client_request_id": record.reserved_client_request_id,
            "reserved_at": record.reserved_at,
            "consumed_audit_id": record.consumed_audit_id,
            "consumed_start_request_id": record.consumed_start_request_id,
            "consumed_at": record.consumed_at,
            "revocation_reason": record.revocation_reason,
            "revoked_at": record.revoked_at,
            "updated_at": record.updated_at,
        }
        encoded_payload = _JSON_MAPPING_ADAPTER.dump_python(payload, mode="json")
        plan = AuditPreflightPlan.model_validate_json(
            json.dumps(
                encoded_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError):
        raise RepositoryIntegrityError(
            "AuditPreflightPlan",
            plan_id,
            reason_code="invalid_canonical_plan",
        ) from None
    if _immutable_plan_json(plan) != record.canonical_json:
        raise RepositoryIntegrityError(
            "AuditPreflightPlan",
            plan_id,
            reason_code="noncanonical_plan_bytes",
        )
    try:
        _validate_redundant_columns(record, plan)
    except (TypeError, ValueError):
        raise RepositoryIntegrityError(
            "AuditPreflightPlan",
            plan_id,
            reason_code="redundant_plan_drift",
        ) from None
    try:
        _validate_preflight_job_binding(preflight_job_record, plan)
    except (TypeError, ValueError):
        raise RepositoryIntegrityError(
            "AuditPreflightPlan",
            plan_id,
            reason_code="preflight_owner_drift",
        ) from None
    return plan


async def compare_and_set_audit_preflight_plan(
    session: AsyncSession,
    *,
    previous: AuditPreflightPlan,
    updated: AuditPreflightPlan,
) -> tuple[AuditPreflightPlan, bool]:
    """Apply one lifecycle CAS inside the caller's existing transaction.

    This helper is intentionally session-scoped so Create v2 and the future
    StartIntent UoW can reserve/consume a Plan atomically with their aggregate.
    """

    _validate_cas_request(previous, updated)
    persisted = await load_validated_audit_preflight_plan(
        session,
        previous.plan_id,
        for_update=True,
    )
    if persisted is None:
        raise RepositoryConflictError("Audit Preflight Plan no longer exists")
    if persisted == updated:
        return persisted, False
    if persisted != previous:
        raise RepositoryConflictError(
            "Audit Preflight Plan changed before compare-and-set"
        )
    result = await session.execute(
        update(AuditPreflightPlanRecord)
        .where(
            AuditPreflightPlanRecord.id == previous.plan_id,
            AuditPreflightPlanRecord.status == previous.status.value,
            AuditPreflightPlanRecord.state_version == previous.state_version,
        )
        .values(**_lifecycle_values(updated))
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise RepositoryConflictError(
            "Audit Preflight Plan changed before compare-and-set"
        )
    await session.flush()
    return updated, True


def _plan_to_record(plan: AuditPreflightPlan) -> AuditPreflightPlanRecord:
    verifier = plan.token_verifier
    return AuditPreflightPlanRecord(
        id=plan.plan_id,
        schema_version=plan.schema_version,
        canonical_json=_immutable_plan_json(plan),
        plan_digest=plan.plan_digest,
        preflight_job_id=plan.preflight_job_id,
        preflight_client_request_id=plan.preflight_client_request_id,
        operator_principal_id=plan.operator_principal_id,
        authorization_scope_digest=plan.authorization_scope_digest,
        request_schema_version=plan.request_schema_version,
        request_digest=plan.request_digest,
        result_schema_version=plan.result_schema_version,
        result_digest=plan.result_digest,
        effect_owner_digest=plan.effect_owner_digest,
        source_node_id=plan.source_node_id,
        source_root_identity_digest=plan.source_root_identity_digest,
        repository_identity_digest=plan.repository_identity_digest,
        content_identity_digest=plan.content_identity_digest,
        backend_id=plan.backend_id,
        image_digest=plan.image_digest,
        policy_digest=plan.policy_digest,
        capsule_prepare_proof_digest=plan.capsule_prepare_proof_digest,
        target_digest=plan.target_digest,
        scope_digest=plan.scope_digest,
        capability_matrix_digest=plan.capability_matrix_digest,
        minimum_feasible_budget_digest=plan.minimum_feasible_budget_digest,
        security_context_id=plan.security_context_id,
        security_context_digest=plan.security_context_digest,
        preflight_completed_at=plan.preflight_completed_at,
        created_at=plan.created_at,
        expires_at=plan.expires_at,
        token_verifier_schema_version=verifier.schema_version,
        token_key_id=verifier.key_id,
        token_nonce=verifier.nonce,
        token_hash=verifier.token_hash,
        **_lifecycle_values(plan),
    )


def _lifecycle_values(plan: AuditPreflightPlan) -> dict[str, object]:
    return {
        "status": plan.status.value,
        "state_version": plan.state_version,
        "reserved_audit_id": plan.reserved_audit_id,
        "reserved_client_request_id": plan.reserved_client_request_id,
        "reserved_at": plan.reserved_at,
        "consumed_audit_id": plan.consumed_audit_id,
        "consumed_start_request_id": plan.consumed_start_request_id,
        "consumed_at": plan.consumed_at,
        "revocation_reason": plan.revocation_reason,
        "revoked_at": plan.revoked_at,
        "updated_at": plan.updated_at,
    }


def _immutable_plan_json(plan: AuditPreflightPlan) -> str:
    payload = plan.model_dump(mode="json", exclude=_IMMUTABLE_PLAN_EXCLUDES)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(canonical.encode("utf-8")) > MAX_AUDIT_PREFLIGHT_PLAN_BYTES:
        raise ValueError("Audit Preflight Plan immutable payload exceeds its byte limit")
    return canonical


def _strict_json_mapping(value: str) -> dict[str, object]:
    if not isinstance(value, str) or not value:
        raise ValueError("canonical immutable Plan must be a JSON object")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, item in pairs:
            if key in parsed:
                raise ValueError("canonical immutable Plan contains duplicate keys")
            parsed[key] = item
        return parsed

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant {value!r} is forbidden")

    parsed = json.loads(
        value,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    if not isinstance(parsed, dict):
        raise ValueError("canonical immutable Plan must be a JSON object")
    return parsed


def _validate_redundant_columns(
    record: AuditPreflightPlanRecord,
    plan: AuditPreflightPlan,
) -> None:
    bindings: tuple[tuple[object, object], ...] = (
        (record.id, plan.plan_id),
        (record.schema_version, plan.schema_version),
        (record.plan_digest, plan.plan_digest),
        (record.preflight_job_id, plan.preflight_job_id),
        (record.preflight_client_request_id, plan.preflight_client_request_id),
        (record.operator_principal_id, plan.operator_principal_id),
        (record.authorization_scope_digest, plan.authorization_scope_digest),
        (record.request_schema_version, plan.request_schema_version),
        (record.request_digest, plan.request_digest),
        (record.result_schema_version, plan.result_schema_version),
        (record.result_digest, plan.result_digest),
        (record.effect_owner_digest, plan.effect_owner_digest),
        (record.source_node_id, plan.source_node_id),
        (record.source_root_identity_digest, plan.source_root_identity_digest),
        (record.repository_identity_digest, plan.repository_identity_digest),
        (record.content_identity_digest, plan.content_identity_digest),
        (record.backend_id, plan.backend_id),
        (record.image_digest, plan.image_digest),
        (record.policy_digest, plan.policy_digest),
        (record.capsule_prepare_proof_digest, plan.capsule_prepare_proof_digest),
        (record.target_digest, plan.target_digest),
        (record.scope_digest, plan.scope_digest),
        (record.capability_matrix_digest, plan.capability_matrix_digest),
        (
            record.minimum_feasible_budget_digest,
            plan.minimum_feasible_budget_digest,
        ),
        (record.security_context_id, plan.security_context_id),
        (record.security_context_digest, plan.security_context_digest),
        (record.preflight_completed_at, plan.preflight_completed_at),
        (record.created_at, plan.created_at),
        (record.expires_at, plan.expires_at),
        (record.token_verifier_schema_version, plan.token_verifier.schema_version),
        (record.token_key_id, plan.token_verifier.key_id),
        (record.token_nonce, plan.token_verifier.nonce),
        (record.token_hash, plan.token_verifier.token_hash),
        (record.status, plan.status.value),
        (record.state_version, plan.state_version),
        (record.reserved_audit_id, plan.reserved_audit_id),
        (record.reserved_client_request_id, plan.reserved_client_request_id),
        (record.reserved_at, plan.reserved_at),
        (record.consumed_audit_id, plan.consumed_audit_id),
        (record.consumed_start_request_id, plan.consumed_start_request_id),
        (record.consumed_at, plan.consumed_at),
        (record.revocation_reason, plan.revocation_reason),
        (record.revoked_at, plan.revoked_at),
        (record.updated_at, plan.updated_at),
    )
    if any(actual != expected for actual, expected in bindings):
        raise ValueError("Audit Preflight Plan redundant columns drifted")


def _owner_projection():
    return select(
        AuditPreflightPlanRecord.id.label("plan_id"),
        AuditPreflightPlanRecord.preflight_job_id,
        AuditPreflightPlanRecord.operator_principal_id,
        AuditPreflightPlanRecord.authorization_scope_digest,
        AuditPreflightPlanRecord.plan_digest,
        AuditPreflightPlanRecord.status,
        AuditPreflightPlanRecord.state_version,
        AuditPreflightPlanRecord.expires_at,
        AuditPreflightPlanRecord.reserved_audit_id,
        AuditPreflightPlanRecord.reserved_client_request_id,
        AuditPreflightPlanRecord.consumed_audit_id,
    ).join(
        AuditPreflightJobRecord,
        AuditPreflightJobRecord.id == AuditPreflightPlanRecord.preflight_job_id,
    ).where(
        AuditPreflightJobRecord.plan_issuance_schema_version
        == AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION
    )


def _token_projection():
    return select(
        AuditPreflightPlanRecord.id.label("plan_id"),
        AuditPreflightPlanRecord.preflight_job_id,
        AuditPreflightPlanRecord.operator_principal_id,
        AuditPreflightPlanRecord.authorization_scope_digest,
        AuditPreflightPlanRecord.plan_digest,
        AuditPreflightPlanRecord.token_verifier_schema_version,
        AuditPreflightPlanRecord.token_key_id,
        AuditPreflightPlanRecord.token_nonce,
        AuditPreflightPlanRecord.token_hash,
        AuditPreflightPlanRecord.status,
        AuditPreflightPlanRecord.state_version,
        AuditPreflightPlanRecord.expires_at,
        AuditPreflightPlanRecord.reserved_audit_id,
        AuditPreflightPlanRecord.reserved_client_request_id,
        AuditPreflightPlanRecord.consumed_audit_id,
        AuditPreflightPlanRecord.consumed_start_request_id,
    ).join(
        AuditPreflightJobRecord,
        AuditPreflightJobRecord.id == AuditPreflightPlanRecord.preflight_job_id,
    ).where(
        AuditPreflightJobRecord.plan_issuance_schema_version
        == AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION
    )


async def _require_plan_issuance_job(
    session: AsyncSession,
    plan: AuditPreflightPlan,
) -> None:
    record = await session.scalar(
        select(AuditPreflightJobRecord).where(
            AuditPreflightJobRecord.id == plan.preflight_job_id
        )
    )
    if record is None:
        raise RepositoryConflictError(
            "Audit Preflight Job is not eligible for Plan issuance"
        )
    try:
        _validate_preflight_job_binding(record, plan)
    except (TypeError, ValueError):
        raise RepositoryConflictError(
            "Audit Preflight Job is not eligible for Plan issuance"
        ) from None


def _validate_preflight_job_binding(
    record: AuditPreflightJobRecord,
    plan: AuditPreflightPlan,
) -> None:
    bindings: tuple[tuple[object, object], ...] = (
        (record.id, plan.preflight_job_id),
        (record.client_request_id, plan.preflight_client_request_id),
        (record.operator_principal_id, plan.operator_principal_id),
        (record.authorization_scope_digest, plan.authorization_scope_digest),
        (record.request_schema_version, plan.request_schema_version),
        (record.request_digest, plan.request_digest),
        (record.result_digest, plan.result_digest),
        (record.effect_owner_digest, plan.effect_owner_digest),
        (record.source_node_id, plan.source_node_id),
        (record.source_root_identity_digest, plan.source_root_identity_digest),
        (record.backend_id, plan.backend_id),
        (record.image_digest, plan.image_digest),
        (record.policy_digest, plan.policy_digest),
        (record.capsule_prepare_proof_digest, plan.capsule_prepare_proof_digest),
        (record.canonical_empty_context_id, plan.security_context_id),
        (record.canonical_empty_context_digest, plan.security_context_digest),
        (record.finished_at, plan.preflight_completed_at),
    )
    if (
        record.plan_issuance_schema_version
        != AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION
        or record.status != AuditPreflightJobStatus.SUCCEEDED.value
        or record.expires_at < plan.expires_at
        or any(actual != expected for actual, expected in bindings)
    ):
        raise ValueError("Preflight Job does not bind the durable Plan")


def _owner_binding_from_row(
    row: Mapping[str, object],
) -> AuditPreflightPlanOwnerBinding:
    status = AuditPreflightPlanStatus(row["status"])
    state_version = _require_state_version(row["state_version"])
    expires_at = _require_datetime(row["expires_at"], label="expiry")
    plan_id = _require_safe_id(row["plan_id"], label="Plan ID")
    preflight_job_id = _require_safe_id(
        row["preflight_job_id"],
        label="Preflight Job ID",
    )
    operator_principal_id = _require_safe_id(
        row["operator_principal_id"],
        label="operator principal ID",
    )
    authorization_scope_digest = _require_digest_value(
        row["authorization_scope_digest"],
        label="authorization scope digest",
    )
    plan_digest = _require_digest_value(row["plan_digest"], label="Plan digest")
    reserved_audit_id = _optional_safe_id(
        row["reserved_audit_id"],
        label="reserved Audit ID",
    )
    reserved_client_request_id = _optional_string(row["reserved_client_request_id"])
    consumed_audit_id = _optional_safe_id(
        row["consumed_audit_id"],
        label="consumed Audit ID",
    )
    _validate_bounded_lifecycle(
        status=status,
        state_version=state_version,
        reserved_audit_id=reserved_audit_id,
        reserved_client_request_id=reserved_client_request_id,
        consumed_audit_id=consumed_audit_id,
    )
    return AuditPreflightPlanOwnerBinding(
        plan_id=plan_id,
        preflight_job_id=preflight_job_id,
        operator_principal_id=operator_principal_id,
        authorization_scope_digest=authorization_scope_digest,
        plan_digest=plan_digest,
        status=status,
        state_version=state_version,
        expires_at=expires_at,
        reserved_audit_id=reserved_audit_id,
        reserved_client_request_id=reserved_client_request_id,
        consumed_audit_id=consumed_audit_id,
    )


def _token_binding_from_row(
    row: Mapping[str, object],
) -> AuditPreflightPlanTokenBinding:
    owner = _owner_binding_from_row(row)
    verifier = AuditPreflightTokenVerifier(
        schema_version=row["token_verifier_schema_version"],
        key_id=row["token_key_id"],
        nonce=row["token_nonce"],
        token_hash=row["token_hash"],
    )
    consumed_start_request_id = _optional_string(row["consumed_start_request_id"])
    if (owner.consumed_audit_id is None) != (consumed_start_request_id is None):
        raise ValueError("bounded Plan consumption projection is incomplete")
    return AuditPreflightPlanTokenBinding(
        plan_id=owner.plan_id,
        preflight_job_id=owner.preflight_job_id,
        operator_principal_id=owner.operator_principal_id,
        authorization_scope_digest=owner.authorization_scope_digest,
        plan_digest=owner.plan_digest,
        token_verifier=verifier,
        status=owner.status,
        state_version=owner.state_version,
        expires_at=owner.expires_at,
        reserved_audit_id=owner.reserved_audit_id,
        reserved_client_request_id=owner.reserved_client_request_id,
        consumed_audit_id=owner.consumed_audit_id,
        consumed_start_request_id=consumed_start_request_id,
    )


def _validate_bounded_lifecycle(
    *,
    status: AuditPreflightPlanStatus,
    state_version: int,
    reserved_audit_id: str | None,
    reserved_client_request_id: str | None,
    consumed_audit_id: str | None,
) -> None:
    has_reservation = reserved_audit_id is not None and reserved_client_request_id is not None
    if (reserved_audit_id is None) != (reserved_client_request_id is None):
        raise ValueError("bounded Plan reservation projection is incomplete")
    if status is AuditPreflightPlanStatus.AVAILABLE:
        valid = state_version == 1 and not has_reservation and consumed_audit_id is None
    elif status is AuditPreflightPlanStatus.RESERVED:
        valid = state_version == 2 and has_reservation and consumed_audit_id is None
    elif status is AuditPreflightPlanStatus.CONSUMED:
        valid = (
            state_version == 3
            and has_reservation
            and consumed_audit_id == reserved_audit_id
        )
    else:
        valid = (
            consumed_audit_id is None
            and (
                (not has_reservation and state_version == 2)
                or (has_reservation and state_version == 3)
            )
        )
    if not valid:
        raise ValueError("bounded Plan lifecycle projection is invalid")


def _validate_cas_request(
    previous: AuditPreflightPlan,
    updated: AuditPreflightPlan,
) -> None:
    if previous.plan_id != updated.plan_id:
        raise ValueError("Audit Preflight Plan compare-and-set IDs differ")
    if (
        previous.identity_json() != updated.identity_json()
        or previous.token_verifier != updated.token_verifier
    ):
        raise ValueError("Audit Preflight Plan compare-and-set changed immutable facts")
    if previous == updated:
        return

    expected: AuditPreflightPlan
    if updated.status is AuditPreflightPlanStatus.RESERVED:
        assert updated.reserved_audit_id is not None
        assert updated.reserved_client_request_id is not None
        assert updated.reserved_at is not None
        expected = previous.reserve(
            audit_id=updated.reserved_audit_id,
            client_request_id=updated.reserved_client_request_id,
            at=updated.reserved_at,
        )
    elif updated.status is AuditPreflightPlanStatus.CONSUMED:
        assert updated.consumed_audit_id is not None
        assert updated.consumed_start_request_id is not None
        assert updated.consumed_at is not None
        expected = previous.consume(
            audit_id=updated.consumed_audit_id,
            start_request_id=updated.consumed_start_request_id,
            at=updated.consumed_at,
        )
    elif updated.status is AuditPreflightPlanStatus.REVOKED:
        assert updated.revocation_reason is not None
        assert updated.revoked_at is not None
        expected = previous.revoke(
            reason_code=updated.revocation_reason,
            at=updated.revoked_at,
        )
    else:
        raise ValueError("Audit Preflight Plan compare-and-set transition is invalid")
    if expected != updated:
        raise ValueError("Audit Preflight Plan compare-and-set transition facts differ")


def _require_exact_create_replay(
    existing: AuditPreflightPlan | None,
    requested: AuditPreflightPlan,
) -> AuditPreflightPlan:
    if existing is None:
        raise RepositoryIntegrityError("AuditPreflightPlan", requested.plan_id)
    if existing != requested:
        raise RepositoryConflictError(
            "Audit Preflight Job is already bound to a different Plan"
        )
    return existing


def _require_digest(value: str, *, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"Audit Preflight Plan {label} must be a lower-hex digest")


def _require_digest_value(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Audit Preflight Plan {label} must be a string")
    _require_digest(value, label=label)
    return value


def _require_safe_id(value: object, *, label: str) -> str:
    allowed = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:@+~-")
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(character not in allowed for character in value)
    ):
        raise ValueError(f"Audit Preflight Plan {label} is invalid")
    return value


def _optional_safe_id(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _require_safe_id(value, label=label)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Audit Preflight Plan projection contains invalid text")
    return value


def _require_state_version(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_PLAN_COUNTER:
        raise ValueError("Audit Preflight Plan state version is invalid")
    return value


def _require_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"Audit Preflight Plan {label} must be timezone-aware")
    return value


assert AUDIT_PREFLIGHT_PLAN_SCHEMA_VERSION == "riftx.audit-preflight-plan/v1"
assert AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION == "riftx.audit-preflight-request/v1"
assert AUDIT_PREFLIGHT_RESULT_SCHEMA_VERSION == "riftx.audit-preflight-result/v1"
assert (
    AUDIT_PREFLIGHT_TOKEN_VERIFIER_SCHEMA_VERSION
    == "riftx.audit-preflight-token-verifier/v1"
)
assert AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID == "riftx.audit-empty-security-context/v1"


__all__ = [
    "AuditPreflightPlanRecord",
    "SQLAlchemyAuditPreflightPlanRepository",
    "compare_and_set_audit_preflight_plan",
    "load_validated_audit_preflight_plan",
]
