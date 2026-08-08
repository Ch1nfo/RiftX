"""Minimal historical Code Audit v2 contract record used by compatibility reads."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .audit import AnalysisProfile, AuditMode

AUDIT_CONTRACT_V2_SCHEMA_VERSION = "riftx.audit-contract/v2"
_MAX_CONTRACT_BYTES = 256 * 1_024
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\\-]{0,127}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


@dataclass(frozen=True, slots=True)
class _HistoricalSourceBinding:
    source_node_id: str


@dataclass(frozen=True, slots=True)
class _HistoricalBudget:
    budget_digest: str


@dataclass(frozen=True, slots=True)
class HistoricalAuditContractV2:
    project_id: str
    mode: AuditMode
    analysis_profile: AnalysisProfile
    baseline_audit_id: None
    model_profile: None
    source_binding: _HistoricalSourceBinding
    budget: _HistoricalBudget


class AuditContractRecordV2(BaseModel):
    """Validated redundant facts for an already persisted v2 draft."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: str = Field(pattern=_ID_PATTERN)
    audit_id: str = Field(pattern=_ID_PATTERN)
    schema_version: Literal["riftx.audit-contract/v2"] = AUDIT_CONTRACT_V2_SCHEMA_VERSION
    canonical_contract_json: str = Field(min_length=2, max_length=_MAX_CONTRACT_BYTES, repr=False)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_target_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_node_id: Literal["local"] = "local"
    source_ingest_backend_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_prepare_proof_digest: str = Field(pattern=_DIGEST_PATTERN)
    preflight_plan_id: str = Field(pattern=_ID_PATTERN)
    preflight_plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    security_context_bundle_id: Literal["riftx.audit-empty-security-context/v1"]
    security_context_bundle_digest: str = Field(pattern=_DIGEST_PATTERN)
    created_at: AwareDatetime
    sealed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_record(self) -> AuditContractRecordV2:
        payload = _canonical_payload(self.canonical_contract_json)
        selection = _mapping(payload, "execution_selection")
        source_target = _mapping(payload, "source_target")
        checks = (
            (payload.get("schema_version"), self.schema_version),
            (payload.get("audit_id"), self.audit_id),
            (payload.get("preflight_plan_id"), self.preflight_plan_id),
            (payload.get("preflight_plan_digest"), self.preflight_plan_digest),
            (payload.get("security_context_bundle_id"), self.security_context_bundle_id),
            (
                payload.get("security_context_bundle_digest"),
                self.security_context_bundle_digest,
            ),
            (source_target.get("target_digest"), self.source_target_digest),
            (selection.get("source_node_id"), self.source_node_id),
            (
                selection.get("source_ingest_backend_component_digest"),
                self.source_ingest_backend_digest,
            ),
            (
                selection.get("source_prepare_proof_digest"),
                self.source_prepare_proof_digest,
            ),
            (_domain_digest(self.schema_version, payload), self.contract_digest),
        )
        if any(
            not (
                isinstance(actual, str)
                and isinstance(expected, str)
                and hmac.compare_digest(actual, expected)
            )
            for actual, expected in checks
        ):
            raise ValueError("Audit v2 contract record does not match canonical facts")
        if self.sealed_at is not None and self.sealed_at < self.created_at:
            raise ValueError("Audit v2 contract sealed_at must not precede created_at")
        return self

    def contract(self) -> HistoricalAuditContractV2:
        payload = _canonical_payload(self.canonical_contract_json)
        source_binding = _mapping(payload, "source_binding")
        budget = _mapping(payload, "budget")
        project_id = payload.get("project_id")
        budget_digest = budget.get("budget_digest")
        source_node_id = source_binding.get("source_node_id")
        if not all(isinstance(value, str) and value for value in (
            project_id,
            budget_digest,
            source_node_id,
        )):
            raise ValueError("Audit v2 contract owner facts are invalid")
        if payload.get("baseline_audit_id") is not None or payload.get("model_profile") is not None:
            raise ValueError("Audit v2 draft carries unsupported later-stage facts")
        return HistoricalAuditContractV2(
            project_id=project_id,
            mode=AuditMode(payload.get("mode")),
            analysis_profile=AnalysisProfile(payload.get("analysis_profile")),
            baseline_audit_id=None,
            model_profile=None,
            source_binding=_HistoricalSourceBinding(source_node_id=source_node_id),
            budget=_HistoricalBudget(budget_digest=budget_digest),
        )


def _canonical_payload(value: str) -> dict[str, object]:
    if len(value.encode("utf-8")) > _MAX_CONTRACT_BYTES:
        raise ValueError("canonical Audit v2 contract exceeds its byte limit")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate Audit v2 contract key")
            result[key] = item
        return result

    try:
        payload = json.loads(value, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, RecursionError, UnicodeEncodeError) as exc:
        raise ValueError("Audit v2 contract JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("Audit v2 contract must be a JSON object")
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if canonical != value:
        raise ValueError("Audit v2 contract JSON is not canonical")
    return payload


def _mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Audit v2 contract {key} is invalid")
    return value


def _domain_digest(domain: str, payload: object) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + canonical).hexdigest()


__all__ = ["AuditContractRecordV2", "HistoricalAuditContractV2"]
