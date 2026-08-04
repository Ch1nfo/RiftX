"""Operator application edge for durable, non-Run Code Audit Preflight jobs."""

from __future__ import annotations

import hashlib
import inspect
import json
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from riftx.application.errors import (
    ApplicationConflictError,
    RepositoryConflictError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
    ServiceUnavailableError,
    resource_not_accessible,
)
from riftx.application.ports.audit_preflight import (
    AuditPreflightOwnerBinding,
    AuditPreflightRepository,
)
from riftx.application.ports.audits import AuditObjectAuthorizer
from riftx.audit.paths import (
    DEFAULT_SOURCE_PATH_POLICY_VERSION,
    AuthorizedSourceRepository,
    SourcePathAuthorizationError,
    SourcePathFailure,
    open_authorized_source_repository,
    validate_repository_filters,
)
from riftx.domain import LocalPrincipal, OperatorCapability
from riftx.domain.audit_preflight import (
    AuditPreflightJob,
    AuditPreflightJobStatus,
    PreflightRequest,
)
from riftx.domain.base import new_id, utc_now

AuditPreflightIdFactory = Callable[[], str]
AuditPreflightClock = Callable[[], datetime]
AuditPreflightAvailabilityCheck = Callable[[], bool | Awaitable[bool]]

_DIGEST_PATTERN = frozenset("0123456789abcdef")
_TERMINAL_STATUSES = frozenset(
    {
        AuditPreflightJobStatus.SUCCEEDED,
        AuditPreflightJobStatus.REJECTED,
        AuditPreflightJobStatus.FAILED,
        AuditPreflightJobStatus.CANCELLED,
    }
)
_CONTROL_PLANE_NEVER_CREATED_PROOF_VERSION = (
    "riftx.audit-preflight-control-plane-never-created-proof/v1"
)


class AuditPreflightSourceOpener(Protocol):
    def __call__(
        self,
        repository_path: str,
        *,
        allowed_roots: Sequence[str | Path],
        policy_version: str,
    ) -> AuthorizedSourceRepository: ...


@dataclass(frozen=True, slots=True)
class AuditPreflightCreationResult:
    """One durable create result with explicit replay semantics."""

    job: AuditPreflightJob
    created: bool

    @property
    def replayed(self) -> bool:
        return not self.created


class AuditPreflightApplicationService:
    """Create, read, and cancel principal-owned SourceIngest Preflight jobs.

    This service deliberately has no Run, Workflow, Event, Artifact, Snapshot,
    model, or general Runner-command dependency.  Restricted request data is
    loaded only after a bounded owner binding has passed authorization.
    """

    def __init__(
        self,
        *,
        repository: AuditPreflightRepository,
        feature_enabled: bool,
        source_roots: Sequence[str | Path],
        backend_id: str,
        image_digest: str | None,
        policy_digest: str,
        source_ingest_available: bool | AuditPreflightAvailabilityCheck = False,
        node_mode: str = "local_same_node",
        allowed_node_ids: Sequence[str] = ("local",),
        source_path_policy_version: str = DEFAULT_SOURCE_PATH_POLICY_VERSION,
        job_ttl_seconds: int = 900,
        id_factory: AuditPreflightIdFactory = new_id,
        clock: AuditPreflightClock = utc_now,
        source_opener: AuditPreflightSourceOpener = open_authorized_source_repository,
    ) -> None:
        if node_mode != "local_same_node" or tuple(allowed_node_ids) != ("local",):
            raise ValueError("Audit Preflight requires the local same-node policy")
        if backend_id != "linux_container":
            raise ValueError("Audit Preflight requires the linux_container backend")
        if image_digest is not None:
            _require_digest(image_digest, "image_digest")
        _require_digest(policy_digest, "policy_digest")
        if not isinstance(source_path_policy_version, str) or not source_path_policy_version:
            raise ValueError("source_path_policy_version must be non-empty")
        if not isinstance(job_ttl_seconds, int) or isinstance(job_ttl_seconds, bool):
            raise TypeError("job_ttl_seconds must be an integer")
        if not 60 <= job_ttl_seconds <= 86_400:
            raise ValueError("job_ttl_seconds must be between 60 and 86400")
        if not isinstance(source_ingest_available, bool) and not callable(source_ingest_available):
            raise TypeError("source_ingest_available must be a bool or callable")

        self._repository = repository
        self._feature_enabled = bool(feature_enabled)
        self._source_roots = tuple(source_roots)
        self._backend_id = backend_id
        self._image_digest = image_digest
        self._policy_digest = policy_digest
        self._source_ingest_available = source_ingest_available
        self._source_path_policy_version = source_path_policy_version
        self._job_ttl_seconds = job_ttl_seconds
        self._id_factory = id_factory
        self._clock = clock
        self._source_opener = source_opener

    def require_create_enabled(self) -> None:
        """Fail before body/source parsing when the create feature is disabled."""

        if not self._feature_enabled:
            raise ServiceUnavailableError(
                "audit_feature_disabled",
                "RiftX Code Audit Preflight creation is disabled",
            )

    async def create_authorized(
        self,
        request: PreflightRequest,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
    ) -> AuditPreflightCreationResult:
        """Create one principal-owned job or return its exact durable replay."""

        authorization_scope_digest = authorizer.preflight_authorization_scope_digest(
            principal,
            capability=OperatorCapability.HOST_EXECUTE,
        )
        self.require_create_enabled()
        if not isinstance(request, PreflightRequest):
            raise ApplicationConflictError(
                "audit_target_invalid",
                "The Code Audit Preflight request is invalid",
            )
        if (
            request.source_execution_target.node_id != "local"
            or request.source_execution_target.source_ingest_backend != self._backend_id
        ):
            raise ApplicationConflictError(
                "audit_target_invalid",
                "The Code Audit Preflight target is not allowed",
            )
        try:
            validate_repository_filters(
                include_paths=request.include_paths,
                exclude_paths=request.exclude_paths,
            )
        except SourcePathAuthorizationError:
            raise ApplicationConflictError(
                "audit_target_invalid",
                "The Code Audit Preflight path filters are invalid",
            ) from None

        existing = await self._get_idempotency_binding(
            principal=principal,
            client_request_id=request.client_request_id,
        )
        if existing is not None:
            if not secrets.compare_digest(
                existing.operator_principal_id,
                principal.id,
            ):
                raise _persistence_unavailable()
            if not _constant_time_text_tuple_equal(
                (
                    existing.authorization_scope_digest,
                    existing.request_schema_version,
                    existing.request_digest,
                ),
                (
                    authorization_scope_digest,
                    request.schema_version,
                    request.request_digest,
                ),
            ):
                raise _idempotency_conflict()
            return AuditPreflightCreationResult(
                job=await self._load_job_for_binding(existing),
                created=False,
            )

        if not self._source_roots:
            raise ApplicationConflictError(
                "audit_source_not_allowed",
                "The requested Code Audit source is not allowed",
            )
        await self._require_source_ingest_available()

        try:
            with self._source_opener(
                request.repository_path,
                allowed_roots=self._source_roots,
                policy_version=self._source_path_policy_version,
            ) as authorized_source:
                authorized_source.verify_unchanged()
                source_root_identity_digest = authorized_source.source_root_identity_digest
        except SourcePathAuthorizationError as exc:
            raise _map_source_authorization_error(exc) from None

        created_at = self._clock()
        expires_at = created_at + timedelta(seconds=self._job_ttl_seconds)
        assert self._image_digest is not None
        try:
            candidate = AuditPreflightJob(
                job_id=self._id_factory(),
                client_request_id=request.client_request_id,
                operator_principal_id=principal.id,
                authorization_scope_digest=authorization_scope_digest,
                request_schema_version=request.schema_version,
                request_digest=request.request_digest,
                restricted_request_json=request.canonical_json(),
                source_node_id=request.source_execution_target.node_id,
                source_root_identity_digest=source_root_identity_digest,
                backend_id=self._backend_id,
                image_digest=self._image_digest,
                policy_digest=self._policy_digest,
                expires_at=expires_at,
                created_at=created_at,
                updated_at=created_at,
            )
        except (AttributeError, TypeError, ValueError):
            raise ServiceUnavailableError(
                "audit_sandbox_unavailable",
                "RiftX Code Audit SourceIngest policy is unavailable",
            ) from None
        try:
            job, created = await self._repository.create(candidate)
        except RepositoryConflictError:
            raise _idempotency_conflict() from None
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _persistence_unavailable() from None
        _require_created_job_binding(
            job,
            principal=principal,
            authorization_scope_digest=authorization_scope_digest,
            request=request,
        )
        return AuditPreflightCreationResult(job=job, created=created)

    async def get_authorized(
        self,
        job_id: str,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
        capability: OperatorCapability = OperatorCapability.READ,
    ) -> AuditPreflightJob:
        """Authorize bounded owner columns before loading the restricted job."""

        authorization_scope_digest = authorizer.preflight_authorization_scope_digest(
            principal,
            capability=capability,
        )
        return await self._load_authorized(
            job_id,
            principal=principal,
            authorization_scope_digest=authorization_scope_digest,
        )

    async def cancel_authorized(
        self,
        job_id: str,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
    ) -> AuditPreflightJob:
        """Fence a job, completing pending jobs with a DB-proven no-effect fact."""

        authorization_scope_digest = authorizer.preflight_authorization_scope_digest(
            principal,
            capability=OperatorCapability.HOST_CONTROL,
        )
        for _ in range(8):
            current = await self._load_authorized(
                job_id,
                principal=principal,
                authorization_scope_digest=authorization_scope_digest,
            )
            if current.status in _TERMINAL_STATUSES or (
                current.status is AuditPreflightJobStatus.CANCELLING
            ):
                return current

            changed_at = max(self._clock(), current.updated_at)
            updates: dict[str, object] = {
                "state_version": current.state_version + 1,
                "updated_at": changed_at,
            }
            if current.status is AuditPreflightJobStatus.PENDING:
                current.validate_transition_to(AuditPreflightJobStatus.CANCELLED)
                updates.update(
                    status=AuditPreflightJobStatus.CANCELLED,
                    never_created_proof_digest=_pending_never_created_proof_digest(
                        current,
                        cancelled_at=changed_at,
                    ),
                    finished_at=changed_at,
                )
            else:
                current.validate_transition_to(AuditPreflightJobStatus.CANCELLING)
                updates["status"] = AuditPreflightJobStatus.CANCELLING
            updated = _validated_job_update(current, **updates)
            try:
                return await self._repository.compare_and_set(
                    previous=current,
                    updated=updated,
                )
            except RepositoryConflictError:
                continue
            except (RepositoryIntegrityError, RepositoryUnavailableError):
                raise _persistence_unavailable() from None
        raise ApplicationConflictError(
            "audit_preflight_state_conflict",
            "The Code Audit Preflight state changed concurrently",
        )

    async def _load_authorized(
        self,
        job_id: str,
        *,
        principal: LocalPrincipal,
        authorization_scope_digest: str,
    ) -> AuditPreflightJob:
        try:
            binding = await self._repository.get_owner_binding(job_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _persistence_unavailable() from None
        if binding is None or not _binding_is_authorized(
            binding,
            principal=principal,
            authorization_scope_digest=authorization_scope_digest,
        ):
            raise resource_not_accessible() from None
        return await self._load_job_for_binding(binding)

    async def _load_job_for_binding(
        self,
        binding: AuditPreflightOwnerBinding,
    ) -> AuditPreflightJob:
        try:
            job = await self._repository.get(binding.job_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _persistence_unavailable() from None
        if job is None:
            raise resource_not_accessible() from None
        _require_full_job_matches_binding(job, binding)
        return job

    async def _get_idempotency_binding(
        self,
        *,
        principal: LocalPrincipal,
        client_request_id: str,
    ) -> AuditPreflightOwnerBinding | None:
        try:
            return await self._repository.get_idempotency_binding(
                operator_principal_id=principal.id,
                client_request_id=client_request_id,
            )
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _persistence_unavailable() from None

    async def _require_source_ingest_available(self) -> None:
        if self._image_digest is None:
            raise _sandbox_unavailable()
        check = self._source_ingest_available
        try:
            available: object = check() if callable(check) else check
            if inspect.isawaitable(available):
                available = await cast(Awaitable[bool], available)
        except Exception:
            raise _sandbox_unavailable() from None
        if available is not True:
            raise _sandbox_unavailable()


def _binding_is_authorized(
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


def _require_created_job_binding(
    job: AuditPreflightJob,
    *,
    principal: LocalPrincipal,
    authorization_scope_digest: str,
    request: PreflightRequest,
) -> None:
    expected = (
        principal.id,
        authorization_scope_digest,
        request.client_request_id,
        request.schema_version,
        request.request_digest,
    )
    actual = (
        job.operator_principal_id,
        job.authorization_scope_digest,
        job.client_request_id,
        job.request_schema_version,
        job.request_digest,
    )
    if not _constant_time_text_tuple_equal(actual, expected):
        raise _persistence_unavailable()


def _require_full_job_matches_binding(
    job: AuditPreflightJob,
    binding: AuditPreflightOwnerBinding,
) -> None:
    expected = (
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
    actual = (
        job.job_id,
        job.operator_principal_id,
        job.authorization_scope_digest,
        job.request_schema_version,
        job.request_digest,
        job.source_node_id,
        job.source_root_identity_digest,
        job.backend_id,
        job.image_digest,
        job.policy_digest,
        job.effect_owner_digest,
    )
    if not _constant_time_text_tuple_equal(actual, expected):
        raise _persistence_unavailable()


def _constant_time_text_tuple_equal(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> bool:
    if len(left) != len(right):
        return False
    return all(
        secrets.compare_digest(actual, expected)
        for actual, expected in zip(left, right, strict=True)
    )


def _validated_job_update(
    job: AuditPreflightJob,
    **updates: object,
) -> AuditPreflightJob:
    payload = job.model_dump(mode="python")
    payload.update(updates)
    return AuditPreflightJob.model_validate(payload)


def _pending_never_created_proof_digest(
    job: AuditPreflightJob,
    *,
    cancelled_at: datetime,
) -> str:
    payload = {
        "cancelled_at": cancelled_at.isoformat(),
        "effect_owner_digest": job.effect_owner_digest,
        "expected_state_version": job.state_version,
        "expected_status": AuditPreflightJobStatus.PENDING.value,
        "job_id": job.job_id,
        "schema_version": _CONTROL_PLANE_NEVER_CREATED_PROOF_VERSION,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(
        _CONTROL_PLANE_NEVER_CREATED_PROOF_VERSION.encode("ascii") + b"\0" + canonical
    ).hexdigest()


def _map_source_authorization_error(
    error: SourcePathAuthorizationError,
) -> ApplicationConflictError | ServiceUnavailableError:
    if error.failure is SourcePathFailure.PLATFORM_UNSUPPORTED:
        return _sandbox_unavailable()
    if error.failure in {
        SourcePathFailure.SOURCE_ROOTS_EMPTY,
        SourcePathFailure.SOURCE_OUTSIDE_ROOT,
    }:
        return ApplicationConflictError(
            "audit_source_not_allowed",
            "The requested Code Audit source is not allowed",
        )
    return ApplicationConflictError(
        "audit_repository_invalid",
        "The requested Code Audit repository could not be safely authorized",
    )


def _sandbox_unavailable() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "audit_sandbox_unavailable",
        "RiftX Code Audit SourceIngest is temporarily unavailable",
    )


def _persistence_unavailable() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "audit_preflight_persistence_unavailable",
        "RiftX Code Audit Preflight persistence is temporarily unavailable",
    )


def _idempotency_conflict() -> ApplicationConflictError:
    return ApplicationConflictError(
        "audit_preflight_idempotency_conflict",
        "The client request identifier is already bound to different Preflight content",
    )


def _require_digest(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _DIGEST_PATTERN for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


__all__ = [
    "AuditPreflightApplicationService",
    "AuditPreflightAvailabilityCheck",
    "AuditPreflightCreationResult",
]
