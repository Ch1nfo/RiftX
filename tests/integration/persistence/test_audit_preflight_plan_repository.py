from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, text
from tests.integration.persistence.test_audit_preflight_repository import (
    NOW,
    _pending_job,
    _running_job,
    _successful_finish,
)

from riftx.application.errors import RepositoryConflictError, RepositoryIntegrityError
from riftx.application.ports.audit_preflight import (
    AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION,
)
from riftx.domain.audit_preflight import AuditPreflightResult, PreflightRequest
from riftx.domain.audit_preflight_plan import (
    AuditPreflightPlan,
    AuditPreflightPlanStatus,
    AuditPreflightTokenCodec,
)
from riftx.persistence import Database
from riftx.persistence.audit_preflight import (
    AuditPreflightJobRecord,
    SQLAlchemyAuditPreflightRepository,
)
from riftx.persistence.audit_preflight_plan import (
    AuditPreflightPlanRecord,
    SQLAlchemyAuditPreflightPlanRepository,
)


async def _repositories(
    tmp_path: Path,
) -> tuple[
    Database,
    SQLAlchemyAuditPreflightRepository,
    SQLAlchemyAuditPreflightPlanRepository,
]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'preflight-plan.db'}")
    await database.create_schema()
    return (
        database,
        SQLAlchemyAuditPreflightRepository(database.session_factory),
        SQLAlchemyAuditPreflightPlanRepository(database.session_factory),
    )


async def _issue_plan(
    job_repository: SQLAlchemyAuditPreflightRepository,
    *,
    job_id: str = "preflight-plan-job",
    plan_id: str = "preflight-plan-1",
    nonce_byte: bytes = b"N",
):
    pending = _pending_job(job_id=job_id)
    running = await _running_job(job_repository, pending)
    succeeded, result, receipt = _successful_finish(running)
    persisted = await job_repository.compare_and_set(
        previous=running,
        updated=succeeded,
        result=result,
        exit_receipt=receipt,
    )
    request = PreflightRequest.model_validate_json(persisted.restricted_request_json)
    codec = AuditPreflightTokenCodec(
        key_id="preflight-key-1",
        key=b"K" * 32,
        nonce_factory=lambda size: nonce_byte * size,
    )
    return AuditPreflightPlan.from_succeeded(
        job=persisted,
        result=result,
        restricted_request=request,
        token_codec=codec,
        plan_id=plan_id,
        created_at=NOW + timedelta(minutes=4),
        expires_at=NOW + timedelta(minutes=20),
    )


async def test_create_replays_exact_plan_and_separates_verifier_from_immutable_json(
    tmp_path: Path,
) -> None:
    database, job_repository, plan_repository = await _repositories(tmp_path)
    try:
        issue = await _issue_plan(job_repository)

        created, was_created = await plan_repository.create(issue.plan)
        replayed, replay_created = await plan_repository.create(issue.plan)

        assert created == replayed == issue.plan
        assert was_created is True
        assert replay_created is False
        assert await plan_repository.get(issue.plan.plan_id) == issue.plan

        owner = await plan_repository.get_owner_binding(issue.plan.plan_id)
        owner_by_job = await plan_repository.get_owner_binding_for_job(
            issue.plan.preflight_job_id
        )
        token_binding = await plan_repository.get_token_binding(
            issue.plan.token_verifier.token_hash
        )
        assert owner == owner_by_job
        assert owner is not None
        assert owner.operator_principal_id == issue.plan.operator_principal_id
        assert token_binding is not None
        assert token_binding.token_verifier == issue.plan.token_verifier
        assert token_binding.status is AuditPreflightPlanStatus.AVAILABLE

        async with database.session() as session:
            record = await session.get(AuditPreflightPlanRecord, issue.plan.plan_id)
            job_record = await session.get(
                AuditPreflightJobRecord,
                issue.plan.preflight_job_id,
            )
            assert record is not None
            assert job_record is not None
            immutable = json.loads(record.canonical_json)
            assert immutable["target"]["repository_path"] == "/srv/source/repository"
            assert "token_verifier" not in immutable
            assert "status" not in immutable
            assert "reserved_audit_id" not in immutable
            assert issue.token not in record.canonical_json
            assert record.token_hash == issue.plan.token_verifier.token_hash
            assert record.token_nonce == issue.plan.token_verifier.nonce
            assert (
                job_record.plan_issuance_schema_version
                == AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION
            )
    finally:
        await database.dispose()


async def test_same_job_cannot_acquire_a_different_plan_identity(
    tmp_path: Path,
) -> None:
    database, job_repository, plan_repository = await _repositories(tmp_path)
    try:
        first = await _issue_plan(job_repository)
        await plan_repository.create(first.plan)
        job = await job_repository.get(first.plan.preflight_job_id)
        assert job is not None
        request = PreflightRequest.model_validate_json(job.restricted_request_json)
        assert job.result_json is not None
        result = AuditPreflightResult.model_validate_json(job.result_json)
        second = AuditPreflightPlan.from_succeeded(
            job=job,
            result=result,
            restricted_request=request,
            token_codec=AuditPreflightTokenCodec(
                key_id="preflight-key-1",
                key=b"K" * 32,
                nonce_factory=lambda size: b"Z" * size,
            ),
            plan_id="preflight-plan-2",
            created_at=NOW + timedelta(minutes=4),
            expires_at=NOW + timedelta(minutes=20),
        )

        with pytest.raises(RepositoryConflictError, match="different Plan"):
            await plan_repository.create(second.plan)
    finally:
        await database.dispose()


async def test_historical_job_without_issuance_marker_cannot_receive_or_load_plan(
    tmp_path: Path,
) -> None:
    database, job_repository, plan_repository = await _repositories(tmp_path)
    try:
        issue = await _issue_plan(job_repository)
        async with database.session_factory.begin() as session:
            await session.execute(
                text(
                    "UPDATE audit_preflight_jobs SET plan_issuance_schema_version = NULL "
                    "WHERE id = :job_id"
                ),
                {"job_id": issue.plan.preflight_job_id},
            )

        owner = await job_repository.get_owner_binding(issue.plan.preflight_job_id)
        assert owner is not None
        assert owner.plan_issuance_schema_version is None
        with pytest.raises(RepositoryConflictError, match="not eligible"):
            await plan_repository.create(issue.plan)

        async with database.session_factory.begin() as session:
            session.add(AuditPreflightPlanRecord(**_record_values(issue.plan)))
        with pytest.raises(RepositoryIntegrityError):
            await plan_repository.get(issue.plan.plan_id)
        assert (
            await plan_repository.get_token_binding(issue.plan.token_verifier.token_hash)
            is None
        )
    finally:
        await database.dispose()


async def test_concurrent_reservation_has_one_cas_winner_and_exact_replay(
    tmp_path: Path,
) -> None:
    database, job_repository, plan_repository = await _repositories(tmp_path)
    try:
        issue = await _issue_plan(job_repository)
        available, _ = await plan_repository.create(issue.plan)
        reserved_at = NOW + timedelta(minutes=5)
        first = available.reserve(
            audit_id="audit-first",
            client_request_id="223e4567-e89b-42d3-a456-426614174000",
            at=reserved_at,
        )
        second = available.reserve(
            audit_id="audit-second",
            client_request_id="323e4567-e89b-42d3-a456-426614174000",
            at=reserved_at,
        )

        outcomes = await asyncio.gather(
            plan_repository.compare_and_set(previous=available, updated=first),
            plan_repository.compare_and_set(previous=available, updated=second),
            return_exceptions=True,
        )
        winners = [value for value in outcomes if isinstance(value, AuditPreflightPlan)]
        conflicts = [value for value in outcomes if isinstance(value, RepositoryConflictError)]
        assert len(winners) == 1
        assert len(conflicts) == 1
        winner = winners[0]
        assert winner.status is AuditPreflightPlanStatus.RESERVED

        # Response-loss retry: persisted == requested is an exact no-write replay,
        # even though the caller's expected previous image is now stale.
        assert (
            await plan_repository.compare_and_set(previous=available, updated=winner)
            == winner
        )
        with pytest.raises(ValueError, match="transition is invalid"):
            await plan_repository.compare_and_set(previous=winner, updated=available)

        consumed = winner.consume(
            audit_id=winner.reserved_audit_id or "missing",
            start_request_id="423e4567-e89b-42d3-a456-426614174000",
            at=NOW + timedelta(minutes=6),
        )
        assert (
            await plan_repository.compare_and_set(previous=winner, updated=consumed)
        ) == consumed
        assert await plan_repository.get(issue.plan.plan_id) == consumed
    finally:
        await database.dispose()


async def test_token_hash_lookup_never_selects_restricted_canonical_json(
    tmp_path: Path,
) -> None:
    database, job_repository, plan_repository = await _repositories(tmp_path)
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(" ".join(statement.lower().split()))

    try:
        issue = await _issue_plan(job_repository)
        await plan_repository.create(issue.plan)
        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            binding = await plan_repository.get_token_binding(
                issue.plan.token_verifier.token_hash
            )
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)

        assert binding is not None
        selects = [statement for statement in statements if statement.startswith("select")]
        assert len(selects) == 1
        assert "token_hash" in selects[0]
        assert "canonical_json" not in selects[0]
        assert "repository_path" not in selects[0]
    finally:
        await database.dispose()


async def test_canonical_or_redundant_drift_fails_closed(
    tmp_path: Path,
) -> None:
    database, job_repository, plan_repository = await _repositories(tmp_path)
    try:
        issue = await _issue_plan(job_repository)
        await plan_repository.create(issue.plan)
        async with database.session_factory.begin() as session:
            record = await session.get(AuditPreflightPlanRecord, issue.plan.plan_id)
            assert record is not None
            record.canonical_json = record.canonical_json.replace(
                "/srv/source/repository",
                "/srv/source/other",
            )

        with pytest.raises(RepositoryIntegrityError):
            await plan_repository.get(issue.plan.plan_id)
    finally:
        await database.dispose()


def _record_values(plan: AuditPreflightPlan) -> dict[str, object]:
    """Build a raw record only for the marker-corruption load test."""

    immutable_excludes = {
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
    canonical_json = json.dumps(
        plan.model_dump(mode="json", exclude=immutable_excludes),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return {
        "id": plan.plan_id,
        "schema_version": plan.schema_version,
        "canonical_json": canonical_json,
        "plan_digest": plan.plan_digest,
        "preflight_job_id": plan.preflight_job_id,
        "preflight_client_request_id": plan.preflight_client_request_id,
        "operator_principal_id": plan.operator_principal_id,
        "authorization_scope_digest": plan.authorization_scope_digest,
        "request_schema_version": plan.request_schema_version,
        "request_digest": plan.request_digest,
        "result_schema_version": plan.result_schema_version,
        "result_digest": plan.result_digest,
        "effect_owner_digest": plan.effect_owner_digest,
        "source_node_id": plan.source_node_id,
        "source_root_identity_digest": plan.source_root_identity_digest,
        "repository_identity_digest": plan.repository_identity_digest,
        "content_identity_digest": plan.content_identity_digest,
        "backend_id": plan.backend_id,
        "image_digest": plan.image_digest,
        "policy_digest": plan.policy_digest,
        "capsule_prepare_proof_digest": plan.capsule_prepare_proof_digest,
        "target_digest": plan.target_digest,
        "scope_digest": plan.scope_digest,
        "capability_matrix_digest": plan.capability_matrix_digest,
        "minimum_feasible_budget_digest": plan.minimum_feasible_budget_digest,
        "security_context_id": plan.security_context_id,
        "security_context_digest": plan.security_context_digest,
        "preflight_completed_at": plan.preflight_completed_at,
        "created_at": plan.created_at,
        "expires_at": plan.expires_at,
        "token_verifier_schema_version": plan.token_verifier.schema_version,
        "token_key_id": plan.token_verifier.key_id,
        "token_nonce": plan.token_verifier.nonce,
        "token_hash": plan.token_verifier.token_hash,
        "status": plan.status.value,
        "state_version": plan.state_version,
        "reserved_audit_id": None,
        "reserved_client_request_id": None,
        "reserved_at": None,
        "consumed_audit_id": None,
        "consumed_start_request_id": None,
        "consumed_at": None,
        "revocation_reason": None,
        "revoked_at": None,
        "updated_at": plan.updated_at,
    }
