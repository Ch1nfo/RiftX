from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from riftx.domain import AnalysisProfile, AuditMode
from riftx.domain.audit_contract_v2 import AuditContractRecordV2

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
SCHEMA_VERSION = "riftx.audit-contract/v2"


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _domain_digest(payload: str) -> str:
    return hashlib.sha256(
        SCHEMA_VERSION.encode("ascii") + b"\0" + payload.encode()
    ).hexdigest()


def _record_payload() -> dict[str, object]:
    backend_digest = _digest("backend")
    prepare_digest = _digest("prepare")
    plan_digest = _digest("plan")
    context_digest = _digest("context")
    target_digest = _digest("target")
    canonical = json.dumps(
        {
            "analysis_profile": "deterministic",
            "audit_id": "audit-1",
            "baseline_audit_id": None,
            "budget": {"budget_digest": _digest("budget")},
            "execution_selection": {
                "source_ingest_backend_component_digest": backend_digest,
                "source_node_id": "local",
                "source_prepare_proof_digest": prepare_digest,
            },
            "mode": "standard",
            "model_profile": None,
            "preflight_plan_digest": plan_digest,
            "preflight_plan_id": "plan-1",
            "project_id": "project-1",
            "schema_version": SCHEMA_VERSION,
            "security_context_bundle_digest": context_digest,
            "security_context_bundle_id": "riftx.audit-empty-security-context/v1",
            "source_binding": {"source_node_id": "local"},
            "source_target": {"target_digest": target_digest},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "contract_id": "contract-1",
        "audit_id": "audit-1",
        "schema_version": SCHEMA_VERSION,
        "canonical_contract_json": canonical,
        "contract_digest": _domain_digest(canonical),
        "source_target_digest": target_digest,
        "source_node_id": "local",
        "source_ingest_backend_digest": backend_digest,
        "source_prepare_proof_digest": prepare_digest,
        "preflight_plan_id": "plan-1",
        "preflight_plan_digest": plan_digest,
        "security_context_bundle_id": "riftx.audit-empty-security-context/v1",
        "security_context_bundle_digest": context_digest,
        "created_at": NOW,
    }


def test_historical_v2_record_exposes_only_read_binding_facts() -> None:
    record = AuditContractRecordV2.model_validate(_record_payload())
    contract = record.contract()

    assert contract.project_id == "project-1"
    assert contract.mode is AuditMode.STANDARD
    assert contract.analysis_profile is AnalysisProfile.DETERMINISTIC
    assert contract.source_binding.source_node_id == "local"
    assert contract.budget.budget_digest == _digest("budget")
    assert contract.baseline_audit_id is contract.model_profile is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract_digest", "0" * 64),
        ("source_target_digest", "0" * 64),
        ("preflight_plan_digest", "0" * 64),
    ],
)
def test_historical_v2_record_rejects_redundant_fact_mismatch(
    field: str,
    value: str,
) -> None:
    payload = _record_payload()
    payload[field] = value

    with pytest.raises(ValidationError, match="does not match canonical facts"):
        AuditContractRecordV2.model_validate(payload)


def test_historical_v2_record_rejects_noncanonical_json() -> None:
    payload = _record_payload()
    payload["canonical_contract_json"] = json.dumps(
        json.loads(str(payload["canonical_contract_json"])),
        sort_keys=True,
    )

    with pytest.raises(ValidationError, match="not canonical"):
        AuditContractRecordV2.model_validate(payload)
