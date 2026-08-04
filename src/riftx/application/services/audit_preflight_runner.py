"""Dedicated authenticated Runner edge for non-Run Audit Preflight jobs.

The service deliberately shares neither the general Runner command repository nor
its Run-scoped ownership model.  Callback admission is staged so an invalid node,
principal, owner, or lease is rejected before restricted request data or mutable
job state is loaded.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from riftx.application.errors import (
    ApplicationConflictError,
    AuthenticationError,
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
    ServiceUnavailableError,
)
from riftx.application.ports.audit_preflight import (
    AuditPreflightDispatch,
    AuditPreflightOwnerBinding,
    AuditPreflightReconciliationCandidate,
    AuditPreflightRepository,
)
from riftx.application.ports.repositories import RunnerCredentialRepository
from riftx.domain.audit_preflight import (
    AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,
    MAX_PREFLIGHT_COUNTER,
    AuditPreflightEffectOwner,
    AuditPreflightExitReceipt,
    AuditPreflightJob,
    AuditPreflightJobStatus,
    AuditPreflightLeaseEnvelope,
    AuditPreflightResult,
    AuditPreflightStopDisposition,
    AuditPreflightStopReceipt,
)
from riftx.domain.audit_preflight_wire import (
    AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
    AuditPreflightCallbackAck,
    AuditPreflightDispatchEnvelope,
    AuditPreflightLeaseGrant,
    AuditPreflightStartGrant,
)
from riftx.domain.base import utc_now
from riftx.domain.runner import RunnerCredential, RunnerPrincipal

_CALLBACK_TERMINAL_STATUSES = frozenset(
    {
        AuditPreflightJobStatus.SUCCEEDED,
        AuditPreflightJobStatus.REJECTED,
        AuditPreflightJobStatus.FAILED,
        AuditPreflightJobStatus.CANCELLED,
    }
)
_FINISH_STATUSES = frozenset(
    {
        AuditPreflightJobStatus.SUCCEEDED,
        AuditPreflightJobStatus.REJECTED,
        AuditPreflightJobStatus.FAILED,
    }
)
_STOP_STATUSES = frozenset(
    {
        AuditPreflightJobStatus.CANCELLED,
        AuditPreflightJobStatus.FAILED,
    }
)
_RECONCILER_NEVER_CREATED_PROOF_VERSION = "riftx.audit-preflight-reconciler-never-created-proof/v1"


class AuditPreflightRunnerService:
    """Authenticate and mutate only the dedicated ``preflight_job`` owner."""

    def __init__(
        self,
        *,
        repository: AuditPreflightRepository,
        credentials: RunnerCredentialRepository,
        lease_duration: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if lease_duration <= timedelta(0) or lease_duration > timedelta(minutes=5):
            raise ValueError(
                "Audit Preflight lease_duration must be positive and at most five minutes"
            )
        self._repository = repository
        self._credentials = credentials
        self._lease_duration = lease_duration
        self._clock = clock

    async def authenticate(
        self,
        node_id: str,
        token: str,
        *,
        declared_principal: RunnerPrincipal,
    ) -> RunnerCredential:
        """Authenticate the immutable credential before capability/owner checks."""

        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        credential = await self._credentials.get_by_token_hash(node_id, token_hash)
        if (
            credential is None
            or credential.revoked_at is not None
            or not secrets.compare_digest(credential.node_id, node_id)
            or not secrets.compare_digest(credential.token_hash, token_hash)
        ):
            raise _authentication_failed()
        _require_protocol_capability(credential.protocol_capabilities)
        if credential.principal != declared_principal:
            raise _authentication_failed()
        return credential

    async def poll(
        self,
        *,
        node_id: str,
        principal: RunnerPrincipal,
        protocol_capabilities: Sequence[str],
        wait_seconds: float = 0,
    ) -> AuditPreflightDispatchEnvelope | None:
        """Claim one pending job through the Preflight-specific repository path."""

        _require_protocol_capability(protocol_capabilities)
        _require_local_node(node_id)
        deadline = asyncio.get_running_loop().time() + min(max(wait_seconds, 0), 30)
        while True:
            now = self._clock()
            try:
                claimed = await self._repository.get_replayable_claim(
                    node_id=node_id,
                    runner_instance_id=principal.instance_id,
                    runner_epoch=principal.epoch,
                    now=now,
                )
                if claimed is not None:
                    return _dispatch_envelope(claimed, principal=principal)
                claimed = await self._repository.claim_next(
                    node_id=node_id,
                    runner_instance_id=principal.instance_id,
                    runner_epoch=principal.epoch,
                    now=now,
                    lease_expires_at=now + self._lease_duration,
                    output_contract_digest=AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
                )
            except (RepositoryIntegrityError, RepositoryUnavailableError):
                raise _persistence_unavailable() from None
            if claimed is not None:
                return _dispatch_envelope(claimed, principal=principal)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.25, remaining))

    async def renew_lease(
        self,
        job_id: str,
        *,
        node_id: str,
        principal: RunnerPrincipal,
        protocol_capabilities: Sequence[str],
        owner: AuditPreflightEffectOwner,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        capsule_id: str,
    ) -> AuditPreflightLeaseGrant:
        """Renew exactly one current lease, accepting only its immediate replay."""

        for attempt in range(2):
            current = await self._load_callback_job(
                job_id,
                node_id=node_id,
                principal=principal,
                protocol_capabilities=protocol_capabilities,
                owner=owner,
                lease=lease,
                capsule_id=capsule_id,
            )
            now = max(self._clock(), current.updated_at)
            if _is_renew_replay(current, lease=lease, state_version=state_version):
                return _lease_grant(current, now=now)
            allowed_statuses = (
                AuditPreflightJobStatus.CLAIMED,
                AuditPreflightJobStatus.RUNNING,
                AuditPreflightJobStatus.CANCELLING,
            )
            _require_callback_state(
                current,
                lease=lease,
                state_version=state_version,
                allowed_statuses=allowed_statuses,
                require_unexpired=(current.status is not AuditPreflightJobStatus.CANCELLING),
                now=now,
            )
            if current.status is AuditPreflightJobStatus.CANCELLING and current.expires_at <= now:
                raise _lease_conflict()
            assert current.lease_expires_at is not None
            next_state_version = current.state_version + 1
            lease_expires_at = max(
                current.lease_expires_at,
                min(now + self._lease_duration, current.expires_at),
            )
            if lease_expires_at <= now:
                raise _lease_conflict()
            renewed = AuditPreflightLeaseEnvelope(
                owner=current.effect_owner(),
                runner_principal=principal,
                lease_id=lease.lease_id,
                lease_expires_at=lease_expires_at,
                expected_state_version=next_state_version,
                output_contract_digest=lease.output_contract_digest,
            )
            updated = _replace_job(
                current,
                state_version=next_state_version,
                lease_expires_at=lease_expires_at,
                lease_expected_state_version=next_state_version,
                lease_envelope_digest=renewed.lease_envelope_digest,
                updated_at=now,
            )
            try:
                persisted = await self._repository.compare_and_set(
                    previous=current,
                    updated=updated,
                )
            except RepositoryConflictError:
                if attempt == 0:
                    continue
                raise _state_conflict() from None
            except (RepositoryIntegrityError, RepositoryUnavailableError):
                raise _persistence_unavailable() from None
            return _lease_grant(persisted, now=now)
        raise _state_conflict()  # pragma: no cover - bounded loop is exhaustive

    async def start(
        self,
        job_id: str,
        *,
        node_id: str,
        principal: RunnerPrincipal,
        protocol_capabilities: Sequence[str],
        owner: AuditPreflightEffectOwner,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        capsule_id: str,
        capsule_prepare_proof_digest: str,
    ) -> AuditPreflightStartGrant:
        """Persist capsule preparation before granting execution start."""

        for attempt in range(2):
            current = await self._load_callback_job(
                job_id,
                node_id=node_id,
                principal=principal,
                protocol_capabilities=protocol_capabilities,
                owner=owner,
                lease=lease,
                capsule_id=capsule_id,
            )
            if _is_start_replay(
                current,
                lease=lease,
                state_version=state_version,
                capsule_prepare_proof_digest=capsule_prepare_proof_digest,
            ):
                return _start_grant(current)
            now = max(self._clock(), current.updated_at)
            _require_callback_state(
                current,
                lease=lease,
                state_version=state_version,
                allowed_statuses=(AuditPreflightJobStatus.CLAIMED,),
                require_unexpired=True,
                now=now,
            )
            current.validate_transition_to(AuditPreflightJobStatus.RUNNING)
            updated = _replace_job(
                current,
                status=AuditPreflightJobStatus.RUNNING,
                state_version=current.state_version + 1,
                capsule_prepare_proof_digest=capsule_prepare_proof_digest,
                started_at=now,
                updated_at=now,
            )
            try:
                persisted = await self._repository.compare_and_set(
                    previous=current,
                    updated=updated,
                )
            except RepositoryConflictError:
                if attempt == 0:
                    continue
                raise _state_conflict() from None
            except (RepositoryIntegrityError, RepositoryUnavailableError):
                raise _persistence_unavailable() from None
            return _start_grant(persisted)
        raise _state_conflict()  # pragma: no cover

    async def finish(
        self,
        job_id: str,
        *,
        node_id: str,
        principal: RunnerPrincipal,
        protocol_capabilities: Sequence[str],
        owner: AuditPreflightEffectOwner,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        capsule_id: str,
        status: AuditPreflightJobStatus,
        result: AuditPreflightResult | None,
        safe_error_code: str | None,
        exit_receipt: AuditPreflightExitReceipt,
    ) -> AuditPreflightCallbackAck:
        """Persist a terminal result and its immutable exit receipt atomically."""

        current = await self._load_callback_job(
            job_id,
            node_id=node_id,
            principal=principal,
            protocol_capabilities=protocol_capabilities,
            owner=owner,
            lease=lease,
            capsule_id=capsule_id,
        )
        return await self._finish_loaded(
            current,
            lease=lease,
            state_version=state_version,
            status=status,
            result=result,
            safe_error_code=safe_error_code,
            exit_receipt=exit_receipt,
            retry_owner=(node_id, principal, protocol_capabilities, owner, capsule_id),
        )

    async def stop(
        self,
        job_id: str,
        *,
        node_id: str,
        principal: RunnerPrincipal,
        protocol_capabilities: Sequence[str],
        owner: AuditPreflightEffectOwner,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        capsule_id: str,
        status: AuditPreflightJobStatus,
        safe_error_code: str | None,
        stop_receipt: AuditPreflightStopReceipt,
    ) -> AuditPreflightCallbackAck:
        """Persist affirmative stop proof and converge the fenced Job."""

        current = await self._load_callback_job(
            job_id,
            node_id=node_id,
            principal=principal,
            protocol_capabilities=protocol_capabilities,
            owner=owner,
            lease=lease,
            capsule_id=capsule_id,
        )
        return await self._stop_loaded(
            current,
            lease=lease,
            state_version=state_version,
            status=status,
            safe_error_code=safe_error_code,
            stop_receipt=stop_receipt,
            retry_owner=(node_id, principal, protocol_capabilities, owner, capsule_id),
        )

    async def reconcile_batch(self, *, limit: int = 100) -> int:
        """Converge only expiry facts that are safe to prove from durable state.

        ``outcome_unknown`` jobs are deliberately excluded because there is no
        expiry-only transition that can safely advance them. A Runner holding a
        journal-bound exit/stop receipt must replay that proof through the
        dedicated callback path.
        """

        observed_at = self._clock()
        try:
            candidates = await self._repository.list_reconciliation_candidates(
                observed_at=observed_at,
                limit=limit,
            )
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _persistence_unavailable() from None

        changed = 0
        for candidate in candidates:
            before = (candidate.status, candidate.state_version)
            reconciled = await self._reconcile_expired_candidate(
                candidate,
                observed_at=observed_at,
            )
            if (reconciled.status, reconciled.state_version) != before:
                changed += 1
        return changed

    async def mark_expired_outcome_unknown(
        self,
        job_id: str,
        *,
        observed_at: datetime,
    ) -> AuditPreflightReconciliationCandidate:
        """Reconciler seam: fence expired active work without blind redispatch."""

        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        current = await self._load_reconciliation_candidate(job_id)
        if current.status not in {
            AuditPreflightJobStatus.CLAIMED,
            AuditPreflightJobStatus.RUNNING,
            AuditPreflightJobStatus.CANCELLING,
        }:
            return current
        return await self._reconcile_expired_candidate(
            current,
            observed_at=observed_at,
        )

    async def expire_pending_never_created(
        self,
        job_id: str,
        *,
        observed_at: datetime,
    ) -> AuditPreflightReconciliationCandidate:
        """Reconciler seam: terminalize an expired pending Job with DB-only proof."""

        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        current = await self._load_reconciliation_candidate(job_id)
        if current.status is not AuditPreflightJobStatus.PENDING:
            return current
        return await self._reconcile_expired_candidate(
            current,
            observed_at=observed_at,
        )

    async def _reconcile_expired_candidate(
        self,
        candidate: AuditPreflightReconciliationCandidate,
        *,
        observed_at: datetime,
    ) -> AuditPreflightReconciliationCandidate:
        current = candidate
        for _ in range(8):
            if current.state_version >= MAX_PREFLIGHT_COUNTER:
                raise _state_conflict()
            if current.status is AuditPreflightJobStatus.PENDING:
                if current.expires_at > observed_at:
                    return current
                status = AuditPreflightJobStatus.CANCELLED
                changed_at = max(observed_at, current.updated_at)
                never_created_proof_digest = _expired_pending_proof_digest(
                    current,
                    observed_at=changed_at,
                )
            elif current.status in {
                AuditPreflightJobStatus.CLAIMED,
                AuditPreflightJobStatus.RUNNING,
                AuditPreflightJobStatus.CANCELLING,
            }:
                if (
                    current.lease_expires_at is None
                    or current.lease_expires_at > observed_at
                    or current.stop_receipt_digest is not None
                    or current.never_created_proof_digest is not None
                ):
                    return current
                status = AuditPreflightJobStatus.OUTCOME_UNKNOWN
                never_created_proof_digest = None
            else:
                return current
            try:
                return await self._repository.compare_and_set_reconciliation(
                    previous=current,
                    status=status,
                    observed_at=observed_at,
                    never_created_proof_digest=never_created_proof_digest,
                )
            except RepositoryConflictError:
                current = await self._load_reconciliation_candidate(current.job_id)
                continue
            except (RepositoryIntegrityError, RepositoryUnavailableError):
                raise _persistence_unavailable() from None
        raise _state_conflict()

    async def converge_finish_receipt(
        self,
        job_id: str,
        *,
        state_version: int,
        status: AuditPreflightJobStatus,
        result: AuditPreflightResult | None,
        safe_error_code: str | None,
        exit_receipt: AuditPreflightExitReceipt,
    ) -> AuditPreflightCallbackAck:
        """Reconciler seam for a journal-proven exit receipt after callback loss."""

        current = await self._load_internal_job(job_id)
        lease = current.lease_envelope()
        if lease is None:
            raise _lease_conflict()
        _require_receipt_owner(current, exit_receipt=exit_receipt)
        return await self._finish_loaded(
            current,
            lease=lease,
            state_version=state_version,
            status=status,
            result=result,
            safe_error_code=safe_error_code,
            exit_receipt=exit_receipt,
            retry_owner=None,
        )

    async def converge_stop_receipt(
        self,
        job_id: str,
        *,
        state_version: int,
        status: AuditPreflightJobStatus,
        safe_error_code: str | None,
        stop_receipt: AuditPreflightStopReceipt,
    ) -> AuditPreflightCallbackAck:
        """Reconciler seam for saved stop proof and cancelling convergence."""

        current = await self._load_internal_job(job_id)
        lease = current.lease_envelope()
        if lease is None:
            raise _lease_conflict()
        _require_receipt_owner(current, stop_receipt=stop_receipt)
        return await self._stop_loaded(
            current,
            lease=lease,
            state_version=state_version,
            status=status,
            safe_error_code=safe_error_code,
            stop_receipt=stop_receipt,
            retry_owner=None,
        )

    async def _finish_loaded(
        self,
        current: AuditPreflightJob,
        *,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        status: AuditPreflightJobStatus,
        result: AuditPreflightResult | None,
        safe_error_code: str | None,
        exit_receipt: AuditPreflightExitReceipt,
        retry_owner: tuple[
            str,
            RunnerPrincipal,
            Sequence[str],
            AuditPreflightEffectOwner,
            str,
        ]
        | None,
    ) -> AuditPreflightCallbackAck:
        if status not in _FINISH_STATUSES:
            raise _state_conflict()
        for attempt in range(2):
            if current.status in _CALLBACK_TERMINAL_STATUSES:
                return await self._exact_finish_replay(
                    current,
                    lease=lease,
                    state_version=state_version,
                    status=status,
                    result=result,
                    safe_error_code=safe_error_code,
                    exit_receipt=exit_receipt,
                )
            if current.status is AuditPreflightJobStatus.CANCELLING:
                if current.lease_envelope() != lease:
                    raise _lease_conflict()
                raise _cancel_requested()
            _require_callback_state(
                current,
                lease=lease,
                state_version=state_version,
                allowed_statuses=(
                    AuditPreflightJobStatus.RUNNING,
                    AuditPreflightJobStatus.OUTCOME_UNKNOWN,
                ),
                allow_outcome_unknown_recovery=True,
            )
            _require_receipt_owner(current, exit_receipt=exit_receipt)
            _require_finish_shape(
                status=status,
                result=result,
                safe_error_code=safe_error_code,
                exit_receipt=exit_receipt,
            )
            finished_at = result.completed_at if result is not None else exit_receipt.received_at
            updated_at = max(
                self._clock(),
                current.updated_at,
                finished_at,
                exit_receipt.received_at,
            )
            updated = _replace_job(
                current,
                status=status,
                state_version=current.state_version + 1,
                result_schema_version=(result.schema_version if result is not None else None),
                result_json=(result.canonical_json() if result is not None else None),
                result_digest=(result.result_digest if result is not None else None),
                safe_error_code=safe_error_code,
                exit_receipt_digest=exit_receipt.receipt_digest,
                finished_at=finished_at,
                updated_at=updated_at,
            )
            try:
                persisted = await self._repository.compare_and_set(
                    previous=current,
                    updated=updated,
                    result=result,
                    exit_receipt=exit_receipt,
                )
            except RepositoryConflictError:
                if attempt == 0:
                    current = await self._reload_after_callback_conflict(
                        current.job_id,
                        retry_owner=retry_owner,
                        lease=lease,
                    )
                    continue
                raise _state_conflict() from None
            except (RepositoryIntegrityError, RepositoryUnavailableError):
                raise _persistence_unavailable() from None
            return _callback_ack(persisted)
        raise _state_conflict()  # pragma: no cover

    async def _stop_loaded(
        self,
        current: AuditPreflightJob,
        *,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        status: AuditPreflightJobStatus,
        safe_error_code: str | None,
        stop_receipt: AuditPreflightStopReceipt,
        retry_owner: tuple[
            str,
            RunnerPrincipal,
            Sequence[str],
            AuditPreflightEffectOwner,
            str,
        ]
        | None,
    ) -> AuditPreflightCallbackAck:
        if status not in _STOP_STATUSES:
            raise _state_conflict()
        for attempt in range(3):
            if current.status in _CALLBACK_TERMINAL_STATUSES:
                return await self._exact_stop_replay(
                    current,
                    lease=lease,
                    state_version=state_version,
                    status=status,
                    safe_error_code=safe_error_code,
                    stop_receipt=stop_receipt,
                )
            _require_callback_state(
                current,
                lease=lease,
                state_version=(current.state_version if attempt > 0 else state_version),
                allowed_statuses=(
                    AuditPreflightJobStatus.CLAIMED,
                    AuditPreflightJobStatus.RUNNING,
                    AuditPreflightJobStatus.CANCELLING,
                    AuditPreflightJobStatus.OUTCOME_UNKNOWN,
                ),
                allow_outcome_unknown_recovery=(attempt == 0),
            )
            _require_receipt_owner(current, stop_receipt=stop_receipt)
            _require_stop_shape(
                status=status,
                safe_error_code=safe_error_code,
                stop_receipt=stop_receipt,
            )
            if (
                status is AuditPreflightJobStatus.CANCELLED
                and current.status is AuditPreflightJobStatus.OUTCOME_UNKNOWN
            ):
                current.validate_transition_to(AuditPreflightJobStatus.CANCELLING)
                staged = _replace_job(
                    current,
                    status=AuditPreflightJobStatus.CANCELLING,
                    state_version=current.state_version + 1,
                    updated_at=max(self._clock(), current.updated_at),
                )
                try:
                    current = await self._repository.compare_and_set(
                        previous=current,
                        updated=staged,
                    )
                except RepositoryConflictError:
                    current = await self._reload_after_callback_conflict(
                        current.job_id,
                        retry_owner=retry_owner,
                        lease=lease,
                    )
                except (RepositoryIntegrityError, RepositoryUnavailableError):
                    raise _persistence_unavailable() from None
                continue
            if status is AuditPreflightJobStatus.CANCELLED and current.status not in {
                AuditPreflightJobStatus.CLAIMED,
                AuditPreflightJobStatus.CANCELLING,
            }:
                raise _state_conflict()
            if status is AuditPreflightJobStatus.FAILED and current.status not in {
                AuditPreflightJobStatus.RUNNING,
                AuditPreflightJobStatus.OUTCOME_UNKNOWN,
            }:
                raise _state_conflict()
            current.validate_transition_to(status)
            updated_at = max(self._clock(), current.updated_at, stop_receipt.received_at)
            updated = _replace_job(
                current,
                status=status,
                state_version=current.state_version + 1,
                safe_error_code=safe_error_code,
                never_created_proof_digest=(
                    stop_receipt.never_created_proof_digest
                    if stop_receipt.disposition is AuditPreflightStopDisposition.NEVER_CREATED
                    else None
                ),
                stop_receipt_digest=stop_receipt.receipt_digest,
                finished_at=stop_receipt.received_at,
                updated_at=updated_at,
            )
            try:
                persisted = await self._repository.compare_and_set(
                    previous=current,
                    updated=updated,
                    stop_receipt=stop_receipt,
                )
            except RepositoryConflictError:
                current = await self._reload_after_callback_conflict(
                    current.job_id,
                    retry_owner=retry_owner,
                    lease=lease,
                )
                continue
            except (RepositoryIntegrityError, RepositoryUnavailableError):
                raise _persistence_unavailable() from None
            return _callback_ack(persisted)
        raise _state_conflict()

    async def _exact_finish_replay(
        self,
        current: AuditPreflightJob,
        *,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        status: AuditPreflightJobStatus,
        result: AuditPreflightResult | None,
        safe_error_code: str | None,
        exit_receipt: AuditPreflightExitReceipt,
    ) -> AuditPreflightCallbackAck:
        if current.lease_envelope() != lease:
            raise _lease_conflict()
        recovery_replay = _is_terminal_recovery_replay(
            current,
            lease=lease,
            state_version=state_version,
            allowed_advances=(2,),
        )
        if (
            current.status is not status
            or (current.state_version != state_version + 1 and not recovery_replay)
            or current.lease_envelope() != lease
            or current.safe_error_code != safe_error_code
            or current.exit_receipt_digest != exit_receipt.receipt_digest
            or current.result_digest != (result.result_digest if result is not None else None)
            or current.result_json != (result.canonical_json() if result is not None else None)
        ):
            raise _state_conflict()
        try:
            persisted = await self._repository.compare_and_set(
                previous=current,
                updated=current,
                result=result,
                exit_receipt=exit_receipt,
            )
        except RepositoryConflictError:
            raise _state_conflict() from None
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _persistence_unavailable() from None
        return _callback_ack(persisted)

    async def _exact_stop_replay(
        self,
        current: AuditPreflightJob,
        *,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        status: AuditPreflightJobStatus,
        safe_error_code: str | None,
        stop_receipt: AuditPreflightStopReceipt,
    ) -> AuditPreflightCallbackAck:
        if current.lease_envelope() != lease:
            raise _lease_conflict()
        recovery_replay = _is_terminal_recovery_replay(
            current,
            lease=lease,
            state_version=state_version,
            allowed_advances=(2, 3),
        )
        direct_advances = {1, 2} if status is AuditPreflightJobStatus.CANCELLED else {1}
        if (
            current.status is not status
            or (
                current.state_version - state_version not in direct_advances and not recovery_replay
            )
            or current.lease_envelope() != lease
            or current.safe_error_code != safe_error_code
            or current.stop_receipt_digest != stop_receipt.receipt_digest
            or current.never_created_proof_digest
            != (
                stop_receipt.never_created_proof_digest
                if stop_receipt.disposition is AuditPreflightStopDisposition.NEVER_CREATED
                else None
            )
        ):
            raise _state_conflict()
        try:
            persisted = await self._repository.compare_and_set(
                previous=current,
                updated=current,
                stop_receipt=stop_receipt,
            )
        except RepositoryConflictError:
            raise _state_conflict() from None
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _persistence_unavailable() from None
        return _callback_ack(persisted)

    async def _load_callback_job(
        self,
        job_id: str,
        *,
        node_id: str,
        principal: RunnerPrincipal,
        protocol_capabilities: Sequence[str],
        owner: AuditPreflightEffectOwner,
        lease: AuditPreflightLeaseEnvelope,
        capsule_id: str,
    ) -> AuditPreflightJob:
        _require_protocol_capability(protocol_capabilities)
        _require_callback_identity_before_lookup(
            job_id,
            node_id=node_id,
            principal=principal,
            owner=owner,
            lease=lease,
        )
        binding = await self._load_owner_binding(job_id)
        if not _owner_binding_matches(binding, owner):
            raise _owner_conflict()
        current = await self._load_internal_job(job_id)
        if current.effect_owner() != owner:
            raise _persistence_unavailable()
        if current.capsule_id is None or not secrets.compare_digest(current.capsule_id, capsule_id):
            raise _lease_conflict()
        return current

    async def _reload_after_callback_conflict(
        self,
        job_id: str,
        *,
        retry_owner: tuple[
            str,
            RunnerPrincipal,
            Sequence[str],
            AuditPreflightEffectOwner,
            str,
        ]
        | None,
        lease: AuditPreflightLeaseEnvelope,
    ) -> AuditPreflightJob:
        if retry_owner is None:
            current = await self._load_internal_job(job_id)
            if current.lease_envelope() is None:
                raise _lease_conflict()
            return current
        node_id, principal, capabilities, owner, capsule_id = retry_owner
        return await self._load_callback_job(
            job_id,
            node_id=node_id,
            principal=principal,
            protocol_capabilities=capabilities,
            owner=owner,
            lease=lease,
            capsule_id=capsule_id,
        )

    async def _load_owner_binding(self, job_id: str) -> AuditPreflightOwnerBinding:
        try:
            binding = await self._repository.get_owner_binding(job_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _persistence_unavailable() from None
        if binding is None:
            raise EntityNotFoundError("AuditPreflightJob", job_id)
        return binding

    async def _load_internal_job(self, job_id: str) -> AuditPreflightJob:
        try:
            current = await self._repository.get(job_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _persistence_unavailable() from None
        if current is None:
            raise EntityNotFoundError("AuditPreflightJob", job_id)
        return current

    async def _load_reconciliation_candidate(
        self,
        job_id: str,
    ) -> AuditPreflightReconciliationCandidate:
        try:
            current = await self._repository.get_reconciliation_candidate(job_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _persistence_unavailable() from None
        if current is None:
            raise EntityNotFoundError("AuditPreflightJob", job_id)
        return current


def _dispatch_envelope(
    claimed: AuditPreflightDispatch,
    *,
    principal: RunnerPrincipal,
) -> AuditPreflightDispatchEnvelope:
    job = claimed.job
    lease = job.lease_envelope()
    if (
        job.status is not AuditPreflightJobStatus.CLAIMED
        or lease is None
        or lease.runner_principal != principal
        or lease.output_contract_digest != AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST
        or job.capsule_id is None
    ):
        raise _persistence_unavailable()
    try:
        return AuditPreflightDispatchEnvelope(
            owner=job.effect_owner(),
            lease=lease,
            request=claimed.request,
            capsule_id=job.capsule_id,
            state_version=job.state_version,
        )
    except (TypeError, ValueError):
        raise _persistence_unavailable() from None


def _require_protocol_capability(capabilities: Sequence[str]) -> None:
    if AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY not in capabilities:
        raise ApplicationConflictError(
            "runner_protocol_capability_missing",
            "Runner does not support the Audit Preflight owner protocol",
            details={"required_capability": AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY},
        )


def _require_local_node(node_id: str) -> None:
    if not secrets.compare_digest(node_id, "local"):
        raise ApplicationConflictError(
            "audit_preflight_node_mismatch",
            "Audit Preflight is restricted to the local source node",
        )


def _require_callback_identity_before_lookup(
    job_id: str,
    *,
    node_id: str,
    principal: RunnerPrincipal,
    owner: AuditPreflightEffectOwner,
    lease: AuditPreflightLeaseEnvelope,
) -> None:
    _require_local_node(node_id)
    if (
        not secrets.compare_digest(job_id, owner.job_id)
        or not secrets.compare_digest(node_id, owner.source_node_id)
        or lease.owner != owner
        or lease.runner_principal != principal
        or lease.output_contract_digest != AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST
    ):
        raise _owner_conflict()


def _owner_binding_matches(
    binding: AuditPreflightOwnerBinding,
    owner: AuditPreflightEffectOwner,
) -> bool:
    actual = (
        binding.job_id,
        binding.operator_principal_id,
        binding.authorization_scope_digest,
        binding.request_schema_version,
        binding.request_digest,
        binding.source_node_id,
        binding.source_root_identity_digest,
        binding.backend_id,
        binding.image_digest,
        binding.policy_digest,
        binding.effect_owner_digest,
    )
    expected = (
        owner.job_id,
        owner.operator_principal_id,
        owner.authorization_scope_digest,
        owner.request_schema_version,
        owner.request_digest,
        owner.source_node_id,
        owner.source_root_identity_digest,
        owner.backend_id,
        owner.image_digest,
        owner.policy_digest,
        owner.effect_owner_digest,
    )
    return all(
        secrets.compare_digest(left, right) for left, right in zip(actual, expected, strict=True)
    )


def _require_callback_state(
    current: AuditPreflightJob,
    *,
    lease: AuditPreflightLeaseEnvelope,
    state_version: int,
    allowed_statuses: Sequence[AuditPreflightJobStatus],
    require_unexpired: bool = False,
    now: datetime | None = None,
    allow_outcome_unknown_recovery: bool = False,
) -> None:
    if current.lease_envelope() != lease:
        raise _lease_conflict()
    exact_state = current.state_version == state_version
    recovery_state = allow_outcome_unknown_recovery and _is_outcome_unknown_recovery(
        current,
        lease=lease,
        state_version=state_version,
    )
    if current.status not in allowed_statuses or (not exact_state and not recovery_state):
        raise _state_conflict()
    if require_unexpired:
        if now is None or lease.lease_expires_at <= now or current.expires_at <= now:
            raise _lease_conflict()


def _is_renew_replay(
    current: AuditPreflightJob,
    *,
    lease: AuditPreflightLeaseEnvelope,
    state_version: int,
) -> bool:
    current_lease = current.lease_envelope()
    return bool(
        current.status
        in {
            AuditPreflightJobStatus.CLAIMED,
            AuditPreflightJobStatus.RUNNING,
            AuditPreflightJobStatus.CANCELLING,
        }
        and current.state_version == state_version + 1
        and current_lease is not None
        and current_lease.owner == lease.owner
        and current_lease.runner_principal == lease.runner_principal
        and current_lease.lease_id == lease.lease_id
        and current_lease.output_contract_digest == lease.output_contract_digest
        and current_lease.expected_state_version == current.state_version
        and current_lease.lease_expires_at >= lease.lease_expires_at
    )


def _is_outcome_unknown_recovery(
    current: AuditPreflightJob,
    *,
    lease: AuditPreflightLeaseEnvelope,
    state_version: int,
) -> bool:
    """Admit only the immediately fenced state known by the durable Runner journal."""

    return bool(
        current.status is AuditPreflightJobStatus.OUTCOME_UNKNOWN
        and current.state_version == state_version + 1
        and current.lease_expected_state_version == state_version
        and current.lease_envelope() == lease
    )


def _is_terminal_recovery_replay(
    current: AuditPreflightJob,
    *,
    lease: AuditPreflightLeaseEnvelope,
    state_version: int,
    allowed_advances: Sequence[int],
) -> bool:
    """Recognize an exact receipt replay after one outcome-unknown fence.

    Terminal rows do not retain a previous-status column.  The unchanged lease
    envelope, its expected version, the bounded state advance, and the exact
    persisted receipt checked by the repository form the durable recovery proof.
    """

    return bool(
        current.state_version - state_version in allowed_advances
        and current.lease_expected_state_version == state_version
        and current.lease_envelope() == lease
    )


def _is_start_replay(
    current: AuditPreflightJob,
    *,
    lease: AuditPreflightLeaseEnvelope,
    state_version: int,
    capsule_prepare_proof_digest: str,
) -> bool:
    return bool(
        current.status is AuditPreflightJobStatus.RUNNING
        and current.state_version == state_version + 1
        and current.lease_envelope() == lease
        and current.capsule_prepare_proof_digest == capsule_prepare_proof_digest
        and current.started_at is not None
    )


def _lease_grant(current: AuditPreflightJob, *, now: datetime) -> AuditPreflightLeaseGrant:
    lease = current.lease_envelope()
    if lease is None or lease.lease_expires_at <= now:
        raise _lease_conflict()
    return AuditPreflightLeaseGrant(
        job_id=current.job_id,
        status=current.status,
        state_version=current.state_version,
        lease_envelope_digest=lease.lease_envelope_digest,
        lease_expires_at=lease.lease_expires_at,
        lease_duration_seconds=(lease.lease_expires_at - now).total_seconds(),
    )


def _start_grant(current: AuditPreflightJob) -> AuditPreflightStartGrant:
    if current.capsule_id is None or current.started_at is None:
        raise _persistence_unavailable()
    return AuditPreflightStartGrant(
        job_id=current.job_id,
        capsule_id=current.capsule_id,
        state_version=current.state_version,
        started_at=current.started_at,
    )


def _callback_ack(current: AuditPreflightJob) -> AuditPreflightCallbackAck:
    return AuditPreflightCallbackAck(
        job_id=current.job_id,
        status=current.status,
        state_version=current.state_version,
        finished_at=current.finished_at,
    )


def _require_finish_shape(
    *,
    status: AuditPreflightJobStatus,
    result: AuditPreflightResult | None,
    safe_error_code: str | None,
    exit_receipt: AuditPreflightExitReceipt,
) -> None:
    expected_terminal = exit_receipt.terminal_state.value
    if expected_terminal != status.value:
        raise _state_conflict()
    if status is AuditPreflightJobStatus.SUCCEEDED:
        valid = result is not None and safe_error_code is None and not result.blocking_errors
    elif status is AuditPreflightJobStatus.REJECTED:
        valid = result is not None and safe_error_code is not None and bool(result.blocking_errors)
    else:
        valid = result is None and safe_error_code is not None
    if not valid:
        raise _state_conflict()
    if exit_receipt.result_digest != (result.result_digest if result is not None else None):
        raise _state_conflict()


def _require_stop_shape(
    *,
    status: AuditPreflightJobStatus,
    safe_error_code: str | None,
    stop_receipt: AuditPreflightStopReceipt,
) -> None:
    if status is AuditPreflightJobStatus.CANCELLED:
        if safe_error_code is not None:
            raise _state_conflict()
    elif safe_error_code is None:
        raise _state_conflict()
    if stop_receipt.disposition not in {
        AuditPreflightStopDisposition.STOPPED,
        AuditPreflightStopDisposition.NEVER_CREATED,
    }:
        raise _state_conflict()


def _require_receipt_owner(
    current: AuditPreflightJob,
    *,
    exit_receipt: AuditPreflightExitReceipt | None = None,
    stop_receipt: AuditPreflightStopReceipt | None = None,
) -> None:
    receipt = exit_receipt if exit_receipt is not None else stop_receipt
    if receipt is None:
        raise _state_conflict()
    lease = current.lease_envelope()
    if (
        lease is None
        or receipt.job_id != current.job_id
        or receipt.effect_owner_digest != current.effect_owner_digest
        or receipt.lease_envelope_digest != lease.lease_envelope_digest
        or receipt.source_node_id != current.source_node_id
        or receipt.runner_principal != lease.runner_principal
        or receipt.backend_id != current.backend_id
        or receipt.image_digest != current.image_digest
        or receipt.policy_digest != current.policy_digest
    ):
        raise _owner_conflict()
    if exit_receipt is not None and exit_receipt.capsule_id != current.capsule_id:
        raise _owner_conflict()
    if (
        stop_receipt is not None
        and stop_receipt.disposition is AuditPreflightStopDisposition.STOPPED
        and stop_receipt.capsule_id != current.capsule_id
    ):
        raise _owner_conflict()


def _replace_job(job: AuditPreflightJob, **updates: object) -> AuditPreflightJob:
    payload = job.model_dump(mode="python")
    payload.update(updates)
    try:
        return AuditPreflightJob.model_validate(payload)
    except (TypeError, ValueError):
        raise _state_conflict() from None


def _expired_pending_proof_digest(
    job: AuditPreflightReconciliationCandidate,
    *,
    observed_at: datetime,
) -> str:
    payload = {
        "effect_owner_digest": job.effect_owner_digest,
        "expected_state_version": job.state_version,
        "expected_status": AuditPreflightJobStatus.PENDING.value,
        "job_id": job.job_id,
        "observed_at": observed_at.isoformat(),
        "reason_code": "audit_preflight_job_expired",
        "schema_version": _RECONCILER_NEVER_CREATED_PROOF_VERSION,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(
        _RECONCILER_NEVER_CREATED_PROOF_VERSION.encode("ascii") + b"\0" + canonical
    ).hexdigest()


def _authentication_failed() -> AuthenticationError:
    return AuthenticationError(
        "runner_authentication_failed",
        "Runner credentials are missing or invalid",
    )


def _owner_conflict() -> ApplicationConflictError:
    return ApplicationConflictError(
        "audit_preflight_owner_mismatch",
        "Audit Preflight callback ownership does not match",
    )


def _lease_conflict() -> ApplicationConflictError:
    return ApplicationConflictError(
        "audit_preflight_lease_mismatch",
        "Audit Preflight callback lease is missing, stale, or expired",
    )


def _state_conflict() -> ApplicationConflictError:
    return ApplicationConflictError(
        "audit_preflight_state_conflict",
        "Audit Preflight state changed or callback facts differ",
    )


def _cancel_requested() -> ApplicationConflictError:
    return ApplicationConflictError(
        "audit_preflight_cancel_requested",
        "Audit Preflight cancellation fenced the normal finish callback",
    )


def _persistence_unavailable() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "audit_preflight_persistence_unavailable",
        "RiftX Code Audit Preflight persistence is temporarily unavailable",
    )


__all__ = ["AuditPreflightRunnerService"]
