"""Authorized issuance edge for durable Code Audit Preflight Plans."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from riftx.application.errors import (
    ApplicationConflictError,
    RepositoryConflictError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
    ServiceUnavailableError,
    resource_not_accessible,
)
from riftx.application.ports.audit_preflight import (
    AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION,
    AuditPreflightOwnerBinding,
    AuditPreflightRepository,
)
from riftx.application.ports.audit_preflight_plan import (
    AuditPreflightPlanOwnerBinding,
    AuditPreflightPlanRepository,
)
from riftx.application.ports.audits import AuditObjectAuthorizer
from riftx.domain import LocalPrincipal, OperatorCapability
from riftx.domain.audit_preflight import (
    AuditPreflightJob,
    AuditPreflightJobStatus,
    AuditPreflightResult,
    PreflightRequest,
)
from riftx.domain.audit_preflight_plan import (
    AuditPreflightPlan,
    AuditPreflightPlanScope,
    AuditPreflightPlanStatus,
    AuditPreflightPlanTarget,
    AuditPreflightTokenCodec,
)
from riftx.domain.base import new_id, utc_now

AuditPreflightPlanIdFactory = Callable[[], str]
AuditPreflightPlanClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class AuditPreflightPlanIssuanceResult:
    """One no-store issuance result whose bearer value is never represented."""

    plan: AuditPreflightPlan
    preflight_token: str = field(repr=False)
    created: bool = True

    @property
    def replayed(self) -> bool:
        return not self.created


class AuditPreflightPlanApplicationService:
    """Issue or replay a principal-owned, short-lived Preflight Plan token.

    Authorization is resolved from bounded Job columns before this service asks
    either repository for restricted request, Result, Plan, nonce, or token
    material. The Job repository's aggregate-read contract guarantees that a
    returned terminal Job has already been checked against its canonical
    request, Result, and exit-receipt child rows.
    """

    def __init__(
        self,
        *,
        preflight_repository: AuditPreflightRepository,
        plan_repository: AuditPreflightPlanRepository,
        feature_enabled: bool,
        token_codec: AuditPreflightTokenCodec | None,
        retained_token_codecs: Sequence[AuditPreflightTokenCodec] = (),
        plan_ttl_seconds: int = 900,
        id_factory: AuditPreflightPlanIdFactory = new_id,
        clock: AuditPreflightPlanClock = utc_now,
    ) -> None:
        if not isinstance(plan_ttl_seconds, int) or isinstance(plan_ttl_seconds, bool):
            raise TypeError("plan_ttl_seconds must be an integer")
        if not 60 <= plan_ttl_seconds <= 3_600:
            raise ValueError("plan_ttl_seconds must be between 60 and 3600")
        if token_codec is not None and not isinstance(token_codec, AuditPreflightTokenCodec):
            raise TypeError("token_codec must be an AuditPreflightTokenCodec or None")
        if any(
            not isinstance(codec, AuditPreflightTokenCodec)
            for codec in retained_token_codecs
        ):
            raise TypeError("retained_token_codecs must contain token codecs")

        codecs = ((token_codec,) if token_codec is not None else ()) + tuple(
            retained_token_codecs
        )
        codec_by_key_id: dict[str, AuditPreflightTokenCodec] = {}
        for codec in codecs:
            if codec.key_id in codec_by_key_id:
                raise ValueError("preflight token key IDs must be unique")
            codec_by_key_id[codec.key_id] = codec

        self._preflight_repository = preflight_repository
        self._plan_repository = plan_repository
        self._feature_enabled = bool(feature_enabled)
        self._token_codec = token_codec
        self._codec_by_key_id = codec_by_key_id
        self._plan_ttl_seconds = plan_ttl_seconds
        self._id_factory = id_factory
        self._clock = clock

    def require_issuance_enabled(self) -> None:
        """Fail before any Job, Plan, nonce, or token operation when disabled."""

        if not self._feature_enabled:
            raise ServiceUnavailableError(
                "audit_feature_disabled",
                "RiftX Code Audit Preflight Plan issuance is disabled",
            )

    async def issue_authorized(
        self,
        job_id: str,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
    ) -> AuditPreflightPlanIssuanceResult:
        """Issue once or re-derive the exact available Plan bearer token."""

        authorization_scope_digest = authorizer.preflight_authorization_scope_digest(
            principal,
            capability=OperatorCapability.WRITE,
        )
        self.require_issuance_enabled()

        binding = await self._load_job_owner_binding(job_id)
        if binding is None or not _job_binding_is_authorized(
            binding,
            principal=principal,
            authorization_scope_digest=authorization_scope_digest,
        ):
            raise resource_not_accessible() from None
        if (
            binding.plan_issuance_schema_version
            != AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION
        ):
            raise _plan_unavailable() from None

        job = await self._load_full_job(binding)
        if job.status is not AuditPreflightJobStatus.SUCCEEDED:
            raise ApplicationConflictError(
                "audit_preflight_not_succeeded",
                "The Code Audit Preflight Job is not eligible for Plan issuance",
            )

        now = self._clock()
        request, result = _reconstruct_succeeded_material(job)
        if now >= job.expires_at or now >= result.expires_at:
            raise ApplicationConflictError(
                "audit_preflight_expired",
                "The Code Audit Preflight result has expired",
            )
        if result.blocking_errors:
            raise _plan_unavailable() from None

        existing = await self._load_plan_binding_for_job(job.job_id)
        if existing is not None:
            return await self._replay_available_plan(
                existing,
                job=job,
                request=request,
                result=result,
                now=now,
            )

        codec = self._token_codec
        if codec is None:
            raise _token_key_unavailable() from None
        expires_at = min(
            now + timedelta(seconds=self._plan_ttl_seconds),
            job.expires_at,
            result.expires_at,
        )
        if expires_at <= now:
            raise ApplicationConflictError(
                "audit_preflight_expired",
                "The Code Audit Preflight result has expired",
            )
        try:
            issue = AuditPreflightPlan.from_succeeded(
                job=job,
                result=result,
                restricted_request=request,
                token_codec=codec,
                plan_id=self._id_factory(),
                created_at=now,
                expires_at=expires_at,
            )
        except (AttributeError, TypeError, ValueError):
            raise _plan_mismatch() from None

        try:
            persisted, created = await self._plan_repository.create(issue.plan)
        except RepositoryConflictError:
            # A concurrent issuer may have won the unique Job binding. Reload
            # that winner and replay only if its complete identity is valid.
            winner = await self._load_plan_binding_for_job(job.job_id)
            if winner is None:
                raise _plan_mismatch() from None
            return await self._replay_available_plan(
                winner,
                job=job,
                request=request,
                result=result,
                now=now,
            )
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _plan_persistence_unavailable() from None

        if created:
            if persisted != issue.plan:
                raise _plan_mismatch() from None
            token = _token_for_available_plan(
                persisted,
                codec=codec,
                now=now,
            )
            if not secrets.compare_digest(token, issue.token):
                raise _plan_mismatch() from None
            return AuditPreflightPlanIssuanceResult(
                plan=persisted,
                preflight_token=token,
                created=True,
            )

        # Some adapters may resolve an exact create race by returning the
        # durable winner instead of raising RepositoryConflictError.
        _require_plan_matches_sources(
            persisted,
            job=job,
            request=request,
            result=result,
        )
        token = self._rederive_token(persisted, now=now)
        return AuditPreflightPlanIssuanceResult(
            plan=persisted,
            preflight_token=token,
            created=False,
        )

    async def _load_job_owner_binding(
        self,
        job_id: str,
    ) -> AuditPreflightOwnerBinding | None:
        try:
            return await self._preflight_repository.get_owner_binding(job_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _preflight_persistence_unavailable() from None

    async def _load_full_job(
        self,
        binding: AuditPreflightOwnerBinding,
    ) -> AuditPreflightJob:
        try:
            job = await self._preflight_repository.get(binding.job_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _preflight_persistence_unavailable() from None
        if job is None:
            raise resource_not_accessible() from None
        _require_job_matches_binding(job, binding)
        return job

    async def _load_plan_binding_for_job(
        self,
        job_id: str,
    ) -> AuditPreflightPlanOwnerBinding | None:
        try:
            return await self._plan_repository.get_owner_binding_for_job(job_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _plan_persistence_unavailable() from None

    async def _replay_available_plan(
        self,
        binding: AuditPreflightPlanOwnerBinding,
        *,
        job: AuditPreflightJob,
        request: PreflightRequest,
        result: AuditPreflightResult,
        now: datetime,
    ) -> AuditPreflightPlanIssuanceResult:
        _require_plan_owner_matches_job(binding, job)
        if binding.status is not AuditPreflightPlanStatus.AVAILABLE:
            raise _plan_unavailable() from None
        if now >= binding.expires_at:
            raise ApplicationConflictError(
                "audit_preflight_expired",
                "The Code Audit Preflight Plan has expired",
            )
        try:
            plan = await self._plan_repository.get(binding.plan_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _plan_persistence_unavailable() from None
        if plan is None:
            raise _plan_mismatch() from None
        _require_plan_matches_binding(plan, binding)
        _require_plan_matches_sources(
            plan,
            job=job,
            request=request,
            result=result,
        )
        token = self._rederive_token(plan, now=now)
        return AuditPreflightPlanIssuanceResult(
            plan=plan,
            preflight_token=token,
            created=False,
        )

    def _rederive_token(self, plan: AuditPreflightPlan, *, now: datetime) -> str:
        codec = self._codec_by_key_id.get(plan.token_verifier.key_id)
        if codec is None:
            raise _token_key_unavailable() from None
        return _token_for_available_plan(plan, codec=codec, now=now)


def _reconstruct_succeeded_material(
    job: AuditPreflightJob,
) -> tuple[PreflightRequest, AuditPreflightResult]:
    if (
        job.result_json is None
        or job.result_schema_version is None
        or job.result_digest is None
        or job.exit_receipt_digest is None
    ):
        raise _plan_mismatch() from None
    try:
        request = PreflightRequest.model_validate_json(job.restricted_request_json)
        result = AuditPreflightResult.model_validate_json(job.result_json)
    except (AttributeError, TypeError, ValueError):
        raise _plan_mismatch() from None
    if (
        request.canonical_json() != job.restricted_request_json
        or result.canonical_json() != job.result_json
        or request.schema_version != job.request_schema_version
        or result.schema_version != job.result_schema_version
        or not secrets.compare_digest(request.request_digest, job.request_digest)
        or not secrets.compare_digest(result.result_digest, job.result_digest)
    ):
        raise _plan_mismatch() from None
    return request, result


def _job_binding_is_authorized(
    binding: AuditPreflightOwnerBinding,
    *,
    principal: LocalPrincipal,
    authorization_scope_digest: str,
) -> bool:
    return secrets.compare_digest(binding.operator_principal_id, principal.id) and (
        secrets.compare_digest(
            binding.authorization_scope_digest,
            authorization_scope_digest,
        )
    )


def _require_job_matches_binding(
    job: AuditPreflightJob,
    binding: AuditPreflightOwnerBinding,
) -> None:
    string_pairs = (
        (job.job_id, binding.job_id),
        (job.operator_principal_id, binding.operator_principal_id),
        (job.authorization_scope_digest, binding.authorization_scope_digest),
        (job.request_schema_version, binding.request_schema_version),
        (job.request_digest, binding.request_digest),
        (job.source_node_id, binding.source_node_id),
        (job.source_root_identity_digest, binding.source_root_identity_digest),
        (job.backend_id, binding.backend_id),
        (job.image_digest, binding.image_digest),
        (job.policy_digest, binding.policy_digest),
        (job.effect_owner_digest, binding.effect_owner_digest),
    )
    if (
        job.status is not binding.status
        or job.state_version != binding.state_version
        or not _constant_time_pairs_equal(string_pairs)
    ):
        raise _preflight_persistence_unavailable() from None


def _require_plan_owner_matches_job(
    binding: AuditPreflightPlanOwnerBinding,
    job: AuditPreflightJob,
) -> None:
    if not _constant_time_pairs_equal(
        (
            (binding.preflight_job_id, job.job_id),
            (binding.operator_principal_id, job.operator_principal_id),
            (binding.authorization_scope_digest, job.authorization_scope_digest),
        )
    ):
        raise _plan_mismatch() from None


def _require_plan_matches_binding(
    plan: AuditPreflightPlan,
    binding: AuditPreflightPlanOwnerBinding,
) -> None:
    if (
        plan.status is not binding.status
        or plan.state_version != binding.state_version
        or plan.expires_at != binding.expires_at
        or plan.reserved_audit_id != binding.reserved_audit_id
        or plan.reserved_client_request_id != binding.reserved_client_request_id
        or plan.consumed_audit_id != binding.consumed_audit_id
        or not _constant_time_pairs_equal(
            (
                (plan.plan_id, binding.plan_id),
                (plan.preflight_job_id, binding.preflight_job_id),
                (plan.operator_principal_id, binding.operator_principal_id),
                (
                    plan.authorization_scope_digest,
                    binding.authorization_scope_digest,
                ),
                (plan.plan_digest, binding.plan_digest),
            )
        )
    ):
        raise _plan_mismatch() from None


def _require_plan_matches_sources(
    plan: AuditPreflightPlan,
    *,
    job: AuditPreflightJob,
    request: PreflightRequest,
    result: AuditPreflightResult,
) -> None:
    try:
        expected_target = AuditPreflightPlanTarget(
            repository_path=request.repository_path,
            source_node_id=job.source_node_id,
            source_ingest_backend_id=job.backend_id,
            kind=result.target_kind,
            revision=result.revision,
            base_revision=result.base_revision,
            mode=result.mode,
            include_untracked=result.include_untracked,
            head_revision=result.head_revision,
            resolved_revision=result.resolved_revision,
            resolved_base_revision=result.resolved_base_revision,
            merge_base_revision=result.merge_base_revision,
        )
        expected_scope = AuditPreflightPlanScope(
            include_paths=request.include_paths,
            exclude_paths=request.exclude_paths,
        )
    except (AttributeError, TypeError, ValueError):
        raise _plan_mismatch() from None

    string_pairs = (
        (plan.preflight_job_id, job.job_id),
        (plan.preflight_client_request_id, job.client_request_id),
        (plan.operator_principal_id, job.operator_principal_id),
        (plan.authorization_scope_digest, job.authorization_scope_digest),
        (plan.request_schema_version, job.request_schema_version),
        (plan.request_digest, job.request_digest),
        (plan.result_schema_version, result.schema_version),
        (plan.result_digest, result.result_digest),
        (plan.effect_owner_digest, job.effect_owner_digest),
        (plan.source_node_id, job.source_node_id),
        (plan.source_root_identity_digest, job.source_root_identity_digest),
        (plan.repository_identity_digest, result.repository_identity_digest),
        (plan.content_identity_digest, result.content_identity_digest),
        (plan.backend_id, job.backend_id),
        (plan.image_digest, job.image_digest),
        (plan.policy_digest, job.policy_digest),
        (
            plan.capsule_prepare_proof_digest,
            result.capsule_prepare_proof_digest,
        ),
        (plan.security_context_id, result.canonical_empty_context_id),
        (plan.security_context_digest, result.canonical_empty_context_digest),
    )
    if (
        plan.status is not AuditPreflightPlanStatus.AVAILABLE
        or not _constant_time_pairs_equal(string_pairs)
        or plan.target != expected_target
        or plan.scope != expected_scope
        or plan.capability_matrix != result.capability_matrix
        or plan.minimum_feasible_budget != result.minimum_feasible_budget
        or plan.preflight_completed_at != result.completed_at
        or plan.expires_at > min(job.expires_at, result.expires_at)
    ):
        raise _plan_mismatch() from None


def _token_for_available_plan(
    plan: AuditPreflightPlan,
    *,
    codec: AuditPreflightTokenCodec,
    now: datetime,
) -> str:
    if plan.status is not AuditPreflightPlanStatus.AVAILABLE:
        raise _plan_unavailable() from None
    if now >= plan.expires_at:
        raise ApplicationConflictError(
            "audit_preflight_expired",
            "The Code Audit Preflight Plan has expired",
        )
    try:
        return codec.token_for(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            verifier=plan.token_verifier,
        )
    except (AttributeError, TypeError, ValueError):
        raise _plan_mismatch() from None


def _constant_time_pairs_equal(pairs: Sequence[tuple[str, str]]) -> bool:
    return all(secrets.compare_digest(left, right) for left, right in pairs)


def _plan_unavailable() -> ApplicationConflictError:
    return ApplicationConflictError(
        "audit_preflight_plan_unavailable",
        "The Code Audit Preflight Plan is unavailable",
    )


def _plan_mismatch() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "audit_preflight_plan_mismatch",
        "The Code Audit Preflight Plan could not be verified",
    )


def _token_key_unavailable() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "audit_preflight_token_key_unavailable",
        "The Code Audit Preflight token key is unavailable",
    )


def _preflight_persistence_unavailable() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "audit_preflight_persistence_unavailable",
        "RiftX Code Audit Preflight persistence is temporarily unavailable",
    )


def _plan_persistence_unavailable() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "audit_preflight_plan_unavailable",
        "RiftX Code Audit Preflight Plan persistence is temporarily unavailable",
    )


__all__ = [
    "AuditPreflightPlanApplicationService",
    "AuditPreflightPlanClock",
    "AuditPreflightPlanIdFactory",
    "AuditPreflightPlanIssuanceResult",
]
