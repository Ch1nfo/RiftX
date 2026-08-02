from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.domain.test_audit_domain import (
    _contract as _domain_contract,
)
from tests.unit.domain.test_audit_domain import (
    _converge_cleanup,
    _running_scan,
    _started_running_scan,
)
from tests.unit.domain.test_audit_domain import (
    _record as _domain_record,
)
from tests.unit.domain.test_audit_domain import (
    _scan as _domain_scan,
)

from riftx.application.errors import (
    ApplicationConflictError,
    AuditIdempotencyConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryUnavailableError,
    ServiceUnavailableError,
)
from riftx.application.ports import (
    AuditAggregate,
    AuditDraftAggregateFactory,
    AuditDraftCreationEnvelope,
    StoredAuditEntity,
)
from riftx.application.services.audits import (
    AuditApplicationService,
    AuditContractBlueprint,
    AuditControlAction,
    AuditControlDisposition,
    AuditControlEffect,
    CreateAuditDraft,
)
from riftx.domain import (
    ApprovalMode,
    AuditClientRequest,
    AuditClosureStatus,
    AuditContract,
    AuditLifecycleStatus,
    AuditMode,
    AuditProject,
    AuditPublicationStatus,
    AuditScan,
    AuditTerminalOutcome,
    Engagement,
    Objective,
    Run,
    RunKind,
    RunStatus,
    Scope,
)

NOW = datetime(2026, 8, 3, 8, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
WORKSPACE_ROOT = Path("/var/lib/riftx/code-audits")
SOURCE_PATH = "/srv/authorized/repository"
CLIENT_REQUEST_ID = "6ed6232a-3fb3-4f93-868f-0be291142f31"


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _replace_contract(contract: AuditContract, **updates: object) -> AuditContract:
    payload = contract.model_dump(mode="python")
    payload.update(updates)
    return AuditContract.model_validate(payload)


def _command(
    *,
    client_request_id: str = CLIENT_REQUEST_ID,
    project_name: str = "RiftX",
    repository_identity_digest: str | None = None,
    authorization_reference: str | None = None,
    engagement_id: str | None = None,
    default_branch: str | None = "main",
    contract: AuditContract | None = None,
) -> CreateAuditDraft:
    return CreateAuditDraft(
        client_request_id=client_request_id,
        project_name=project_name,
        repository_identity_digest=repository_identity_digest or _digest("repository"),
        authorization_reference=authorization_reference or _digest("authorization"),
        engagement_id=engagement_id,
        default_branch=default_branch,
        contract=AuditContractBlueprint.from_contract(contract or _domain_contract()),
    )


class DeterministicIds:
    def __init__(self, prefix: str = "generated") -> None:
        self.prefix = prefix
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"{self.prefix}-{self.calls}"


def _aggregate_from_envelope(
    envelope: AuditDraftCreationEnvelope,
    *,
    audit_state_version: int = 1,
) -> AuditAggregate:
    return AuditAggregate(
        audit=StoredAuditEntity(envelope.audit, state_version=audit_state_version),
        contract=StoredAuditEntity(envelope.contract, state_version=1),
        project=StoredAuditEntity(envelope.project, state_version=1),
        run=envelope.run,
        engagement=envelope.engagement,
        client_request=envelope.client_request,
    )


class FakeCreationUnitOfWork:
    def __init__(
        self,
        *,
        created: bool = True,
        aggregate: AuditAggregate | None = None,
        error: Exception | None = None,
    ) -> None:
        self.created = created
        self.aggregate = aggregate
        self.error = error
        self.calls: list[AuditDraftAggregateFactory] = []
        self.envelopes: list[AuditDraftCreationEnvelope] = []

    async def create_draft(
        self,
        factory: AuditDraftAggregateFactory,
    ) -> tuple[AuditAggregate, bool]:
        self.calls.append(factory)
        if self.error is not None:
            raise self.error
        if self.aggregate is not None:
            return self.aggregate, self.created
        engagement = factory.build_engagement()
        project = factory.build_project(engagement)
        envelope = factory.build(project, engagement)
        self.envelopes.append(envelope)
        return _aggregate_from_envelope(envelope), self.created


class FakeAggregateRepository:
    def __init__(
        self,
        items: Sequence[AuditAggregate] = (),
        *,
        list_result: Sequence[AuditAggregate] | None = None,
    ) -> None:
        self.items = {item.audit.value.id: item for item in items}
        self.list_result = tuple(items if list_result is None else list_result)
        self.get_calls: list[tuple[str, str | None, str | None]] = []
        self.list_calls: list[dict[str, object]] = []

    async def get(
        self,
        audit_id: str,
        *,
        project_id: str | None = None,
        engagement_id: str | None = None,
    ) -> AuditAggregate | None:
        self.get_calls.append((audit_id, project_id, engagement_id))
        return self.items.get(audit_id)

    async def list(
        self,
        *,
        run_id: str | None = None,
        project_id: str | None = None,
        engagement_id: str | None = None,
        lifecycle_status: AuditLifecycleStatus | None = None,
        mode: AuditMode | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AuditAggregate]:
        self.list_calls.append(
            {
                "run_id": run_id,
                "project_id": project_id,
                "engagement_id": engagement_id,
                "lifecycle_status": lifecycle_status,
                "mode": mode,
                "created_from": created_from,
                "created_to": created_to,
                "limit": limit,
                "offset": offset,
            }
        )
        return self.list_result


def _service(
    *,
    uow: FakeCreationUnitOfWork | None = None,
    repository: FakeAggregateRepository | None = None,
    enabled: bool = True,
    workspace_root: Path = WORKSPACE_ROOT,
    id_factory: Callable[[], str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AuditApplicationService:
    return AuditApplicationService(
        creation_uow=uow if uow is not None else FakeCreationUnitOfWork(),
        aggregate_repository=(repository if repository is not None else FakeAggregateRepository()),
        feature_enabled=enabled,
        workspace_root=workspace_root,
        id_factory=id_factory or DeterministicIds(),
        clock=clock or (lambda: NOW),
    )


def _run_status_for(scan: AuditScan) -> RunStatus:
    direct = {
        AuditLifecycleStatus.DRAFT: RunStatus.CREATED,
        AuditLifecycleStatus.QUEUED: RunStatus.PREPARING,
        AuditLifecycleStatus.PREFLIGHTING: RunStatus.PREPARING,
        AuditLifecycleStatus.SNAPSHOTTING: RunStatus.PREPARING,
        AuditLifecycleStatus.RUNNING: RunStatus.RUNNING,
        AuditLifecycleStatus.WAITING_APPROVAL: RunStatus.WAITING_APPROVAL,
        AuditLifecycleStatus.PAUSING: RunStatus.PAUSING,
        AuditLifecycleStatus.PAUSED: RunStatus.PAUSED,
        AuditLifecycleStatus.FINALIZING: RunStatus.COMPLETING,
        AuditLifecycleStatus.CANCELLING: RunStatus.CANCELLING,
        AuditLifecycleStatus.FAILING: RunStatus.COMPLETING,
        AuditLifecycleStatus.COMPLETED: RunStatus.COMPLETED,
        AuditLifecycleStatus.COMPLETED_PARTIAL: RunStatus.COMPLETED,
        AuditLifecycleStatus.FAILED: RunStatus.FAILED,
        AuditLifecycleStatus.CANCELLED: RunStatus.CANCELLED,
    }
    if scan.lifecycle_status in direct:
        return direct[scan.lifecycle_status]
    if scan.lifecycle_status is AuditLifecycleStatus.CLEANING:
        if scan.cleanup_proof_digest is not None:
            assert scan.run_terminal_status is not None
            return scan.run_terminal_status
        return (
            RunStatus.CANCELLING
            if scan.terminal_outcome is AuditTerminalOutcome.CANCELLED
            else RunStatus.COMPLETING
        )
    assert scan.terminal_outcome is not None
    return {
        AuditTerminalOutcome.COMPLETE: RunStatus.COMPLETED,
        AuditTerminalOutcome.PARTIAL: RunStatus.COMPLETED,
        AuditTerminalOutcome.FAILED: RunStatus.FAILED,
        AuditTerminalOutcome.CANCELLED: RunStatus.CANCELLED,
    }[scan.terminal_outcome]


def _aggregate_for_scan(
    scan: AuditScan,
    *,
    state_version: int = 7,
    run_status: RunStatus | None = None,
    run_kind: RunKind = RunKind.CODE_AUDIT,
    workflow_id: str | None = None,
) -> AuditAggregate:
    contract = _domain_record()
    engagement = Engagement(
        id="engagement-1",
        name="RiftX Code Audit",
        authorization_reference=_digest("authorization"),
        created_at=NOW,
        updated_at=NOW,
    )
    project = AuditProject(
        id=scan.project_id,
        engagement_id=engagement.id,
        display_name="RiftX",
        repository_identity_digest=_digest("repository"),
        default_branch="main",
        created_at=NOW,
        updated_at=NOW,
    )
    run = Run(
        id=scan.run_id,
        engagement_id=engagement.id,
        node_id=scan.selected_node_id,
        kind=run_kind,
        objective=Objective(description="Code Audit: RiftX"),
        scope=Scope(),
        status=run_status or _run_status_for(scan),
        approval_mode=ApprovalMode.MANUAL,
        model_profile=scan.model_profile,
        workspace_path=str(WORKSPACE_ROOT / scan.id),
        temporal_workflow_id=workflow_id or scan.temporal_workflow_id,
        created_at=NOW,
    )
    request = AuditClientRequest(
        client_request_id=CLIENT_REQUEST_ID,
        request_digest=_digest("request"),
        audit_id=scan.id,
        run_id=scan.run_id,
        project_id=scan.project_id,
        engagement_id=engagement.id,
        contract_id=contract.contract_id,
        contract_digest=contract.contract_digest,
        temporal_workflow_id=scan.temporal_workflow_id,
        created_at=NOW,
    )
    return AuditAggregate(
        audit=StoredAuditEntity(scan, state_version=state_version),
        contract=StoredAuditEntity(contract, state_version=3),
        project=StoredAuditEntity(project, state_version=2),
        run=run,
        engagement=engagement,
        client_request=request,
    )


def _complete_publication_path() -> dict[AuditLifecycleStatus, AuditScan]:
    scan = _running_scan().transition_to(
        AuditLifecycleStatus.FINALIZING,
        terminal_outcome=AuditTerminalOutcome.COMPLETE,
    )
    finalizing = scan
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    cleaning = scan
    scan = _converge_cleanup(scan)
    scan = scan.record_closure(AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE)
    scan = scan.transition_to(AuditLifecycleStatus.SEALING_CORE, at=NOW + timedelta(minutes=1))
    sealing = scan
    scan = scan.record_core_seal(
        core_seal_root=_digest("core-seal"),
        at=NOW + timedelta(minutes=2),
    )
    scan = scan.transition_to(AuditLifecycleStatus.REPORTING)
    reporting = scan
    scan = scan.transition_to(AuditLifecycleStatus.PACKAGING)
    packaging = scan
    scan = scan.record_distribution_revision(
        revision_id="distribution-1",
        at=NOW + timedelta(minutes=3),
    )
    completed = scan.transition_to(AuditLifecycleStatus.COMPLETED)
    return {
        AuditLifecycleStatus.FINALIZING: finalizing,
        AuditLifecycleStatus.CLEANING: cleaning,
        AuditLifecycleStatus.SEALING_CORE: sealing,
        AuditLifecycleStatus.REPORTING: reporting,
        AuditLifecycleStatus.PACKAGING: packaging,
        AuditLifecycleStatus.COMPLETED: completed,
    }


def _failed_terminal_scan() -> AuditScan:
    scan = _domain_scan().transition_to(AuditLifecycleStatus.FAILING)
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    scan = _converge_cleanup(scan)
    scan = scan.record_closure(AuditClosureStatus.FAILED)
    scan = scan.transition_to(AuditLifecycleStatus.SEALING_CORE, at=NOW + timedelta(minutes=1))
    scan = scan.record_publication_failure(AuditPublicationStatus.SEAL_FAILED)
    return scan.transition_to(AuditLifecycleStatus.FAILED)


def _cancelled_terminal_scan() -> AuditScan:
    scan = _domain_scan().transition_to(AuditLifecycleStatus.CANCELLING)
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    scan = _converge_cleanup(scan)
    scan = scan.record_closure(AuditClosureStatus.CANCELLED)
    scan = scan.transition_to(AuditLifecycleStatus.SEALING_CORE, at=NOW + timedelta(minutes=1))
    scan = scan.record_publication_failure(AuditPublicationStatus.SEAL_FAILED)
    return scan.transition_to(AuditLifecycleStatus.CANCELLED)


def _completed_partial_scan() -> AuditScan:
    scan = _complete_publication_path()[AuditLifecycleStatus.REPORTING]
    scan = scan.record_publication_failure(AuditPublicationStatus.REPORT_FAILED)
    return scan.transition_to(AuditLifecycleStatus.COMPLETED_PARTIAL)


def _scan_for(status: AuditLifecycleStatus) -> AuditScan:
    draft = _domain_scan()
    queued = draft.transition_to(AuditLifecycleStatus.QUEUED, at=NOW)
    preflighting = queued.transition_to(AuditLifecycleStatus.PREFLIGHTING)
    snapshotting = preflighting.transition_to(AuditLifecycleStatus.SNAPSHOTTING)
    simple = {
        AuditLifecycleStatus.DRAFT: draft,
        AuditLifecycleStatus.QUEUED: queued,
        AuditLifecycleStatus.PREFLIGHTING: preflighting,
        AuditLifecycleStatus.SNAPSHOTTING: snapshotting,
        AuditLifecycleStatus.RUNNING: _started_running_scan(),
        AuditLifecycleStatus.WAITING_APPROVAL: _started_running_scan().transition_to(
            AuditLifecycleStatus.WAITING_APPROVAL
        ),
        AuditLifecycleStatus.PAUSING: _started_running_scan().transition_to(
            AuditLifecycleStatus.PAUSING
        ),
        AuditLifecycleStatus.PAUSED: _started_running_scan()
        .transition_to(AuditLifecycleStatus.PAUSING)
        .transition_to(AuditLifecycleStatus.PAUSED),
        AuditLifecycleStatus.CANCELLING: draft.transition_to(AuditLifecycleStatus.CANCELLING),
        AuditLifecycleStatus.FAILING: draft.transition_to(AuditLifecycleStatus.FAILING),
        AuditLifecycleStatus.CANCELLED: _cancelled_terminal_scan(),
        AuditLifecycleStatus.FAILED: _failed_terminal_scan(),
        AuditLifecycleStatus.COMPLETED_PARTIAL: _completed_partial_scan(),
    }
    if status in simple:
        return simple[status]
    return _complete_publication_path()[status]


def _read_snapshot(aggregate: AuditAggregate) -> tuple[str, str, int]:
    return (
        aggregate.audit.value.model_dump_json(),
        aggregate.run.model_dump_json(),
        aggregate.audit.state_version,
    )


def test_service_rejects_non_absolute_or_non_normalized_workspace_root() -> None:
    for invalid in (Path("relative/audits"), Path("/var/lib/../audits")):
        with pytest.raises(ValueError, match="absolute normalized"):
            _service(workspace_root=invalid)


async def test_feature_flag_precedes_create_validation_and_uow_access() -> None:
    uow = FakeCreationUnitOfWork(aggregate=_aggregate_for_scan(_domain_scan()), created=False)
    repository = FakeAggregateRepository()
    service = _service(uow=uow, repository=repository, enabled=False)

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.create_draft(object())  # type: ignore[arg-type]

    assert captured.value.code == "feature_disabled"
    assert uow.calls == []
    assert repository.get_calls == []
    assert repository.list_calls == []


async def test_create_draft_builds_the_bound_safe_code_audit_envelope() -> None:
    ids = DeterministicIds("created")
    uow = FakeCreationUnitOfWork()
    service = _service(uow=uow, id_factory=ids)
    command = _command()

    result = await service.create_draft(command)

    assert result.created is True
    assert len(uow.calls) == 1
    assert len(uow.envelopes) == 1
    envelope = uow.envelopes[0]
    aggregate = result.aggregate
    assert aggregate.audit.value == envelope.audit
    assert aggregate.run == envelope.run
    assert aggregate.project.value == envelope.project
    assert aggregate.engagement == envelope.engagement

    scan = envelope.audit
    run = envelope.run
    contract = envelope.contract.contract()
    assert run.kind is RunKind.CODE_AUDIT
    assert run.status is RunStatus.CREATED
    assert run.approval_mode is ApprovalMode.MANUAL
    assert run.engagement_id == envelope.engagement.id == envelope.project.engagement_id
    assert run.node_id == scan.selected_node_id
    assert run.model_profile == scan.model_profile
    assert run.temporal_workflow_id == scan.temporal_workflow_id
    assert run.temporal_workflow_id == f"riftx-code-audit-{scan.id}"
    assert run.workspace_path == str(WORKSPACE_ROOT / scan.id)
    assert run.objective.description == "Code Audit: RiftX"
    assert contract.audit_id == scan.id
    assert contract.project_id == envelope.project.id
    assert (contract.audit_id, contract.project_id) != ("audit-1", "project-1")

    assert envelope.run_created_event.sequence == 1
    assert envelope.run_created_event.event_type == "run.created"
    assert envelope.audit_created_event.sequence == 2
    assert envelope.audit_created_event.event_type == "audit.created"
    assert envelope.run_created_event.run_id == envelope.audit_created_event.run_id == run.id
    assert envelope.client_request.request_digest == uow.calls[0].request_digest
    assert envelope.client_request.audit_id == scan.id
    assert envelope.client_request.contract_digest == envelope.contract.contract_digest

    safe_projection = "\n".join(
        (
            run.objective.model_dump_json(),
            envelope.run_created_event.model_dump_json(),
            envelope.audit_created_event.model_dump_json(),
            envelope.client_request.model_dump_json(),
            repr(aggregate),
        )
    )
    assert SOURCE_PATH not in safe_projection
    assert envelope.contract.canonical_contract_json not in safe_projection


async def test_maximum_project_name_fits_both_project_and_engagement_records() -> None:
    project_name = "P" * 255
    uow = FakeCreationUnitOfWork()

    result = await _service(uow=uow).create_draft(_command(project_name=project_name))

    assert result.aggregate.project.value.display_name == project_name
    assert result.aggregate.engagement.name == project_name


async def test_exact_replay_does_not_generate_ids_or_time_and_returns_current_state() -> None:
    current = _aggregate_for_scan(_scan_for(AuditLifecycleStatus.RUNNING), state_version=19)
    uow = FakeCreationUnitOfWork(created=False, aggregate=current)

    def forbidden() -> str:
        raise AssertionError("exact replay must not generate IDs")

    def forbidden_clock() -> datetime:
        raise AssertionError("exact replay must not sample creation time")

    service = _service(uow=uow, id_factory=forbidden, clock=forbidden_clock)

    result = await service.create_draft(_command())

    assert result.created is False
    assert result.aggregate is current
    assert result.aggregate.audit.value.lifecycle_status is AuditLifecycleStatus.RUNNING
    assert result.aggregate.audit.state_version == 19
    assert uow.envelopes == []


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (AuditIdempotencyConflictError("different payload"), "audit_idempotency_conflict"),
        (RepositoryConflictError("owner collision"), "audit_creation_conflict"),
    ],
)
async def test_create_draft_translates_repository_conflicts_without_exposing_causes(
    error: Exception,
    expected_code: str,
) -> None:
    service = _service(uow=FakeCreationUnitOfWork(error=error))

    with pytest.raises(ApplicationConflictError) as captured:
        await service.create_draft(_command())

    assert captured.value.code == expected_code
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True
    assert SOURCE_PATH not in str(captured.value)


async def test_create_draft_translates_sanitized_persistence_failure() -> None:
    service = _service(
        uow=FakeCreationUnitOfWork(
            error=RepositoryUnavailableError("Code Audit persistence operation failed")
        )
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.create_draft(_command())

    assert captured.value.code == "audit_persistence_unavailable"
    assert SOURCE_PATH not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "command",
    [
        replace(_command(), client_request_id="not-a-uuid"),
        replace(_command(), project_name=" RiftX"),
        replace(_command(), repository_identity_digest="A" * 64),
        replace(_command(), authorization_reference="short"),
        replace(_command(), engagement_id=" engagement-1"),
        replace(_command(), default_branch="main\nsecret"),
    ],
)
async def test_invalid_create_request_is_rejected_before_uow(command: CreateAuditDraft) -> None:
    uow = FakeCreationUnitOfWork()
    service = _service(uow=uow)

    with pytest.raises(ApplicationConflictError) as captured:
        await service.create_draft(command)

    assert captured.value.code == "audit_contract_invalid"
    assert uow.calls == []


async def test_source_and_workspace_overlap_is_rejected_without_filesystem_or_uow_access() -> None:
    uow = FakeCreationUnitOfWork()
    service = _service(uow=uow, workspace_root=Path("/srv/authorized"))

    with pytest.raises(ApplicationConflictError) as captured:
        await service.create_draft(_command())

    assert captured.value.code == "audit_contract_invalid"
    assert SOURCE_PATH not in str(captured.value)
    assert uow.calls == []


async def _request_digest_for(
    command: CreateAuditDraft,
    *,
    id_prefix: str,
    now: datetime,
) -> str:
    uow = FakeCreationUnitOfWork()
    result = await _service(
        uow=uow,
        id_factory=DeterministicIds(id_prefix),
        clock=lambda: now,
    ).create_draft(command)
    assert result.aggregate.client_request.request_digest == uow.calls[0].request_digest
    return result.aggregate.client_request.request_digest


async def test_request_digest_excludes_request_key_candidate_ids_generated_ids_and_time() -> None:
    original = _command()
    candidate_rebound = _replace_contract(
        original.contract.template,
        audit_id="caller-candidate-audit",
        project_id="caller-candidate-project",
    )
    semantically_equal = replace(
        original,
        client_request_id="086ab9d1-92ef-476c-8649-0bf2f5205f50",
        contract=AuditContractBlueprint.from_contract(candidate_rebound),
    )

    first = await _request_digest_for(original, id_prefix="first", now=NOW)
    second = await _request_digest_for(semantically_equal, id_prefix="second", now=LATER)

    assert first == second
    assert len(first) == 64
    assert first == first.lower()


async def test_request_digest_is_sensitive_to_each_caller_owned_top_level_dimension() -> None:
    base = _command()
    changed_contract = _replace_contract(base.contract.template, config_digest=_digest("changed"))
    variants = (
        replace(base, project_name="RiftX Enterprise"),
        replace(base, repository_identity_digest=_digest("different repository")),
        replace(base, authorization_reference=_digest("different authorization")),
        replace(base, engagement_id="engagement-explicit"),
        replace(base, default_branch="release/3.0"),
        replace(base, contract=AuditContractBlueprint.from_contract(changed_contract)),
    )
    base_digest = await _request_digest_for(base, id_prefix="base", now=NOW)

    variant_digests = [
        await _request_digest_for(command, id_prefix=f"variant-{index}", now=LATER)
        for index, command in enumerate(variants)
    ]

    assert all(digest != base_digest for digest in variant_digests)
    assert len(set(variant_digests)) == len(variant_digests)


async def test_get_passes_owner_scope_and_missing_audits_are_not_found() -> None:
    aggregate = _aggregate_for_scan(_domain_scan())
    repository = FakeAggregateRepository((aggregate,))
    service = _service(repository=repository)

    result = await service.get(
        aggregate.audit.value.id,
        project_id="project-1",
        engagement_id="engagement-1",
    )

    assert result is aggregate
    assert repository.get_calls == [("audit-1", "project-1", "engagement-1")]

    with pytest.raises(EntityNotFoundError) as captured:
        await service.get("audit-missing", project_id="project-1")
    assert captured.value.entity == "Audit"
    assert captured.value.entity_id == "audit-missing"


async def test_list_passes_every_filter_and_bounded_page_argument() -> None:
    aggregate = _aggregate_for_scan(_domain_scan())
    repository = FakeAggregateRepository(list_result=(aggregate,))
    service = _service(repository=repository)

    result = await service.list(
        run_id="run-1",
        project_id="project-1",
        engagement_id="engagement-1",
        lifecycle_status=AuditLifecycleStatus.DRAFT,
        mode=AuditMode.STANDARD,
        created_from=NOW,
        created_to=LATER,
        limit=200,
        offset=40,
    )

    assert result == (aggregate,)
    assert repository.list_calls == [
        {
            "run_id": "run-1",
            "project_id": "project-1",
            "engagement_id": "engagement-1",
            "lifecycle_status": AuditLifecycleStatus.DRAFT,
            "mode": AuditMode.STANDARD,
            "created_from": NOW,
            "created_to": LATER,
            "limit": 200,
            "offset": 40,
        }
    ]


@pytest.mark.parametrize(
    ("limit", "offset"),
    [(0, 0), (201, 0), (True, 0), (1.5, 0), (50, -1), (50, True), (50, 1.5)],
)
async def test_list_rejects_invalid_pages_before_repository_access(
    limit: object,
    offset: object,
) -> None:
    repository = FakeAggregateRepository()
    service = _service(repository=repository)

    with pytest.raises(ValueError):
        await service.list(limit=limit, offset=offset)  # type: ignore[arg-type]

    assert repository.list_calls == []


@pytest.mark.parametrize(
    ("run_kind", "run_status", "workflow_id"),
    [
        (RunKind.GENERAL, RunStatus.CREATED, "riftx-code-audit-audit-1"),
        (RunKind.CODE_AUDIT, RunStatus.RUNNING, "riftx-code-audit-audit-1"),
        (RunKind.CODE_AUDIT, RunStatus.CREATED, "riftx-code-audit-other"),
    ],
)
async def test_get_rejects_run_kind_state_or_workflow_projection_conflicts(
    run_kind: RunKind,
    run_status: RunStatus,
    workflow_id: str,
) -> None:
    aggregate = _aggregate_for_scan(
        _domain_scan(),
        run_kind=run_kind,
        run_status=run_status,
        workflow_id=workflow_id,
    )
    service = _service(repository=FakeAggregateRepository((aggregate,)))

    with pytest.raises(ApplicationConflictError) as captured:
        await service.get("audit-1")

    assert captured.value.code == "audit_run_state_conflict"
    assert captured.value.details == {
        "audit_id": "audit-1",
        "lifecycle_status": "draft",
        "run_id": "run-1",
        "run_status": run_status.value,
    }


@pytest.mark.parametrize("status", tuple(AuditLifecycleStatus))
async def test_get_rejects_an_unmapped_run_status_for_every_audit_lifecycle(
    status: AuditLifecycleStatus,
) -> None:
    aggregate = _aggregate_for_scan(
        _scan_for(status),
        run_status=RunStatus.WAITING_USER,
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await _service(repository=FakeAggregateRepository((aggregate,))).get("audit-1")

    assert captured.value.code == "audit_run_state_conflict"
    assert captured.value.details["lifecycle_status"] == status.value
    assert captured.value.details["run_status"] == RunStatus.WAITING_USER.value


async def test_list_fails_closed_when_any_aggregate_has_a_projection_conflict() -> None:
    valid = _aggregate_for_scan(_domain_scan())
    corrupted = _aggregate_for_scan(_domain_scan(), run_status=RunStatus.RUNNING)
    repository = FakeAggregateRepository(list_result=(valid, corrupted))

    with pytest.raises(ApplicationConflictError) as captured:
        await _service(repository=repository).list()

    assert captured.value.code == "audit_run_state_conflict"
    assert repository.list_calls[0]["limit"] == 50
    assert repository.list_calls[0]["offset"] == 0


async def test_disabled_feature_keeps_reads_pause_and_cancel_but_blocks_resume_flag_first() -> None:
    draft = _aggregate_for_scan(_scan_for(AuditLifecycleStatus.DRAFT))
    paused = _aggregate_for_scan(_scan_for(AuditLifecycleStatus.PAUSED))
    repository = FakeAggregateRepository((draft,), list_result=(draft, paused))
    uow = FakeCreationUnitOfWork()
    service = _service(uow=uow, repository=repository, enabled=False)

    assert await service.get(draft.audit.value.id) is draft
    assert await service.list() == (draft, paused)
    assert (await service.cancel(draft.audit.value.id)).disposition is (
        AuditControlDisposition.TRANSITION
    )
    repository.items[paused.audit.value.id] = paused
    assert (await service.pause(paused.audit.value.id)).disposition is (
        AuditControlDisposition.ALREADY_SATISFIED
    )

    reads_before_resume = list(repository.get_calls)
    with pytest.raises(ServiceUnavailableError) as captured:
        await service.resume(paused.audit.value.id)
    assert captured.value.code == "feature_disabled"
    assert repository.get_calls == reads_before_resume
    assert uow.calls == []


@pytest.mark.parametrize(
    ("status", "disposition", "effect", "reason_code", "target_audit", "target_run"),
    [
        (
            AuditLifecycleStatus.RUNNING,
            AuditControlDisposition.TRANSITION,
            AuditControlEffect.PAUSE_WORKFLOW_THEN_PROJECT,
            "audit_pause_requested",
            AuditLifecycleStatus.PAUSING,
            RunStatus.PAUSING,
        ),
        (
            AuditLifecycleStatus.WAITING_APPROVAL,
            AuditControlDisposition.TRANSITION,
            AuditControlEffect.PAUSE_WORKFLOW_THEN_PROJECT,
            "audit_pause_requested",
            AuditLifecycleStatus.PAUSING,
            RunStatus.PAUSING,
        ),
        (
            AuditLifecycleStatus.PAUSING,
            AuditControlDisposition.RECONCILE,
            AuditControlEffect.RECONCILE_PAUSE,
            "audit_pause_reconciliation_required",
            AuditLifecycleStatus.PAUSED,
            RunStatus.PAUSED,
        ),
        (
            AuditLifecycleStatus.PAUSED,
            AuditControlDisposition.ALREADY_SATISFIED,
            AuditControlEffect.NONE,
            "audit_already_paused",
            AuditLifecycleStatus.PAUSED,
            RunStatus.PAUSED,
        ),
    ],
)
async def test_pause_returns_a_read_only_cas_plan(
    status: AuditLifecycleStatus,
    disposition: AuditControlDisposition,
    effect: AuditControlEffect,
    reason_code: str,
    target_audit: AuditLifecycleStatus,
    target_run: RunStatus,
) -> None:
    aggregate = _aggregate_for_scan(_scan_for(status), state_version=23)
    before = _read_snapshot(aggregate)
    repository = FakeAggregateRepository((aggregate,))
    uow = FakeCreationUnitOfWork()

    plan = await _service(uow=uow, repository=repository).pause(aggregate.audit.value.id)

    assert plan.operation is AuditControlAction.PAUSE
    assert plan.disposition is disposition
    assert plan.required_effect is effect
    assert plan.reason_code == reason_code
    assert plan.audit_id == aggregate.audit.value.id
    assert plan.run_id == aggregate.run.id
    assert plan.expected_audit_state_version == 23
    assert plan.current_audit_lifecycle is status
    assert plan.current_run_status is _run_status_for(aggregate.audit.value)
    assert plan.target_audit_lifecycle is target_audit
    assert plan.target_run_status is target_run
    assert _read_snapshot(aggregate) == before
    assert uow.calls == []


_PAUSE_REJECTED = tuple(
    status
    for status in AuditLifecycleStatus
    if status
    not in {
        AuditLifecycleStatus.RUNNING,
        AuditLifecycleStatus.WAITING_APPROVAL,
        AuditLifecycleStatus.PAUSING,
        AuditLifecycleStatus.PAUSED,
    }
)


@pytest.mark.parametrize("status", _PAUSE_REJECTED)
async def test_pause_rejects_every_other_lifecycle_without_mutation(
    status: AuditLifecycleStatus,
) -> None:
    aggregate = _aggregate_for_scan(_scan_for(status))
    before = _read_snapshot(aggregate)
    uow = FakeCreationUnitOfWork()
    service = _service(uow=uow, repository=FakeAggregateRepository((aggregate,)))

    with pytest.raises(ApplicationConflictError) as captured:
        await service.pause(aggregate.audit.value.id)

    assert captured.value.code == "audit_not_pauseable"
    assert captured.value.details["lifecycle_status"] == status.value
    assert _read_snapshot(aggregate) == before
    assert uow.calls == []


async def test_resume_returns_a_read_only_running_plan_from_paused() -> None:
    aggregate = _aggregate_for_scan(_scan_for(AuditLifecycleStatus.PAUSED), state_version=31)
    before = _read_snapshot(aggregate)
    uow = FakeCreationUnitOfWork()

    plan = await _service(
        uow=uow,
        repository=FakeAggregateRepository((aggregate,)),
    ).resume(aggregate.audit.value.id)

    assert plan.operation is AuditControlAction.RESUME
    assert plan.disposition is AuditControlDisposition.TRANSITION
    assert plan.required_effect is AuditControlEffect.RESUME_WORKFLOW_THEN_PROJECT
    assert plan.reason_code == "audit_resume_requested"
    assert plan.current_audit_lifecycle is AuditLifecycleStatus.PAUSED
    assert plan.current_run_status is RunStatus.PAUSED
    assert plan.target_audit_lifecycle is AuditLifecycleStatus.RUNNING
    assert plan.target_run_status is RunStatus.RUNNING
    assert plan.expected_audit_state_version == 31
    assert _read_snapshot(aggregate) == before
    assert uow.calls == []


_RESUME_REJECTED = tuple(
    status for status in AuditLifecycleStatus if status is not AuditLifecycleStatus.PAUSED
)


@pytest.mark.parametrize("status", _RESUME_REJECTED)
async def test_resume_rejects_every_other_lifecycle_without_mutation(
    status: AuditLifecycleStatus,
) -> None:
    aggregate = _aggregate_for_scan(_scan_for(status))
    before = _read_snapshot(aggregate)
    service = _service(repository=FakeAggregateRepository((aggregate,)))

    with pytest.raises(ApplicationConflictError) as captured:
        await service.resume(aggregate.audit.value.id)

    assert captured.value.code == "audit_not_resumable"
    assert captured.value.details["lifecycle_status"] == status.value
    assert _read_snapshot(aggregate) == before


_CANCEL_EXPECTATIONS = {
    AuditLifecycleStatus.DRAFT: AuditControlDisposition.TRANSITION,
    AuditLifecycleStatus.QUEUED: AuditControlDisposition.TRANSITION,
    AuditLifecycleStatus.PREFLIGHTING: AuditControlDisposition.TRANSITION,
    AuditLifecycleStatus.SNAPSHOTTING: AuditControlDisposition.TRANSITION,
    AuditLifecycleStatus.RUNNING: AuditControlDisposition.TRANSITION,
    AuditLifecycleStatus.WAITING_APPROVAL: AuditControlDisposition.TRANSITION,
    AuditLifecycleStatus.PAUSING: AuditControlDisposition.TRANSITION,
    AuditLifecycleStatus.PAUSED: AuditControlDisposition.TRANSITION,
    AuditLifecycleStatus.FINALIZING: AuditControlDisposition.TRANSITION,
    AuditLifecycleStatus.CANCELLING: AuditControlDisposition.RECONCILE,
    AuditLifecycleStatus.FAILING: AuditControlDisposition.TRANSITION,
    AuditLifecycleStatus.CLEANING: AuditControlDisposition.RECONCILE,
    AuditLifecycleStatus.SEALING_CORE: AuditControlDisposition.SAFETY_ONLY,
    AuditLifecycleStatus.REPORTING: AuditControlDisposition.SAFETY_ONLY,
    AuditLifecycleStatus.PACKAGING: AuditControlDisposition.SAFETY_ONLY,
    AuditLifecycleStatus.COMPLETED: AuditControlDisposition.SAFETY_ONLY,
    AuditLifecycleStatus.COMPLETED_PARTIAL: AuditControlDisposition.SAFETY_ONLY,
    AuditLifecycleStatus.FAILED: AuditControlDisposition.SAFETY_ONLY,
    AuditLifecycleStatus.CANCELLED: AuditControlDisposition.SAFETY_ONLY,
}


@pytest.mark.parametrize(("status", "disposition"), _CANCEL_EXPECTATIONS.items())
async def test_cancel_full_lifecycle_matrix_returns_read_only_host_control_plans(
    status: AuditLifecycleStatus,
    disposition: AuditControlDisposition,
) -> None:
    aggregate = _aggregate_for_scan(_scan_for(status), state_version=41)
    before = _read_snapshot(aggregate)
    uow = FakeCreationUnitOfWork()

    plan = await _service(
        uow=uow,
        repository=FakeAggregateRepository((aggregate,)),
    ).cancel(aggregate.audit.value.id)

    assert plan.operation is AuditControlAction.CANCEL
    assert plan.disposition is disposition
    assert plan.expected_audit_state_version == 41
    assert plan.current_audit_lifecycle is status
    assert plan.current_run_status is _run_status_for(aggregate.audit.value)
    if disposition is AuditControlDisposition.TRANSITION:
        assert plan.required_effect is AuditControlEffect.FENCE_NEW_EFFECTS_AND_STOP
        assert plan.reason_code == "audit_cancel_requested"
        assert plan.target_audit_lifecycle is AuditLifecycleStatus.CANCELLING
        assert plan.target_run_status is RunStatus.CANCELLING
    elif disposition is AuditControlDisposition.RECONCILE:
        assert plan.required_effect is AuditControlEffect.RECONCILE_CANCEL_STOP
        assert plan.reason_code == "audit_cancel_reconciliation_required"
        if status is AuditLifecycleStatus.CANCELLING:
            assert plan.target_audit_lifecycle is AuditLifecycleStatus.CLEANING
        elif aggregate.audit.value.terminal_outcome is AuditTerminalOutcome.CANCELLED:
            assert plan.target_audit_lifecycle is AuditLifecycleStatus.CLEANING
        else:
            assert plan.target_audit_lifecycle is AuditLifecycleStatus.CANCELLING
        assert plan.target_run_status is RunStatus.CANCELLING
    else:
        assert plan.required_effect is AuditControlEffect.SAFETY_STOP_SWEEP_ONLY
        assert plan.reason_code == "audit_cancel_safety_sweep"
        assert plan.target_audit_lifecycle is status
        assert plan.target_run_status is aggregate.run.status
    assert _read_snapshot(aggregate) == before
    assert uow.calls == []


def _cleaning_scan(outcome: AuditTerminalOutcome, *, converged: bool) -> AuditScan:
    if outcome is AuditTerminalOutcome.CANCELLED:
        scan = _domain_scan().transition_to(AuditLifecycleStatus.CANCELLING)
    elif outcome is AuditTerminalOutcome.FAILED:
        scan = _domain_scan().transition_to(AuditLifecycleStatus.FAILING)
    else:
        scan = _running_scan().transition_to(
            AuditLifecycleStatus.FINALIZING,
            terminal_outcome=outcome,
        )
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    return _converge_cleanup(scan) if converged else scan


@pytest.mark.parametrize(
    ("outcome", "converged", "disposition"),
    [
        (
            AuditTerminalOutcome.COMPLETE,
            False,
            AuditControlDisposition.RECONCILE,
        ),
        (
            AuditTerminalOutcome.PARTIAL,
            False,
            AuditControlDisposition.RECONCILE,
        ),
        (
            AuditTerminalOutcome.FAILED,
            False,
            AuditControlDisposition.RECONCILE,
        ),
        (
            AuditTerminalOutcome.CANCELLED,
            False,
            AuditControlDisposition.RECONCILE,
        ),
        (
            AuditTerminalOutcome.COMPLETE,
            True,
            AuditControlDisposition.SAFETY_ONLY,
        ),
        (
            AuditTerminalOutcome.PARTIAL,
            True,
            AuditControlDisposition.SAFETY_ONLY,
        ),
        (
            AuditTerminalOutcome.FAILED,
            True,
            AuditControlDisposition.SAFETY_ONLY,
        ),
        (
            AuditTerminalOutcome.CANCELLED,
            True,
            AuditControlDisposition.SAFETY_ONLY,
        ),
    ],
)
async def test_cancel_distinguishes_cleaning_outcome_and_convergence(
    outcome: AuditTerminalOutcome,
    converged: bool,
    disposition: AuditControlDisposition,
) -> None:
    aggregate = _aggregate_for_scan(_cleaning_scan(outcome, converged=converged))

    plan = await _service(
        repository=FakeAggregateRepository((aggregate,)),
    ).cancel(aggregate.audit.value.id)

    assert plan.disposition is disposition
    if disposition is AuditControlDisposition.TRANSITION:
        assert plan.required_effect is AuditControlEffect.FENCE_NEW_EFFECTS_AND_STOP
        assert plan.target_audit_lifecycle is AuditLifecycleStatus.CANCELLING
        assert plan.target_run_status is RunStatus.CANCELLING
    elif disposition is AuditControlDisposition.RECONCILE:
        assert plan.required_effect is AuditControlEffect.RECONCILE_CANCEL_STOP
        assert plan.target_audit_lifecycle is (
            AuditLifecycleStatus.CLEANING
            if outcome is AuditTerminalOutcome.CANCELLED
            else AuditLifecycleStatus.CANCELLING
        )
        assert plan.target_run_status is RunStatus.CANCELLING
    else:
        assert plan.required_effect is AuditControlEffect.SAFETY_STOP_SWEEP_ONLY
        assert plan.target_audit_lifecycle is AuditLifecycleStatus.CLEANING
        assert plan.target_run_status is aggregate.run.status
