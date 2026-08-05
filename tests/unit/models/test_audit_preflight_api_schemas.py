from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from tests.unit.domain.test_audit_preflight import _leased_job, _result

from riftx.api.schemas.audit_preflight import (
    AuditPreflightCreateResponse,
    AuditPreflightJobResponse,
    CreateAuditPreflightRequest,
)
from riftx.application.services.audit_preflight import AuditPreflightCreationResult
from riftx.domain.audit_preflight import (
    AuditPreflightExitReceipt,
    AuditPreflightExitTerminalState,
    AuditPreflightJob,
    AuditPreflightJobStatus,
)
from riftx.domain.runner import RunnerPrincipal

NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "riftx.audit-preflight-request/v1",
        "client_request_id": "123e4567-e89b-42d3-a456-426614174000",
        "repository_path": "/srv/source/repository",
        "source_execution_target": {
            "node_id": "local",
            "source_ingest_backend": "linux_container",
        },
        "target": {
            "kind": "working_tree",
            "revision": "HEAD",
            "include_untracked": False,
        },
        "include_paths": ["src"],
        "exclude_paths": ["vendor"],
        "security_context": {
            "input_id": None,
            "repository_paths": [],
            "discover_defaults": False,
        },
        "mode": "standard",
    }
    payload.update(updates)
    return payload


def _job() -> AuditPreflightJob:
    request = CreateAuditPreflightRequest.model_validate(_payload()).to_domain()
    return AuditPreflightJob(
        job_id="preflight-job-1",
        client_request_id=request.client_request_id,
        operator_principal_id="operator-1",
        authorization_scope_digest=_digest("scope"),
        request_digest=request.request_digest,
        restricted_request_json=request.canonical_json(),
        source_root_identity_digest=_digest("root"),
        backend_id="linux_container",
        image_digest=_digest("image"),
        policy_digest=_digest("policy"),
        expires_at=NOW + timedelta(minutes=15),
        created_at=NOW,
        updated_at=NOW,
    )


def test_request_maps_only_caller_owned_aud_200_fields() -> None:
    wire = CreateAuditPreflightRequest.model_validate(_payload())
    request = wire.to_domain()

    assert request.repository_path == "/srv/source/repository"
    assert request.source_execution_target.node_id == "local"
    assert request.source_execution_target.source_ingest_backend == "linux_container"
    assert request.security_context.input_id is None
    assert request.security_context.repository_paths == ()
    assert request.security_context.discover_defaults is False
    assert "image_digest" not in CreateAuditPreflightRequest.model_fields
    assert "policy_digest" not in CreateAuditPreflightRequest.model_fields
    assert "authorization_scope_digest" not in CreateAuditPreflightRequest.model_fields


@pytest.mark.parametrize(
    "updates",
    [
        {"source_execution_target": {"node_id": "remote"}},
        {
            "source_execution_target": {
                "node_id": "local",
                "source_ingest_backend": "caller_selected",
            }
        },
        {
            "security_context": {
                "input_id": "context-1",
                "repository_paths": [],
                "discover_defaults": False,
            }
        },
        {
            "security_context": {
                "input_id": None,
                "repository_paths": ["SECURITY.md"],
                "discover_defaults": False,
            }
        },
        {
            "security_context": {
                "input_id": None,
                "repository_paths": [],
                "discover_defaults": True,
            }
        },
        {"include_paths": ["../escape"]},
        {"include_paths": ["src", "src"]},
        {"include_paths": ["z", "a"]},
        {"preflight_token": "caller-owned-token"},
        {"image_digest": "0" * 64},
    ],
)
def test_request_rejects_cross_node_context_paths_and_server_owned_fields(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CreateAuditPreflightRequest.model_validate(_payload(**updates))


def test_request_is_strict_and_rejects_noncanonical_target_combinations() -> None:
    with pytest.raises(ValidationError):
        CreateAuditPreflightRequest.model_validate(
            _payload(
                target={
                    "kind": "revision",
                    "revision": "HEAD",
                    "include_untracked": 1,
                }
            )
        )
    with pytest.raises(ValidationError):
        CreateAuditPreflightRequest.model_validate(
            _payload(
                mode="diff",
                target={
                    "kind": "working_tree",
                    "revision": "HEAD",
                    "include_untracked": False,
                },
            )
        )
    with pytest.raises(ValidationError):
        CreateAuditPreflightRequest.model_validate(
            _payload(
                mode="standard",
                target={
                    "kind": "working_tree",
                    "revision": "HEAD",
                    "base_revision": "main",
                    "include_untracked": False,
                },
            )
        )


def test_sensitive_invalid_path_is_hidden_from_validation_text() -> None:
    canary = "/srv/CANARY-SECRET/../repository"
    with pytest.raises(ValidationError) as captured:
        CreateAuditPreflightRequest.model_validate(_payload(repository_path=canary))
    assert canary not in str(captured.value)


def test_job_projection_excludes_restricted_owner_and_effect_fields() -> None:
    response = AuditPreflightJobResponse.from_job(_job())
    payload = response.model_dump(mode="json")
    forbidden = {
        "repository_path",
        "restricted_request_json",
        "operator_principal_id",
        "authorization_scope_digest",
        "source_root_identity_digest",
        "effect_owner_digest",
        "lease_id",
        "lease_envelope_digest",
        "capsule_id",
        "never_created_proof_digest",
        "stop_receipt_digest",
        "exit_receipt_digest",
    }

    assert not forbidden.intersection(payload)
    assert payload["job_id"] == "preflight-job-1"
    assert payload["status"] == "pending"
    assert payload["result"] is None


def test_create_projection_has_exact_created_and_replayed_flags() -> None:
    job = _job()
    created = AuditPreflightCreateResponse.from_result(
        AuditPreflightCreationResult(job=job, created=True)
    )
    replayed = AuditPreflightCreateResponse.from_result(
        AuditPreflightCreationResult(job=job, created=False)
    )

    assert (created.created, created.replayed) == (True, False)
    assert (replayed.created, replayed.replayed) == (False, True)


def test_terminal_result_projection_contains_only_bounded_safe_facts() -> None:
    running = _leased_job(
        status=AuditPreflightJobStatus.RUNNING,
        state_version=3,
        running=True,
    )
    result = _result(running)
    receipt = AuditPreflightExitReceipt(
        job_id=running.job_id,
        effect_owner_digest=running.effect_owner_digest,
        lease_envelope_digest=running.lease_envelope_digest or _digest("lease"),
        capsule_id=running.capsule_id or "capsule-1",
        runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=7),
        backend_id=running.backend_id,
        image_digest=running.image_digest,
        policy_digest=running.policy_digest,
        process_identity_digest=_digest("process"),
        result_digest=result.result_digest,
        terminal_state=AuditPreflightExitTerminalState.SUCCEEDED,
        received_at=result.completed_at,
    )
    payload = running.model_dump(mode="python")
    payload.update(
        status=AuditPreflightJobStatus.SUCCEEDED,
        state_version=4,
        result_schema_version=result.schema_version,
        result_json=result.canonical_json(),
        result_digest=result.result_digest,
        exit_receipt_digest=receipt.receipt_digest,
        updated_at=result.completed_at,
        finished_at=result.completed_at,
    )
    response = AuditPreflightJobResponse.from_job(
        AuditPreflightJob.model_validate(payload)
    ).model_dump(mode="json")

    assert response["status"] == "succeeded"
    projected = response["result"]
    assert isinstance(projected, dict)
    assert projected["result_digest"] == result.result_digest
    assert projected["repository_identity_digest"] == result.repository_identity_digest
    assert projected["capability_matrix"]["matrix_digest"] == (
        result.capability_matrix.matrix_digest
    )
    assert "effect_owner_digest" not in projected
    assert "repository_path" not in str(projected)
