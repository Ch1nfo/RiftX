from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from riftx.application.errors import RepositoryConflictError, RepositoryIntegrityError
from riftx.domain.audit import AuditMode, SourceTargetKind
from riftx.domain.audit_preflight import (
    AuditPreflightBudgetStatus,
    AuditPreflightCapabilityFact,
    AuditPreflightCapabilityMatrix,
    AuditPreflightCapabilityStatus,
    AuditPreflightExitReceipt,
    AuditPreflightExitTerminalState,
    AuditPreflightJob,
    AuditPreflightJobStatus,
    AuditPreflightLanguageEstimate,
    AuditPreflightMinimumFeasibleBudget,
    AuditPreflightObservedTerminalState,
    AuditPreflightResult,
    AuditPreflightSourceExecutionTarget,
    AuditPreflightStopDisposition,
    AuditPreflightStopReceipt,
    AuditPreflightTarget,
    PreflightRequest,
)
from riftx.domain.runner import RunnerPrincipal
from riftx.persistence import Database
from riftx.persistence.audit_preflight import (
    AuditPreflightExitReceiptRecord,
    AuditPreflightJobRecord,
    AuditPreflightJobRequestRecord,
    AuditPreflightResultRecord,
    AuditPreflightStopReceiptRecord,
    SQLAlchemyAuditPreflightRepository,
)

NOW = datetime(2026, 8, 4, 8, tzinfo=UTC)
REVISION = "1" * 40


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _replace[T](value: T, **updates: object) -> T:
    payload = value.model_dump(mode="python")  # type: ignore[attr-defined]
    payload.update(updates)
    return type(value).model_validate(payload)  # type: ignore[attr-defined,no-any-return]


def _request(
    *,
    client_request_id: str = "123e4567-e89b-42d3-a456-426614174000",
    repository_path: str = "/srv/source/repository",
) -> PreflightRequest:
    return PreflightRequest(
        client_request_id=client_request_id,
        repository_path=repository_path,
        source_execution_target=AuditPreflightSourceExecutionTarget(
            source_ingest_backend="linux_container"
        ),
        target=AuditPreflightTarget(
            kind=SourceTargetKind.WORKING_TREE,
            revision="HEAD",
        ),
        include_paths=("src",),
        exclude_paths=("vendor",),
        mode=AuditMode.STANDARD,
    )


def _pending_job(
    *,
    job_id: str = "preflight-job-1",
    request: PreflightRequest | None = None,
    operator_principal_id: str = "operator-1",
    authorization_scope_digest: str | None = None,
    created_at: datetime = NOW,
    expires_at: datetime | None = None,
) -> AuditPreflightJob:
    request = request or _request()
    return AuditPreflightJob(
        job_id=job_id,
        client_request_id=request.client_request_id,
        operator_principal_id=operator_principal_id,
        authorization_scope_digest=(authorization_scope_digest or _digest("authorization")),
        request_digest=request.request_digest,
        restricted_request_json=request.canonical_json(),
        source_root_identity_digest=_digest("source-root"),
        backend_id=request.source_execution_target.source_ingest_backend,
        image_digest=_digest("image"),
        policy_digest=_digest("policy"),
        expires_at=expires_at or created_at + timedelta(hours=2),
        created_at=created_at,
        updated_at=created_at,
    )


def _capability_matrix() -> AuditPreflightCapabilityMatrix:
    return AuditPreflightCapabilityMatrix(
        entries=(
            AuditPreflightCapabilityFact(
                capability_id="detector_inventory",
                status=AuditPreflightCapabilityStatus.UNAVAILABLE,
                reason_code="audit_inventory_unavailable",
            ),
            AuditPreflightCapabilityFact(
                capability_id="source_ingest",
                status=AuditPreflightCapabilityStatus.AVAILABLE,
                component_version="v1",
                component_digest=_digest("source-ingest-component"),
                proof_digest=_digest("source-ingest-proof"),
            ),
        )
    )


def _result(job: AuditPreflightJob, *, completed_at: datetime) -> AuditPreflightResult:
    return AuditPreflightResult(
        preflight_job_id=job.job_id,
        request_digest=job.request_digest,
        effect_owner_digest=job.effect_owner_digest,
        source_root_identity_digest=job.source_root_identity_digest,
        repository_identity_digest=_digest("repository"),
        content_identity_digest=_digest("content"),
        backend_id=job.backend_id,
        image_digest=job.image_digest,
        policy_digest=job.policy_digest,
        capsule_prepare_proof_digest=job.capsule_prepare_proof_digest or _digest("prepare"),
        target_kind=SourceTargetKind.WORKING_TREE,
        revision="HEAD",
        mode=AuditMode.STANDARD,
        include_untracked=False,
        head_revision=REVISION,
        resolved_revision=REVISION,
        dirty=False,
        staged=False,
        unstaged=False,
        untracked=False,
        file_count=3,
        total_bytes=300,
        max_file_bytes=150,
        language_estimates=(
            AuditPreflightLanguageEstimate(
                language_id="python",
                file_count=2,
                total_bytes=240,
            ),
        ),
        capability_matrix=_capability_matrix(),
        minimum_feasible_budget=AuditPreflightMinimumFeasibleBudget(
            status=AuditPreflightBudgetStatus.UNAVAILABLE,
            provenance_digest=_digest("budget"),
            reason_code="audit_inventory_unavailable",
        ),
        completed_at=completed_at,
        expires_at=completed_at + timedelta(minutes=20),
    )


async def _database_and_repository(
    tmp_path: Path,
) -> tuple[Database, SQLAlchemyAuditPreflightRepository]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'preflight.db'}")
    await database.create_schema()
    return database, SQLAlchemyAuditPreflightRepository(database.session_factory)


async def _claim(
    repository: SQLAlchemyAuditPreflightRepository,
    *,
    runner_instance_id: str = "runner-1",
    now: datetime = NOW + timedelta(minutes=1),
    lease_seconds: int = 300,
):
    return await repository.claim_next(
        node_id="local",
        runner_instance_id=runner_instance_id,
        runner_epoch=7,
        now=now,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        output_contract_digest=_digest("output-contract"),
    )


async def _running_job(
    repository: SQLAlchemyAuditPreflightRepository,
    pending: AuditPreflightJob,
) -> AuditPreflightJob:
    await repository.create(pending)
    dispatch = await _claim(repository)
    assert dispatch is not None
    claimed = dispatch.job
    started_at = NOW + timedelta(minutes=2)
    running = _replace(
        claimed,
        status=AuditPreflightJobStatus.RUNNING,
        state_version=claimed.state_version + 1,
        capsule_prepare_proof_digest=_digest("prepare"),
        started_at=started_at,
        updated_at=started_at,
    )
    return await repository.compare_and_set(previous=claimed, updated=running)


def _successful_finish(
    running: AuditPreflightJob,
) -> tuple[AuditPreflightJob, AuditPreflightResult, AuditPreflightExitReceipt]:
    completed_at = NOW + timedelta(minutes=3)
    result = _result(running, completed_at=completed_at)
    assert running.lease_envelope_digest is not None
    assert running.capsule_id is not None
    assert running.lease_owner_instance_id is not None
    assert running.lease_owner_epoch is not None
    receipt = AuditPreflightExitReceipt(
        job_id=running.job_id,
        effect_owner_digest=running.effect_owner_digest,
        lease_envelope_digest=running.lease_envelope_digest,
        capsule_id=running.capsule_id,
        runner_principal=RunnerPrincipal(
            instance_id=running.lease_owner_instance_id,
            epoch=running.lease_owner_epoch,
        ),
        backend_id=running.backend_id,
        image_digest=running.image_digest,
        policy_digest=running.policy_digest,
        process_identity_digest=_digest("process"),
        result_digest=result.result_digest,
        terminal_state=AuditPreflightExitTerminalState.SUCCEEDED,
        received_at=completed_at,
    )
    succeeded = _replace(
        running,
        status=AuditPreflightJobStatus.SUCCEEDED,
        state_version=running.state_version + 1,
        result_schema_version=result.schema_version,
        result_json=result.canonical_json(),
        result_digest=result.result_digest,
        exit_receipt_digest=receipt.receipt_digest,
        updated_at=completed_at,
        finished_at=completed_at,
    )
    return succeeded, result, receipt


async def test_create_is_exactly_idempotent_and_rejects_identity_drift(
    tmp_path: Path,
) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        first = _pending_job(job_id="preflight-first")
        replay = _pending_job(job_id="preflight-retry", created_at=NOW + timedelta(seconds=1))

        assert (
            await repository.get_idempotency_binding(
                operator_principal_id=first.operator_principal_id,
                client_request_id=first.client_request_id,
            )
            is None
        )

        created_job, created = await repository.create(first)
        replayed_job, replayed = await repository.create(replay)

        assert created is True
        assert replayed is False
        assert created_job == first
        assert replayed_job == first
        binding = await repository.get_idempotency_binding(
            operator_principal_id=first.operator_principal_id,
            client_request_id=first.client_request_id,
        )
        assert binding is not None
        assert binding.job_id == first.job_id
        assert binding.request_digest == first.request_digest

        drift = _pending_job(
            job_id="preflight-drift",
            authorization_scope_digest=_digest("other-authorization"),
        )
        with pytest.raises(RepositoryConflictError):
            await repository.create(drift)

        async with database.session() as session:
            assert (
                await session.scalar(select(func.count()).select_from(AuditPreflightJobRecord)) == 1
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(AuditPreflightJobRequestRecord)
                )
                == 1
            )
    finally:
        await database.dispose()


async def test_concurrent_create_has_one_durable_owner(tmp_path: Path) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        first = _pending_job(job_id="preflight-concurrent-a")
        second = _pending_job(job_id="preflight-concurrent-b")

        outcomes = await asyncio.gather(
            repository.create(first),
            repository.create(second),
        )

        assert sorted(created for _, created in outcomes) == [False, True]
        assert len({job.job_id for job, _ in outcomes}) == 1
    finally:
        await database.dispose()


async def test_claim_preallocates_capsule_and_never_blindly_reclaims_expired_lease(
    tmp_path: Path,
) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        first = _pending_job(job_id="preflight-first")
        await repository.create(first)
        first_dispatch = await _claim(repository, lease_seconds=30)
        assert first_dispatch is not None
        assert first_dispatch.job.status is AuditPreflightJobStatus.CLAIMED
        assert first_dispatch.job.state_version == 2
        assert first_dispatch.job.attempt == 1
        assert first_dispatch.job.capsule_id is not None
        assert first_dispatch.request == _request()

        second_request = _request(
            client_request_id="223e4567-e89b-42d3-a456-426614174000",
            repository_path="/srv/source/second",
        )
        second = _pending_job(
            job_id="preflight-second",
            request=second_request,
            created_at=NOW + timedelta(seconds=1),
        )
        await repository.create(second)
        after_expiry = NOW + timedelta(minutes=2)
        second_dispatch = await _claim(repository, now=after_expiry)

        assert second_dispatch is not None
        assert second_dispatch.job.job_id == second.job_id
        assert await _claim(repository, now=after_expiry + timedelta(minutes=1)) is None
        persisted_first = await repository.get(first.job_id)
        assert persisted_first == first_dispatch.job
    finally:
        await database.dispose()


async def test_concurrent_claim_delivers_a_pending_job_once(tmp_path: Path) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        await repository.create(_pending_job())

        results = await asyncio.gather(
            _claim(repository, runner_instance_id="runner-a"),
            _claim(repository, runner_instance_id="runner-b"),
        )

        claimed = [result for result in results if result is not None]
        assert len(claimed) == 1
        assert claimed[0].job.attempt == 1
    finally:
        await database.dispose()


async def test_success_finish_persists_immutable_result_and_exact_terminal_replay(
    tmp_path: Path,
) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        running = await _running_job(repository, _pending_job())
        succeeded, result, receipt = _successful_finish(running)

        stored = await repository.compare_and_set(
            previous=running,
            updated=succeeded,
            result=result,
            exit_receipt=receipt,
        )
        assert stored == succeeded
        assert await repository.get(stored.job_id) == stored

        replay = await repository.compare_and_set(
            previous=stored,
            updated=stored,
            result=result,
            exit_receipt=receipt,
        )
        assert replay == stored

        drifted_receipt = _replace(
            receipt,
            receipt_digest="",
            process_identity_digest=_digest("different-process"),
        )
        with pytest.raises(ValueError, match="exit receipt"):
            await repository.compare_and_set(
                previous=stored,
                updated=stored,
                result=result,
                exit_receipt=drifted_receipt,
            )

        with pytest.raises(RepositoryConflictError):
            await repository.compare_and_set(
                previous=running,
                updated=succeeded,
                result=result,
                exit_receipt=receipt,
            )

        async with database.session() as session:
            assert (
                await session.scalar(select(func.count()).select_from(AuditPreflightResultRecord))
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(AuditPreflightExitReceiptRecord)
                )
                == 1
            )
    finally:
        await database.dispose()


async def test_cancel_fence_wins_stale_finish_and_converges_with_stop_receipt(
    tmp_path: Path,
) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        running = await _running_job(repository, _pending_job())
        succeeded, result, exit_receipt = _successful_finish(running)
        cancelling_at = NOW + timedelta(minutes=2, seconds=30)
        cancelling = _replace(
            running,
            status=AuditPreflightJobStatus.CANCELLING,
            state_version=running.state_version + 1,
            updated_at=cancelling_at,
        )
        cancelling = await repository.compare_and_set(
            previous=running,
            updated=cancelling,
        )

        with pytest.raises(RepositoryConflictError):
            await repository.compare_and_set(
                previous=running,
                updated=succeeded,
                result=result,
                exit_receipt=exit_receipt,
            )

        assert cancelling.lease_envelope_digest is not None
        assert cancelling.capsule_id is not None
        assert cancelling.lease_owner_instance_id is not None
        assert cancelling.lease_owner_epoch is not None
        stopped_at = NOW + timedelta(minutes=4)
        stop_receipt = AuditPreflightStopReceipt(
            job_id=cancelling.job_id,
            effect_owner_digest=cancelling.effect_owner_digest,
            lease_envelope_digest=cancelling.lease_envelope_digest,
            capsule_id=cancelling.capsule_id,
            runner_principal=RunnerPrincipal(
                instance_id=cancelling.lease_owner_instance_id,
                epoch=cancelling.lease_owner_epoch,
            ),
            backend_id=cancelling.backend_id,
            image_digest=cancelling.image_digest,
            policy_digest=cancelling.policy_digest,
            disposition=AuditPreflightStopDisposition.STOPPED,
            process_identity_digest=_digest("stopped-process"),
            observed_terminal_state=AuditPreflightObservedTerminalState.CANCELLED,
            received_at=stopped_at,
        )
        cancelled = _replace(
            cancelling,
            status=AuditPreflightJobStatus.CANCELLED,
            state_version=cancelling.state_version + 1,
            stop_receipt_digest=stop_receipt.receipt_digest,
            updated_at=stopped_at,
            finished_at=stopped_at,
        )
        cancelled = await repository.compare_and_set(
            previous=cancelling,
            updated=cancelled,
            stop_receipt=stop_receipt,
        )

        assert cancelled.status is AuditPreflightJobStatus.CANCELLED
        assert await repository.get(cancelled.job_id) == cancelled
        async with database.session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(AuditPreflightStopReceiptRecord)
                )
                == 1
            )
    finally:
        await database.dispose()


async def test_receipt_owner_drift_rolls_back_terminal_mutation(tmp_path: Path) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        running = await _running_job(repository, _pending_job())
        succeeded, result, receipt = _successful_finish(running)
        forged_receipt = _replace(
            receipt,
            receipt_digest="",
            runner_principal=RunnerPrincipal(instance_id="runner-forged", epoch=7),
        )
        forged_job = _replace(
            succeeded,
            exit_receipt_digest=forged_receipt.receipt_digest,
        )

        with pytest.raises(ValueError, match="exit receipt"):
            await repository.compare_and_set(
                previous=running,
                updated=forged_job,
                result=result,
                exit_receipt=forged_receipt,
            )

        assert await repository.get(running.job_id) == running
    finally:
        await database.dispose()


async def test_compare_and_set_rejects_attempt_and_lease_identity_mutation(
    tmp_path: Path,
) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        await repository.create(_pending_job())
        dispatch = await _claim(repository)
        assert dispatch is not None
        claimed = dispatch.job
        changed_at = NOW + timedelta(minutes=2)

        attempt_drift = _replace(
            claimed,
            state_version=claimed.state_version + 1,
            attempt=claimed.attempt + 1,
            updated_at=changed_at,
        )
        with pytest.raises(ValueError, match="attempt"):
            await repository.compare_and_set(
                previous=claimed,
                updated=attempt_drift,
            )

        lease_drift = _replace(
            claimed,
            state_version=claimed.state_version + 1,
            capsule_id="different-capsule",
            updated_at=changed_at,
        )
        with pytest.raises(ValueError, match="lease and capsule identity"):
            await repository.compare_and_set(
                previous=claimed,
                updated=lease_drift,
            )
    finally:
        await database.dispose()


@pytest.mark.parametrize(
    ("statement", "reason_code"),
    [
        (
            "UPDATE audit_preflight_job_requests SET canonical_json = "
            "replace(canonical_json, ':', ': ')",
            "restricted_request_not_canonical",
        ),
        (
            "UPDATE audit_preflight_jobs SET effect_owner_digest = "
            "'0000000000000000000000000000000000000000000000000000000000000000'",
            "invalid_persisted_state",
        ),
    ],
)
async def test_corrupt_owner_or_restricted_request_fails_closed(
    tmp_path: Path,
    statement: str,
    reason_code: str,
) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        job = _pending_job()
        await repository.create(job)
        async with database.session() as session, session.begin():
            await session.execute(text(statement))

        with pytest.raises(RepositoryIntegrityError) as raised:
            await repository.get(job.job_id)
        assert raised.value.reason_code == reason_code
    finally:
        await database.dispose()


async def test_reconciliation_scan_is_bounded_and_deterministic(tmp_path: Path) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        jobs = []
        for index in range(3):
            request = _request(
                client_request_id=f"{index + 1:08x}-e89b-42d3-a456-426614174000",
                repository_path=f"/srv/source/repository-{index}",
            )
            job = _pending_job(
                job_id=f"preflight-{index}",
                request=request,
                created_at=NOW - timedelta(hours=3) + timedelta(seconds=index),
            )
            jobs.append(job)
            await repository.create(job)

        selected = await repository.list_reconciliation_candidates(
            observed_at=NOW,
            limit=2,
        )

        assert [job.job_id for job in selected] == [jobs[0].job_id, jobs[1].job_id]
        with pytest.raises(ValueError, match="limit"):
            await repository.list_reconciliation_candidates(
                observed_at=NOW,
                limit=0,
            )
    finally:
        await database.dispose()


async def test_reconciliation_scan_excludes_non_actionable_rows_before_limit(
    tmp_path: Path,
) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        for index in range(3):
            request = _request(
                client_request_id=f"{index + 1:08x}-e89b-42d3-a456-426614174100",
                repository_path=f"/srv/source/not-expired-{index}",
            )
            non_actionable = _pending_job(
                job_id=f"not-expired-{index}",
                request=request,
                created_at=NOW - timedelta(hours=8) + timedelta(seconds=index),
                expires_at=NOW + timedelta(hours=1),
            )
            await repository.create(non_actionable)

        expired_request = _request(
            client_request_id="00000010-e89b-42d3-a456-426614174100",
            repository_path="/srv/source/expired",
        )
        expired = _pending_job(
            job_id="expired-actionable",
            request=expired_request,
            created_at=NOW - timedelta(hours=3),
        )
        await repository.create(expired)

        selected = await repository.list_reconciliation_candidates(
            observed_at=NOW,
            limit=1,
        )

        assert [candidate.job_id for candidate in selected] == [expired.job_id]
    finally:
        await database.dispose()


async def test_outcome_unknown_rows_cannot_starve_expiry_reconciliation(
    tmp_path: Path,
) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        unknown_request = _request(
            client_request_id="00000020-e89b-42d3-a456-426614174100",
            repository_path="/srv/source/outcome-unknown",
        )
        await repository.create(
            _pending_job(
                job_id="outcome-unknown",
                request=unknown_request,
                created_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(hours=1),
            )
        )
        dispatch = await _claim(repository, now=NOW, lease_seconds=30)
        assert dispatch is not None
        claimed = dispatch.job
        assert claimed.lease_expires_at is not None
        unknown = _replace(
            claimed,
            status=AuditPreflightJobStatus.OUTCOME_UNKNOWN,
            state_version=claimed.state_version + 1,
            updated_at=claimed.lease_expires_at,
        )
        await repository.compare_and_set(previous=claimed, updated=unknown)

        expired_request = _request(
            client_request_id="00000021-e89b-42d3-a456-426614174100",
            repository_path="/srv/source/expired-after-unknown",
        )
        expired = _pending_job(
            job_id="expired-after-unknown",
            request=expired_request,
            created_at=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=2),
        )
        await repository.create(expired)

        selected = await repository.list_reconciliation_candidates(
            observed_at=NOW + timedelta(hours=1),
            limit=1,
        )

        assert [candidate.job_id for candidate in selected] == [expired.job_id]
    finally:
        await database.dispose()


async def test_reconciliation_projection_does_not_read_restricted_request(
    tmp_path: Path,
) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        expired = _pending_job(created_at=NOW - timedelta(hours=3))
        await repository.create(expired)
        async with database.session() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE audit_preflight_job_requests "
                    "SET canonical_json = '{not-json' WHERE job_id = :job_id"
                ),
                {"job_id": expired.job_id},
            )

        selected = await repository.list_reconciliation_candidates(
            observed_at=NOW,
            limit=1,
        )

        assert [candidate.job_id for candidate in selected] == [expired.job_id]
        with pytest.raises(RepositoryIntegrityError):
            await repository.get(expired.job_id)
    finally:
        await database.dispose()


async def test_reconciliation_compare_and_set_has_one_projection_winner(
    tmp_path: Path,
) -> None:
    database, repository = await _database_and_repository(tmp_path)
    try:
        expired = _pending_job(created_at=NOW - timedelta(hours=3))
        await repository.create(expired)
        candidates = await repository.list_reconciliation_candidates(
            observed_at=NOW,
            limit=1,
        )
        assert len(candidates) == 1
        candidate = candidates[0]

        results = await asyncio.gather(
            *(
                repository.compare_and_set_reconciliation(
                    previous=candidate,
                    status=AuditPreflightJobStatus.CANCELLED,
                    observed_at=NOW,
                    never_created_proof_digest=_digest("reconciler-proof"),
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )

        winners = [result for result in results if not isinstance(result, BaseException)]
        conflicts = [result for result in results if isinstance(result, RepositoryConflictError)]
        assert len(winners) == len(conflicts) == 1
        persisted = await repository.get_reconciliation_candidate(expired.job_id)
        assert persisted is not None
        assert persisted.status is AuditPreflightJobStatus.CANCELLED
        assert persisted.state_version == candidate.state_version + 1
    finally:
        await database.dispose()
