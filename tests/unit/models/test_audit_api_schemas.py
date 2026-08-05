from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from tests.unit.domain.test_audit_domain import _contract as domain_contract
from tests.unit.domain.test_audit_domain import _scan as domain_scan

from riftx.api.schemas import (
    AuditDraftResponse,
    AuditListQuery,
    AuditListResponse,
    AuditResponse,
    CreateAuditDraftRequest,
    CreateAuditDraftRequestV2,
)
from riftx.application.ports import AuditAggregate, StoredAuditEntity
from riftx.application.services import AuditDraftResult
from riftx.domain import (
    ApprovalMode,
    AuditClientRequest,
    AuditContractRecord,
    AuditLifecycleStatus,
    AuditMode,
    AuditProject,
    Engagement,
    Objective,
    Run,
    RunKind,
    RunStatus,
    Scope,
)

CLIENT_REQUEST_ID = "6ed6232a-3fb3-4f93-868f-0be291142f31"


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _request_payload() -> dict[str, object]:
    return {
        "client_request_id": CLIENT_REQUEST_ID,
        "project_name": "RiftX",
        "repository_identity_digest": _digest("repository"),
        "engagement_id": "engagement-1",
        "default_branch": "main",
        "contract": domain_contract().model_dump(
            mode="json",
            exclude={"audit_id", "project_id"},
        ),
    }


def _request_v2_payload() -> dict[str, object]:
    return {
        "schema_version": "riftx.audit-create-draft-request/v2",
        "client_request_id": CLIENT_REQUEST_ID,
        "preflight_token": "A" * 86,
        "project_name": "RiftX",
        "engagement_id": None,
        "mode": "standard",
        "analysis_profile": "deterministic",
        "model_profile": None,
        "model_data_egress": {"mode": "local_only"},
        "validation_policy": "static_only",
        "baseline_audit_id": None,
        "execution_target": {
            "node_id": "local",
            "required_sandbox_backend": "linux_container",
        },
        "budget": {
            "schema_version": "riftx.audit-draft-budget/v2",
            "max_wall_seconds": 1800,
            "max_detector_jobs": 64,
            "max_worker_jobs": 8,
            "max_epochs": 1,
            "max_model_calls": 0,
            "max_input_tokens": 0,
            "max_output_tokens": 0,
            "max_read_bytes": 16_777_216,
            "max_candidates": 100,
            "max_signals": 1000,
            "max_dynamic_validations": 0,
            "max_artifact_output_bytes": 16_777_216,
        },
    }


def test_create_v2_wire_accepts_only_caller_preferences_and_hides_token() -> None:
    request = CreateAuditDraftRequestV2.model_validate(_request_v2_payload())
    command = request.to_command()

    assert command.preflight_token == "A" * 86
    assert "A" * 86 not in repr(request)
    assert "A" * 86 not in repr(command)
    assert command.mode is AuditMode.STANDARD
    assert command.analysis_profile.value == "deterministic"

    for forbidden in (
        "repository_path",
        "source_target",
        "execution_selection",
        "operator_consent_at",
        "security_context_bundle_id",
        "preflight_plan_digest",
        "proof_digest",
    ):
        payload = _request_v2_payload()
        payload[forbidden] = "forged"
        with pytest.raises(ValidationError):
            CreateAuditDraftRequestV2.model_validate(payload)

    with pytest.raises(ValidationError):
        CreateAuditDraftRequestV2.model_validate(_request_payload())


def _aggregate() -> AuditAggregate:
    now = datetime(2026, 8, 3, 8, tzinfo=UTC)
    contract = domain_contract()
    contract_record = AuditContractRecord.from_contract(
        contract,
        contract_id="contract-1",
        created_at=now,
    )
    scan = domain_scan(contract, contract_record)
    engagement = Engagement(
        id="engagement-1",
        name="RiftX Code Audit",
        authorization_reference=_digest("authorization-canary"),
        created_at=now,
        updated_at=now,
    )
    project = AuditProject(
        id=scan.project_id,
        engagement_id=engagement.id,
        display_name="RiftX",
        repository_identity_digest=_digest("repository-canary"),
        default_branch="main",
        created_at=now,
        updated_at=now,
    )
    run = Run(
        id=scan.run_id,
        engagement_id=engagement.id,
        node_id=scan.selected_node_id,
        kind=RunKind.CODE_AUDIT,
        objective=Objective(description="Code Audit: RiftX"),
        scope=Scope(),
        status=RunStatus.CREATED,
        approval_mode=ApprovalMode.MANUAL,
        model_profile=scan.model_profile,
        workspace_path="/sensitive/riftx/audit/workspace",
        temporal_workflow_id=scan.temporal_workflow_id,
        created_at=now,
    )
    client_request = AuditClientRequest(
        client_request_id=CLIENT_REQUEST_ID,
        request_digest=_digest("request-canary"),
        audit_id=scan.id,
        run_id=scan.run_id,
        project_id=scan.project_id,
        engagement_id=engagement.id,
        contract_id=contract_record.contract_id,
        contract_digest=contract_record.contract_digest,
        temporal_workflow_id=scan.temporal_workflow_id,
        created_at=now,
    )
    return AuditAggregate(
        audit=StoredAuditEntity(scan, state_version=7),
        contract=StoredAuditEntity(contract_record, state_version=3),
        project=StoredAuditEntity(project, state_version=2),
        run=run,
        engagement=engagement,
        client_request=client_request,
    )


def _all_mapping_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys.update(_all_mapping_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_all_mapping_keys(item))
    return keys


def _object_schemas(value: object) -> list[dict[str, object]]:
    schemas: list[dict[str, object]] = []
    if isinstance(value, dict):
        if value.get("type") == "object" and isinstance(value.get("properties"), dict):
            schemas.append(value)
        for item in value.values():
            schemas.extend(_object_schemas(item))
    elif isinstance(value, list):
        for item in value:
            schemas.extend(_object_schemas(item))
    return schemas


def test_draft_wire_converts_non_strict_json_to_a_strict_server_bound_blueprint() -> None:
    payload = _request_payload()
    contract = payload["contract"]
    assert isinstance(contract, dict)
    budget = contract["budget"]
    assert isinstance(budget, dict)
    budget["max_wall_seconds"] = "7200"

    request = CreateAuditDraftRequest.model_validate(payload)
    command = request.to_command(
        authorization_reference=_digest("server-authorized-source-policy"),
    )
    materialized = command.contract.materialize(
        audit_id="server-audit-id",
        project_id="server-project-id",
    )

    assert request.contract.budget.max_wall_seconds == 7_200
    assert command.client_request_id == CLIENT_REQUEST_ID
    assert command.authorization_reference == _digest("server-authorized-source-policy")
    assert command.engagement_id == "engagement-1"
    assert materialized.audit_id == "server-audit-id"
    assert materialized.project_id == "server-project-id"
    expected = domain_contract().model_dump(
        mode="json",
        exclude={"audit_id", "project_id"},
    )
    expected["budget"]["max_wall_seconds"] = 7_200
    assert (
        materialized.model_dump(
            mode="json",
            exclude={"audit_id", "project_id"},
        )
        == expected
    )


@pytest.mark.parametrize(
    "field",
    [
        "authorization_reference",
        "preflight_token",
        "audit_id",
        "project_id",
        "run_id",
        "contract_id",
        "workspace_path",
        "temporal_workflow_id",
        "start",
        "auto_start",
    ],
)
def test_draft_wire_rejects_authorization_preflight_and_server_owned_fields(
    field: str,
) -> None:
    payload = _request_payload()
    payload[field] = "caller-must-not-control-this"

    with pytest.raises(ValidationError):
        CreateAuditDraftRequest.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "audit_id",
        "project_id",
        "run_id",
        "contract_id",
        "workspace_path",
        "temporal_workflow_id",
        "preflight_token",
    ],
)
def test_frozen_contract_wire_rejects_server_owned_fields(field: str) -> None:
    payload = _request_payload()
    contract = payload["contract"]
    assert isinstance(contract, dict)
    contract[field] = "caller-must-not-control-this"

    with pytest.raises(ValidationError):
        CreateAuditDraftRequest.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("contract", "source_target"),
        ("contract", "budget"),
        ("contract", "execution_selection"),
        ("contract", "capability_matrix", "entries", 0),
    ],
)
def test_every_nested_audit_wire_layer_forbids_unknown_fields(
    path: tuple[str | int, ...],
) -> None:
    payload = copy.deepcopy(_request_payload())
    current: object = payload
    for part in path:
        if isinstance(part, int):
            assert isinstance(current, list)
            current = current[part]
        else:
            assert isinstance(current, dict)
            current = current[part]
    assert isinstance(current, dict)
    current["unexpected"] = "must-be-rejected"

    with pytest.raises(ValidationError):
        CreateAuditDraftRequest.model_validate(payload)


@pytest.mark.parametrize(
    "client_request_id",
    [
        "00000000-0000-0000-0000-000000000000",
        "6ED6232A-3FB3-4F93-868F-0BE291142F31",
        "{6ed6232a-3fb3-4f93-868f-0be291142f31}",
        "not-a-uuid",
    ],
)
def test_client_request_id_must_be_a_nonzero_canonical_uuid(
    client_request_id: str,
) -> None:
    payload = _request_payload()
    payload["client_request_id"] = client_request_id

    with pytest.raises(ValidationError):
        CreateAuditDraftRequest.model_validate(payload)


def test_server_authorization_reference_is_required_and_strictly_validated() -> None:
    request = CreateAuditDraftRequest.model_validate(_request_payload())

    with pytest.raises(TypeError):
        request.to_command()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        request.to_command(authorization_reference="A" * 64)


def test_invalid_contract_error_text_does_not_render_the_source_path() -> None:
    payload = _request_payload()
    contract = payload["contract"]
    assert isinstance(contract, dict)
    source_target = contract["source_target"]
    assert isinstance(source_target, dict)
    canary = "relative/RIFTX_TEST_SECRET_SOURCE_PATH_DO_NOT_RENDER"
    source_target["repository_path"] = canary

    with pytest.raises(ValidationError) as captured:
        CreateAuditDraftRequest.model_validate(payload)

    assert canary not in str(captured.value)
    assert "frozen Code Audit contract is invalid" in str(captured.value)


def test_create_request_openapi_has_no_authorization_or_server_owned_fields() -> None:
    schema = CreateAuditDraftRequest.model_json_schema()
    top_level = schema["properties"]

    assert set(top_level) == {
        "client_request_id",
        "project_name",
        "repository_identity_digest",
        "contract",
        "engagement_id",
        "default_branch",
    }
    assert all(item.get("additionalProperties") is False for item in _object_schemas(schema))
    serialized_schema = json.dumps(schema, sort_keys=True)
    for forbidden in (
        "authorization_reference",
        "preflight_token",
        '"audit_id"',
        '"project_id"',
        '"run_id"',
        "workspace_path",
        "temporal_workflow_id",
    ):
        assert forbidden not in serialized_schema


def test_audit_list_query_parses_wire_values_and_enforces_bounded_range() -> None:
    query = AuditListQuery.model_validate(
        {
            "run_id": "run-1",
            "project_id": "project-1",
            "engagement_id": "engagement-1",
            "status": "draft",
            "mode": "standard",
            "created_from": "2026-08-03T00:00:00Z",
            "created_to": "2026-08-03T01:00:00Z",
            "limit": "200",
            "offset": "4",
        }
    )

    assert query.lifecycle_status is AuditLifecycleStatus.DRAFT
    assert query.mode is AuditMode.STANDARD
    assert query.created_from == datetime(2026, 8, 3, tzinfo=UTC)
    assert query.limit == 200
    assert query.offset == 4

    for invalid in (
        {"limit": 201},
        {"offset": -1},
        {"created_from": "2026-08-03T00:00:00"},
        {
            "created_from": "2026-08-03T01:00:00Z",
            "created_to": "2026-08-03T00:00:00Z",
        },
        {"unknown": "not-allowed"},
    ):
        with pytest.raises(ValidationError):
            AuditListQuery.model_validate(invalid)


def test_audit_response_is_an_exact_positive_allowlist_without_sensitive_facts() -> None:
    aggregate = _aggregate()
    response = AuditResponse.from_aggregate(aggregate)
    payload = response.model_dump(mode="json")

    assert set(payload) == {
        "id",
        "run_id",
        "project",
        "state_version",
        "snapshot_id",
        "base_snapshot_id",
        "baseline_audit_id",
        "purpose",
        "parent_audit_id",
        "mode",
        "analysis_profile",
        "lifecycle_status",
        "current_phase",
        "terminal_outcome",
        "closure_status",
        "publication_status",
        "initial_distribution_revision_id",
        "latest_distribution_revision_id",
        "model_profile",
        "run_status",
        "created_at",
        "started_at",
        "analysis_finished_at",
        "publication_finished_at",
        "sealed_at",
    }
    assert set(payload["project"]) == {
        "id",
        "engagement_id",
        "display_name",
        "vcs_kind",
        "default_branch",
    }
    assert payload["id"] == aggregate.audit.value.id
    assert payload["run_id"] == aggregate.run.id
    assert payload["state_version"] == aggregate.audit.state_version

    all_keys = _all_mapping_keys(payload)
    assert not any("digest" in key for key in all_keys)
    assert all_keys.isdisjoint(
        {
            "repository_path",
            "authorization_reference",
            "canonical_contract_json",
            "contract",
            "workspace_path",
            "temporal_workflow_id",
            "request_digest",
        }
    )
    serialized = json.dumps(payload, sort_keys=True)
    for hidden in (
        aggregate.contract.value.canonical_contract_json,
        aggregate.engagement.authorization_reference,
        aggregate.project.value.repository_identity_digest,
        aggregate.run.workspace_path,
        aggregate.run.temporal_workflow_id,
        aggregate.client_request.request_digest,
    ):
        assert hidden is None or hidden not in serialized

    with pytest.raises(ValidationError):
        AuditResponse.model_validate({**payload, "repository_path": "/must/not/leak"})


def test_draft_and_list_responses_preserve_disposition_and_page_without_expansion() -> None:
    aggregate = _aggregate()
    created = AuditDraftResponse.from_result(AuditDraftResult(aggregate=aggregate, created=True))
    replayed = AuditDraftResponse.from_result(AuditDraftResult(aggregate=aggregate, created=False))
    page = AuditListResponse.from_aggregates((aggregate,), limit=50, offset=10)

    assert (created.created, created.replayed) == (True, False)
    assert (replayed.created, replayed.replayed) == (False, True)
    assert page.limit == 50
    assert page.offset == 10
    assert page.items == [AuditResponse.from_aggregate(aggregate)]
    with pytest.raises(ValidationError):
        AuditDraftResponse(created=True, replayed=True, audit=created.audit)
