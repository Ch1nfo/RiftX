from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from riftx.application.errors import (
    ApplicationConflictError,
    RepositoryConflictError,
    ResourceNotAccessibleError,
    ServiceUnavailableError,
)
from riftx.application.ports.audit_preflight import AuditPreflightOwnerBinding
from riftx.application.services.audit_preflight import (
    AuditPreflightApplicationService,
)
from riftx.domain import (
    AuditMode,
    LocalPrincipal,
    OperatorCapability,
    SourceTargetKind,
)
from riftx.domain.audit_preflight import (
    AuditPreflightJob,
    AuditPreflightJobStatus,
    AuditPreflightLeaseEnvelope,
    AuditPreflightSecurityContext,
    AuditPreflightSourceExecutionTarget,
    AuditPreflightTarget,
    PreflightRequest,
    audit_preflight_is_exact_replay,
)
from riftx.domain.runner import RunnerPrincipal

NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
IMAGE_DIGEST = hashlib.sha256(b"image").hexdigest()
POLICY_DIGEST = hashlib.sha256(b"policy").hexdigest()
SCOPE_DIGEST = hashlib.sha256(b"scope").hexdigest()

PRINCIPAL = LocalPrincipal(
    id="operator-1",
    capabilities=frozenset(OperatorCapability),
)


class FakeAuthorizer:
    def __init__(self, *, scope_digest: str = SCOPE_DIGEST) -> None:
        self.scope_digest = scope_digest
        self.capabilities: list[OperatorCapability] = []

    def preflight_authorization_scope_digest(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> str:
        assert principal == PRINCIPAL
        self.capabilities.append(capability)
        return self.scope_digest


class InMemoryPreflightRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, AuditPreflightJob] = {}
        self.owner_reads = 0
        self.idempotency_reads = 0
        self.full_reads = 0
        self.create_calls = 0
        self.cas_calls = 0
        self.force_conflict_once = False
        self.advance_after_owner_read = False

    async def create(
        self,
        job: AuditPreflightJob,
    ) -> tuple[AuditPreflightJob, bool]:
        self.create_calls += 1
        existing = next(
            (
                value
                for value in self.jobs.values()
                if value.operator_principal_id == job.operator_principal_id
                and value.client_request_id == job.client_request_id
            ),
            None,
        )
        if existing is not None:
            if audit_preflight_is_exact_replay(
                existing,
                operator_principal_id=job.operator_principal_id,
                client_request_id=job.client_request_id,
                authorization_scope_digest=job.authorization_scope_digest,
                request_schema_version=job.request_schema_version,
                request_digest=job.request_digest,
            ):
                return existing, False
            raise RepositoryConflictError("idempotency drift")
        self.jobs[job.job_id] = job
        return job, True

    async def get_idempotency_binding(
        self,
        *,
        operator_principal_id: str,
        client_request_id: str,
    ) -> AuditPreflightOwnerBinding | None:
        self.idempotency_reads += 1
        job = next(
            (
                value
                for value in self.jobs.values()
                if value.operator_principal_id == operator_principal_id
                and value.client_request_id == client_request_id
            ),
            None,
        )
        return _owner_binding(job) if job is not None else None

    async def get_owner_binding(
        self,
        job_id: str,
    ) -> AuditPreflightOwnerBinding | None:
        self.owner_reads += 1
        job = self.jobs.get(job_id)
        if job is None:
            return None
        binding = _owner_binding(job)
        if self.advance_after_owner_read:
            self.advance_after_owner_read = False
            self.jobs[job_id] = _claimed_job(job)
        return binding

    async def get(self, job_id: str) -> AuditPreflightJob | None:
        self.full_reads += 1
        return self.jobs.get(job_id)

    async def compare_and_set(
        self,
        *,
        previous: AuditPreflightJob,
        updated: AuditPreflightJob,
        **_: object,
    ) -> AuditPreflightJob:
        self.cas_calls += 1
        current = self.jobs.get(previous.job_id)
        if self.force_conflict_once:
            self.force_conflict_once = False
            assert current is not None
            self.jobs[previous.job_id] = _claimed_job(current)
            raise RepositoryConflictError("claim won")
        if current != previous:
            raise RepositoryConflictError("stale state")
        self.jobs[updated.job_id] = updated
        return updated


def _owner_binding(job: AuditPreflightJob) -> AuditPreflightOwnerBinding:
    return AuditPreflightOwnerBinding(
        job_id=job.job_id,
        operator_principal_id=job.operator_principal_id,
        authorization_scope_digest=job.authorization_scope_digest,
        request_schema_version=job.request_schema_version,
        request_digest=job.request_digest,
        source_node_id=job.source_node_id,
        source_root_identity_digest=job.source_root_identity_digest,
        backend_id=job.backend_id,
        image_digest=job.image_digest,
        policy_digest=job.policy_digest,
        status=job.status,
        state_version=job.state_version,
        effect_owner_digest=job.effect_owner_digest,
    )


def _request(repository: Path, **updates: Any) -> PreflightRequest:
    payload: dict[str, Any] = {
        "client_request_id": "123e4567-e89b-42d3-a456-426614174000",
        "repository_path": str(repository),
        "source_execution_target": AuditPreflightSourceExecutionTarget(
            source_ingest_backend="linux_container"
        ),
        "target": AuditPreflightTarget(
            kind=SourceTargetKind.WORKING_TREE,
            revision="HEAD",
        ),
        "include_paths": ("src",),
        "exclude_paths": ("vendor",),
        "security_context": AuditPreflightSecurityContext(),
        "mode": AuditMode.STANDARD,
    }
    payload.update(updates)
    return PreflightRequest(**payload)


def _service(
    repository: InMemoryPreflightRepository,
    source_root: Path,
    **updates: Any,
) -> AuditPreflightApplicationService:
    identifiers = iter(f"preflight-job-{index}" for index in range(1, 20))
    values: dict[str, Any] = {
        "repository": repository,
        "feature_enabled": True,
        "source_roots": (source_root,),
        "backend_id": "linux_container",
        "image_digest": IMAGE_DIGEST,
        "policy_digest": POLICY_DIGEST,
        "source_ingest_available": True,
        "job_ttl_seconds": 900,
        "id_factory": lambda: next(identifiers),
        "clock": lambda: NOW,
    }
    values.update(updates)
    return AuditPreflightApplicationService(**values)


def _claimed_job(job: AuditPreflightJob) -> AuditPreflightJob:
    lease = AuditPreflightLeaseEnvelope(
        owner=job.effect_owner(),
        runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=1),
        lease_id="lease-1",
        lease_expires_at=NOW + timedelta(minutes=5),
        expected_state_version=job.state_version + 1,
        output_contract_digest=hashlib.sha256(b"output").hexdigest(),
    )
    payload = job.model_dump(mode="python")
    payload.update(
        status=AuditPreflightJobStatus.CLAIMED,
        state_version=job.state_version + 1,
        attempt=1,
        lease_id=lease.lease_id,
        lease_owner_instance_id=lease.runner_principal.instance_id,
        lease_owner_epoch=lease.runner_principal.epoch,
        lease_expires_at=lease.lease_expires_at,
        lease_expected_state_version=lease.expected_state_version,
        lease_output_contract_digest=lease.output_contract_digest,
        lease_envelope_digest=lease.lease_envelope_digest,
        capsule_id="capsule-1",
        updated_at=NOW,
    )
    return AuditPreflightJob.model_validate(payload)


@pytest.mark.asyncio
async def test_create_is_exactly_idempotent_and_drift_conflicts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    repository_path = source / "repository"
    repository_path.mkdir(parents=True)
    repository = InMemoryPreflightRepository()
    service = _service(repository, source)
    authorizer = FakeAuthorizer()
    request = _request(repository_path)

    first = await service.create_authorized(
        request,
        principal=PRINCIPAL,
        authorizer=authorizer,
    )
    replay = await service.create_authorized(
        request,
        principal=PRINCIPAL,
        authorizer=authorizer,
    )

    assert first.created is True
    assert first.replayed is False
    assert replay.created is False
    assert replay.replayed is True
    assert replay.job == first.job
    assert first.job.status is AuditPreflightJobStatus.PENDING
    assert first.job.restricted_request_json == request.canonical_json()
    assert authorizer.capabilities == [
        OperatorCapability.HOST_EXECUTE,
        OperatorCapability.HOST_EXECUTE,
    ]

    changed = _request(repository_path, include_paths=("different",))
    with pytest.raises(ApplicationConflictError) as captured:
        await service.create_authorized(
            changed,
            principal=PRINCIPAL,
            authorizer=authorizer,
        )
    assert captured.value.code == "audit_preflight_idempotency_conflict"


@pytest.mark.asyncio
async def test_exact_replay_and_drift_resolve_before_backend_or_source_io(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    repository_path = source / "repository"
    repository_path.mkdir(parents=True)
    repository = InMemoryPreflightRepository()
    request = _request(repository_path)
    created = await _service(repository, source).create_authorized(
        request,
        principal=PRINCIPAL,
        authorizer=FakeAuthorizer(),
    )
    repository_path.rmdir()
    calls: list[str] = []

    def unavailable_check() -> bool:
        calls.append("availability")
        return False

    def forbidden_opener(*_: object, **__: object) -> None:
        calls.append("source")
        raise AssertionError("idempotency resolution must precede source I/O")

    replay_service = _service(
        repository,
        source,
        source_ingest_available=unavailable_check,
        source_opener=forbidden_opener,
    )
    replay = await replay_service.create_authorized(
        request,
        principal=PRINCIPAL,
        authorizer=FakeAuthorizer(),
    )
    with pytest.raises(ApplicationConflictError) as captured:
        await replay_service.create_authorized(
            _request(repository_path, include_paths=("different",)),
            principal=PRINCIPAL,
            authorizer=FakeAuthorizer(),
        )

    assert replay.created is False
    assert replay.job == created.job
    assert captured.value.code == "audit_preflight_idempotency_conflict"
    assert calls == []
    assert repository.create_calls == 1


@pytest.mark.asyncio
async def test_disabled_create_fails_before_availability_source_or_persistence(
    tmp_path: Path,
) -> None:
    repository = InMemoryPreflightRepository()
    calls: list[str] = []

    def unavailable_check() -> bool:
        calls.append("availability")
        return False

    def forbidden_opener(*_: object, **__: object) -> None:
        calls.append("source")
        raise AssertionError("source opener must not run")

    service = _service(
        repository,
        tmp_path,
        feature_enabled=False,
        source_ingest_available=unavailable_check,
        source_opener=forbidden_opener,
    )
    authorizer = FakeAuthorizer()

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.create_authorized(
            object(),  # type: ignore[arg-type]
            principal=PRINCIPAL,
            authorizer=authorizer,
        )

    assert captured.value.code == "audit_feature_disabled"
    assert authorizer.capabilities == [OperatorCapability.HOST_EXECUTE]
    assert calls == []
    assert repository.create_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image_digest", "available"),
    [(None, True), (IMAGE_DIGEST, False)],
)
async def test_source_ingest_availability_is_fail_closed_before_source_io(
    tmp_path: Path,
    image_digest: str | None,
    available: bool,
) -> None:
    repository = InMemoryPreflightRepository()
    source = tmp_path / "source"
    repository_path = source / "repository"
    repository_path.mkdir(parents=True)
    opened = False

    def forbidden_opener(*_: object, **__: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("unavailable backend must not touch source")

    service = _service(
        repository,
        source,
        image_digest=image_digest,
        source_ingest_available=available,
        source_opener=forbidden_opener,
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.create_authorized(
            _request(repository_path),
            principal=PRINCIPAL,
            authorizer=FakeAuthorizer(),
        )

    assert captured.value.code == "audit_sandbox_unavailable"
    assert opened is False
    assert repository.create_calls == 0


@pytest.mark.asyncio
async def test_source_outside_allowed_root_is_path_free_conflict(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside" / "repository"
    allowed.mkdir()
    outside.mkdir(parents=True)
    service = _service(InMemoryPreflightRepository(), allowed)

    with pytest.raises(ApplicationConflictError) as captured:
        await service.create_authorized(
            _request(outside),
            principal=PRINCIPAL,
            authorizer=FakeAuthorizer(),
        )

    assert captured.value.code == "audit_source_not_allowed"
    assert str(outside) not in captured.value.message


@pytest.mark.asyncio
async def test_get_checks_bounded_owner_before_loading_restricted_job(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    repository_path = source / "repository"
    repository_path.mkdir(parents=True)
    repository = InMemoryPreflightRepository()
    service = _service(repository, source)
    created = await service.create_authorized(
        _request(repository_path),
        principal=PRINCIPAL,
        authorizer=FakeAuthorizer(),
    )
    repository.full_reads = 0

    with pytest.raises(ResourceNotAccessibleError) as captured:
        await service.get_authorized(
            created.job.job_id,
            principal=PRINCIPAL,
            authorizer=FakeAuthorizer(scope_digest=hashlib.sha256(b"other").hexdigest()),
        )

    assert captured.value.code == "resource_not_accessible"
    assert repository.owner_reads == 1
    assert repository.full_reads == 0


@pytest.mark.asyncio
async def test_get_accepts_state_progress_after_immutable_owner_authorization(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    repository_path = source / "repository"
    repository_path.mkdir(parents=True)
    repository = InMemoryPreflightRepository()
    service = _service(repository, source)
    created = await service.create_authorized(
        _request(repository_path),
        principal=PRINCIPAL,
        authorizer=FakeAuthorizer(),
    )
    repository.advance_after_owner_read = True

    loaded = await service.get_authorized(
        created.job.job_id,
        principal=PRINCIPAL,
        authorizer=FakeAuthorizer(),
    )

    assert loaded.status is AuditPreflightJobStatus.CLAIMED
    assert loaded.state_version == 2


@pytest.mark.asyncio
async def test_pending_cancel_is_idempotent_and_carries_never_created_proof(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    repository_path = source / "repository"
    repository_path.mkdir(parents=True)
    repository = InMemoryPreflightRepository()
    service = _service(repository, source)
    created = await service.create_authorized(
        _request(repository_path),
        principal=PRINCIPAL,
        authorizer=FakeAuthorizer(),
    )
    authorizer = FakeAuthorizer()

    cancelled = await service.cancel_authorized(
        created.job.job_id,
        principal=PRINCIPAL,
        authorizer=authorizer,
    )
    replay = await service.cancel_authorized(
        created.job.job_id,
        principal=PRINCIPAL,
        authorizer=authorizer,
    )

    assert cancelled.status is AuditPreflightJobStatus.CANCELLED
    assert cancelled.state_version == 2
    assert cancelled.never_created_proof_digest is not None
    assert cancelled.stop_receipt_digest is None
    assert cancelled.finished_at == NOW
    assert replay == cancelled
    assert repository.cas_calls == 1
    assert authorizer.capabilities == [
        OperatorCapability.HOST_CONTROL,
        OperatorCapability.HOST_CONTROL,
    ]


@pytest.mark.asyncio
async def test_claim_winning_cancel_race_converges_to_cancelling(tmp_path: Path) -> None:
    source = tmp_path / "source"
    repository_path = source / "repository"
    repository_path.mkdir(parents=True)
    repository = InMemoryPreflightRepository()
    service = _service(repository, source)
    created = await service.create_authorized(
        _request(repository_path),
        principal=PRINCIPAL,
        authorizer=FakeAuthorizer(),
    )
    repository.force_conflict_once = True

    cancelling = await service.cancel_authorized(
        created.job.job_id,
        principal=PRINCIPAL,
        authorizer=FakeAuthorizer(),
    )

    assert cancelling.status is AuditPreflightJobStatus.CANCELLING
    assert cancelling.state_version == 3
    assert cancelling.attempt == 1
    assert cancelling.capsule_id == "capsule-1"
    assert cancelling.never_created_proof_digest is None
    assert repository.cas_calls == 2


def test_constructor_rejects_cross_node_or_unpinned_policy() -> None:
    repository = InMemoryPreflightRepository()
    with pytest.raises(ValueError, match="same-node"):
        AuditPreflightApplicationService(
            repository=repository,
            feature_enabled=True,
            source_roots=("/source",),
            backend_id="linux_container",
            image_digest=IMAGE_DIGEST,
            policy_digest=POLICY_DIGEST,
            node_mode="remote",
        )
    with pytest.raises(ValueError, match="image_digest"):
        AuditPreflightApplicationService(
            repository=repository,
            feature_enabled=True,
            source_roots=("/source",),
            backend_id="linux_container",
            image_digest="latest",
            policy_digest=POLICY_DIGEST,
        )
