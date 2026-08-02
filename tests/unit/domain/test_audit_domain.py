from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from itertools import product
from typing import Any

import pytest
from pydantic import ValidationError

from riftx.domain import (
    AnalysisProfile,
    AuditBudget,
    AuditCapabilityMatrix,
    AuditCapabilityRequirement,
    AuditCapabilityVersion,
    AuditClosureStatus,
    AuditContract,
    AuditContractRecord,
    AuditExecutionSelection,
    AuditLanguageTier,
    AuditLifecycleStatus,
    AuditMode,
    AuditPhase,
    AuditPhaseRequirement,
    AuditPublicationStatus,
    AuditPurpose,
    AuditRuntimeMissingOutcome,
    AuditScan,
    AuditStartMissingOutcome,
    AuditTerminalOutcome,
    CandidateStatus,
    CapabilityMissingOutcome,
    InvalidStateTransitionError,
    ModelDataEgressMode,
    ModelDataEgressPolicy,
    ModelExecutionLocality,
    ModelRetentionTrainingDisclosure,
    ModelTrainingUsage,
    RunStatus,
    SchemaVersionRef,
    SourceTarget,
    SourceTargetKind,
    ValidationPolicy,
    VersionedCanonicalPolicy,
    VersionedComponentRef,
    candidate_can_transition_to,
    validate_candidate_transition,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _domain_digest(domain: str, payload: bytes) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\0" + payload).hexdigest()


def _budget(
    *,
    hybrid: bool = False,
    dynamic: bool = False,
    epochs: int = 2,
) -> AuditBudget:
    return AuditBudget(
        max_wall_seconds=3_600,
        max_detector_jobs=32,
        max_worker_jobs=16,
        max_epochs=epochs,
        max_model_calls=10 if hybrid else 0,
        max_input_tokens=100_000 if hybrid else 0,
        max_output_tokens=10_000 if hybrid else 0,
        max_read_bytes=10_000_000,
        max_candidates=100,
        max_signals=1_000,
        max_dynamic_validations=10 if dynamic else 0,
        max_artifact_output_bytes=10_000_000,
    )


def _required_capability(
    capability_id: str,
    *,
    phase: AuditPhase = AuditPhase.AUTHORIZE_AND_FREEZE,
    runtime: AuditRuntimeMissingOutcome = AuditRuntimeMissingOutcome.PARTIAL_CAPABILITY,
    provider_id: str = "control_plane",
    node_id: str | None = None,
    backend_id: str | None = None,
    minimum_version: str = "v1",
    component_digest: str | None = None,
    proof_digest: str | None = None,
) -> AuditCapabilityRequirement:
    return AuditCapabilityRequirement(
        phase=phase,
        capability_id=capability_id,
        requirement=AuditPhaseRequirement.REQUIRED,
        provider_id=provider_id,
        node_id=node_id,
        backend_id=backend_id,
        min_version_and_digest=AuditCapabilityVersion(
            minimum_version=minimum_version,
            component_digest=component_digest or _digest(f"component:{capability_id}"),
        ),
        proof_kind="signed_digest",
        proof_digest=proof_digest or _digest(f"proof:{capability_id}"),
        missing_outcome=CapabilityMissingOutcome(
            start=AuditStartMissingOutcome.REJECT_START,
            runtime=runtime,
        ),
    )


def _not_applicable_capability(
    capability_id: str,
    *,
    phase: AuditPhase = AuditPhase.AGENT_HUNT,
) -> AuditCapabilityRequirement:
    return AuditCapabilityRequirement(
        phase=phase,
        capability_id=capability_id,
        requirement=AuditPhaseRequirement.NOT_APPLICABLE,
        missing_outcome=CapabilityMissingOutcome(
            start=AuditStartMissingOutcome.NOT_APPLICABLE,
            runtime=AuditRuntimeMissingOutcome.NOT_APPLICABLE,
        ),
        reason_code="profile_not_applicable",
    )


_CAPABILITY_PHASES = {
    "agent_hunt": AuditPhase.AGENT_HUNT,
    "analysis_backend": AuditPhase.AUTHORIZE_AND_FREEZE,
    "child_workflow": AuditPhase.AGENT_HUNT,
    "closure": AuditPhase.VALIDATE_CLOSURE,
    "core_seal": AuditPhase.SEAL_CORE,
    "diff_mapper": AuditPhase.MAP_SCOPE,
    "dynamic_approval": AuditPhase.PROVE,
    "dynamic_egress": AuditPhase.PROVE,
    "dynamic_sandbox": AuditPhase.PROVE,
    "epoch_budget": AuditPhase.AGENT_HUNT,
    "isolated_build": AuditPhase.PROVE,
    "isolated_fix_and_retest": AuditPhase.PROVE,
    "isolated_poc": AuditPhase.PROVE,
    "isolated_test": AuditPhase.PROVE,
    "minimum_visits": AuditPhase.AGENT_HUNT,
    "model_adapter": AuditPhase.THREAT_MODEL,
    "model_transport": AuditPhase.THREAT_MODEL,
    "paired_closure": AuditPhase.COMPARE_BASELINE,
    "proof": AuditPhase.PROVE,
    "risk_comparator": AuditPhase.COMPARE_BASELINE,
    "sealed_base_head": AuditPhase.AUTHORIZE_AND_FREEZE,
    "skeptic": AuditPhase.RECONCILE,
    "snapshot_store": AuditPhase.AUTHORIZE_AND_FREEZE,
    "source_ingest": AuditPhase.AUTHORIZE_AND_FREEZE,
    "threat_model": AuditPhase.THREAT_MODEL,
    "typed_output": AuditPhase.THREAT_MODEL,
}


def _capability_matrix(
    *,
    profile: AnalysisProfile,
    mode: AuditMode,
    validation_policy: ValidationPolicy,
) -> AuditCapabilityMatrix:
    required = {
        "analysis_backend",
        "closure",
        "core_seal",
        "snapshot_store",
        "source_ingest",
    }
    hybrid = {
        "agent_hunt",
        "model_adapter",
        "model_transport",
        "proof",
        "skeptic",
        "threat_model",
        "typed_output",
    }
    entries: list[AuditCapabilityRequirement] = []
    if profile is AnalysisProfile.HYBRID:
        required.update(hybrid)
    else:
        entries.extend(
            _not_applicable_capability(name, phase=_CAPABILITY_PHASES[name])
            for name in hybrid
        )
    if mode is AuditMode.DEEP:
        required.update({"child_workflow", "epoch_budget", "minimum_visits"})
    if mode is AuditMode.DIFF:
        required.update(
            {"diff_mapper", "paired_closure", "risk_comparator", "sealed_base_head"}
        )
    if validation_policy is ValidationPolicy.STATIC_ONLY:
        entries.extend(
            _not_applicable_capability(name, phase=_CAPABILITY_PHASES[name])
            for name in {
                "isolated_build",
                "isolated_fix_and_retest",
                "isolated_poc",
                "isolated_test",
            }
        )
    else:
        required.update({"dynamic_approval", "dynamic_egress", "dynamic_sandbox"})
        selected_validation = {
            ValidationPolicy.ISOLATED_BUILD: "isolated_build",
            ValidationPolicy.ISOLATED_TEST: "isolated_test",
            ValidationPolicy.ISOLATED_POC: "isolated_poc",
            ValidationPolicy.ISOLATED_FIX_AND_RETEST: "isolated_fix_and_retest",
        }[validation_policy]
        required.add(selected_validation)
        entries.extend(
            _not_applicable_capability(name, phase=_CAPABILITY_PHASES[name])
            for name in {
                "isolated_build",
                "isolated_fix_and_retest",
                "isolated_poc",
                "isolated_test",
            }
            - {selected_validation}
        )

    for name in required:
        kwargs: dict[str, object] = {}
        if name == "source_ingest":
            kwargs = {
                "node_id": "source-node",
                "backend_id": "linux_container",
                "component_digest": _digest("source-backend"),
                "proof_digest": _digest("source-proof"),
            }
        elif name == "analysis_backend":
            kwargs = {
                "node_id": "analysis-node",
                "backend_id": "linux_container",
                "component_digest": _digest("analysis-backend"),
                "proof_digest": _digest("analysis-proof"),
            }
        elif name in hybrid or name == "dynamic_sandbox" or name == {
            ValidationPolicy.ISOLATED_BUILD: "isolated_build",
            ValidationPolicy.ISOLATED_TEST: "isolated_test",
            ValidationPolicy.ISOLATED_POC: "isolated_poc",
            ValidationPolicy.ISOLATED_FIX_AND_RETEST: "isolated_fix_and_retest",
            ValidationPolicy.STATIC_ONLY: "",
        }[validation_policy]:
            kwargs = {
                "node_id": "analysis-node",
                "backend_id": "linux_container",
            }
        entries.append(
            _required_capability(
                name,
                phase=_CAPABILITY_PHASES[name],
                **kwargs,  # type: ignore[arg-type]
            )
        )

    entries.extend(
        (
            _required_capability(
                "detector:riftx_inventory",
                phase=AuditPhase.DETERMINISTIC_PROBE,
                node_id="analysis-node",
                backend_id="linux_container",
                component_digest=_digest("detector"),
            ),
            _required_capability(
                "parser:python",
                phase=AuditPhase.DETERMINISTIC_PROBE,
                node_id="analysis-node",
                backend_id="linux_container",
                component_digest=_digest("parser"),
            ),
        )
    )
    return AuditCapabilityMatrix(entries=tuple(sorted(entries, key=lambda entry: entry.identity)))


def _source_target(mode: AuditMode = AuditMode.STANDARD) -> SourceTarget:
    return SourceTarget(
        repository_path="/srv/authorized/repository",
        kind=SourceTargetKind.WORKING_TREE,
        revision="HEAD",
        base_revision="HEAD~1" if mode is AuditMode.DIFF else None,
        include_untracked=False,
    )


def _model_disclosure(*, remote: bool = False) -> ModelRetentionTrainingDisclosure:
    return ModelRetentionTrainingDisclosure(
        data_residency_regions=("us",) if remote else ("local_controlled",),
        retention_days=30 if remote else 0,
        training_usage=ModelTrainingUsage.NOT_USED_FOR_TRAINING,
        provider_terms_version="v1",
        provider_terms_digest=_digest("remote-terms" if remote else "local-terms"),
    )


def _egress(*, hybrid: bool = False) -> ModelDataEgressPolicy:
    kwargs: dict[str, object] = {}
    if hybrid:
        kwargs = {
            "model_profile_digest": _digest("model-profile"),
            "endpoint_origin_digest": _digest("local-model-endpoint"),
            "provider_display_name": "RiftX Local Model Runtime",
            "execution_locality": ModelExecutionLocality.LOCAL_CONTROLLED,
            "retention_training_disclosure": _model_disclosure(),
            "allowed_scope_classes": ("application_code", "configuration"),
            "operator_consent_at": NOW,
        }
    return ModelDataEgressPolicy(
        mode=ModelDataEgressMode.LOCAL_ONLY,
        max_bytes_per_call=65_536,
        max_bytes_per_audit=1_000_000,
        **kwargs,  # type: ignore[arg-type]
    )


def _execution_selection() -> AuditExecutionSelection:
    return AuditExecutionSelection(
        source_node_id="source-node",
        source_ingest_backend_id="linux_container",
        source_ingest_backend_digest=_digest("source-backend"),
        source_prepare_proof_digest=_digest("source-proof"),
        selected_node_id="analysis-node",
        required_backend_id="linux_container",
        analysis_backend_digest=_digest("analysis-backend"),
        analysis_prepare_proof_digest=_digest("analysis-proof"),
        analysis_image_digest=_digest("analysis-image"),
        analysis_policy_digest=_digest("analysis-policy"),
        snapshot_hydration_policy_digest=_digest("hydration-policy"),
        selection_policy_version="v1",
        eligible_candidates_digest=_digest("eligible-candidates"),
    )


def _policy(name: str) -> VersionedCanonicalPolicy:
    return VersionedCanonicalPolicy.from_value(
        policy_schema_version=f"riftx.{name}/v1",
        value={"enabled": True, "name": name},
    )


def _validation_policy_document(
    validation_policy: ValidationPolicy,
) -> VersionedCanonicalPolicy:
    return VersionedCanonicalPolicy.from_value(
        policy_schema_version="riftx.validation-policy/v1",
        value={"validation_policy": validation_policy.value},
    )


def _contract(
    *,
    profile: AnalysisProfile = AnalysisProfile.DETERMINISTIC,
    mode: AuditMode = AuditMode.STANDARD,
    validation_policy: ValidationPolicy = ValidationPolicy.STATIC_ONLY,
    audit_id: str = "audit-1",
) -> AuditContract:
    hybrid = profile is AnalysisProfile.HYBRID
    return AuditContract(
        audit_id=audit_id,
        project_id="project-1",
        source_target=_source_target(mode),
        mode=mode,
        analysis_profile=profile,
        scope_capture_policy=_policy("scope-capture"),
        detectors=(
            VersionedComponentRef(
                component_id="riftx_inventory",
                version="v1",
                digest=_digest("detector"),
            ),
        ),
        rulepacks=(
            VersionedComponentRef(
                component_id="riftx_default",
                version="v1",
                digest=_digest("rulepack"),
            ),
        ),
        parsers=(
            VersionedComponentRef(
                component_id="python",
                version="v1",
                digest=_digest("parser"),
            ),
        ),
        model_profile="primary" if hybrid else None,
        model_profile_digest=_digest("model-profile") if hybrid else None,
        model_data_egress_policy=_egress(hybrid=hybrid),
        validation_policy=validation_policy,
        validation_policy_document=_validation_policy_document(validation_policy),
        budget=_budget(
            hybrid=hybrid,
            dynamic=validation_policy is not ValidationPolicy.STATIC_ONLY,
        ),
        execution_selection=_execution_selection(),
        capability_matrix=_capability_matrix(
            profile=profile,
            mode=mode,
            validation_policy=validation_policy,
        ),
        policy_digest=_digest("policy"),
        config_digest=_digest("config"),
        schema_versions=(SchemaVersionRef(schema_id="audit_events", version="v1"),),
    )


def _record(contract: AuditContract | None = None) -> AuditContractRecord:
    return AuditContractRecord.from_contract(
        contract or _contract(),
        contract_id="contract-1",
        created_at=NOW,
    )


def _scan(
    contract: AuditContract | None = None,
    record: AuditContractRecord | None = None,
) -> AuditScan:
    contract = contract or _contract()
    record = record or _record(contract)
    return AuditScan(
        id=contract.audit_id,
        run_id="run-1",
        project_id=contract.project_id,
        contract_id=record.contract_id,
        baseline_audit_id=contract.baseline_audit_id,
        mode=contract.mode,
        analysis_profile=contract.analysis_profile,
        model_profile=contract.model_profile,
        selected_node_id=contract.execution_selection.selected_node_id,
        required_backend_id=contract.execution_selection.required_backend_id,
        policy_digest=contract.policy_digest,
        budget_digest=contract.budget.digest,
        config_digest=contract.config_digest,
        contract_digest=contract.contract_digest,
        temporal_workflow_id=f"riftx-code-audit-{contract.audit_id}",
        created_at=NOW,
    )


def _payload(model: object) -> dict[str, Any]:
    return model.model_dump(mode="python")  # type: ignore[attr-defined,no-any-return]


def _replace_capability(
    matrix: AuditCapabilityMatrix,
    capability_id: str,
    **updates: object,
) -> AuditCapabilityMatrix:
    replaced = False
    entries: list[AuditCapabilityRequirement] = []
    for entry in matrix.entries:
        if (
            entry.capability_id == capability_id
            and not entry.scope_classes
            and not entry.language_tiers
        ):
            entry_payload = _payload(entry)
            entry_payload.update(updates)
            entry = AuditCapabilityRequirement.model_validate(entry_payload)
            replaced = True
        entries.append(entry)
    assert replaced, capability_id
    return AuditCapabilityMatrix(
        entries=tuple(sorted(entries, key=lambda entry: entry.identity))
    )


def test_audit_models_round_trip_through_strict_json() -> None:
    contract = _contract(profile=AnalysisProfile.HYBRID)
    record = _record(contract)
    scan = _scan(contract, record)
    models = (
        contract.source_target,
        contract.budget,
        contract.capability_matrix.entries[0].missing_outcome,
        contract.capability_matrix.entries[0],
        contract.capability_matrix,
        contract.scope_capture_policy,
        contract.model_data_egress_policy,
        contract.execution_selection,
        contract,
        record,
        scan,
    )

    for model in models:
        restored = type(model).model_validate_json(model.model_dump_json())
        assert restored == model


def test_strict_python_input_rejects_wire_enum_and_integer_coercion() -> None:
    with pytest.raises(ValidationError, match="SourceTargetKind"):
        SourceTarget(
            repository_path="/repo",
            kind="revision",  # type: ignore[arg-type]
            revision="HEAD",
        )

    payload = _payload(_budget())
    for invalid in (True, 1.0, "1"):
        payload["max_worker_jobs"] = invalid
        with pytest.raises(ValidationError, match="max_worker_jobs"):
            AuditBudget.model_validate(payload)


def test_exact_wire_enums_accept_json_and_reject_variants() -> None:
    payload = _payload(_source_target())
    json_payload = json.loads(_source_target().model_dump_json())
    restored = SourceTarget.model_validate_json(json.dumps(json_payload))
    assert restored.kind is SourceTargetKind.WORKING_TREE

    for invalid in ("Working_Tree", " working_tree", "diff", "unknown"):
        json_payload["kind"] = invalid
        with pytest.raises(ValidationError, match="kind"):
            SourceTarget.model_validate_json(json.dumps(json_payload))

    payload["kind"] = SourceTargetKind.WORKING_TREE
    assert SourceTarget.model_validate(payload).kind is SourceTargetKind.WORKING_TREE


def test_unknown_fields_fail_closed_at_top_and_nested_levels() -> None:
    contract = _contract()
    payload = _payload(contract)
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="unexpected"):
        AuditContract.model_validate(payload)

    payload = _payload(contract)
    source = dict(payload["source_target"])
    source["unexpected"] = True
    payload["source_target"] = source
    with pytest.raises(ValidationError, match="unexpected"):
        AuditContract.model_validate(payload)


def test_audit_models_are_frozen_and_forbid_unvalidated_copy_updates() -> None:
    contract = _contract()
    scan = _scan(contract)

    with pytest.raises(ValidationError, match="frozen"):
        contract.mode = AuditMode.DEEP
    with pytest.raises(ValidationError, match="frozen"):
        scan.lifecycle_status = AuditLifecycleStatus.RUNNING
    with pytest.raises(TypeError, match="unvalidated model_copy"):
        contract.model_copy(update={"mode": AuditMode.DEEP})
    with pytest.raises(TypeError, match="deprecated copy"):
        scan.copy(
            update={
                "lifecycle_status": AuditLifecycleStatus.COMPLETED,
                "terminal_outcome": AuditTerminalOutcome.COMPLETE,
                "current_phase": AuditPhase.PACKAGE_AND_PUBLISH,
            }
        )
    with pytest.raises(TypeError, match="deprecated copy"):
        contract.copy(include={"audit_id"})
    with pytest.raises(TypeError, match="deprecated copy"):
        contract.copy()

    assert isinstance(contract.detectors, tuple)
    with pytest.raises(AttributeError):
        contract.detectors.append(contract.detectors[0])  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "repository_path",
    [
        "repo",
        "~/repo",
        " /repo",
        "/repo\nsecret",
        "/repo\x00tail",
        "/repo/../secret",
        "/repo/./src",
        "/repo//src",
        "/repo/src/",
        "c:/repo",
        "C:\\repo",
        "C:/repo/",
        "C://repo",
        "C:/repo//src",
        "//Server/share/repo",
        "//server/share/",
        "\\\\server\\share\\repo",
        "x" * 4097,
    ],
)
def test_source_target_rejects_noncanonical_or_unsafe_paths(repository_path: str) -> None:
    with pytest.raises(ValidationError, match="repository_path"):
        SourceTarget(
            repository_path=repository_path,
            kind=SourceTargetKind.WORKING_TREE,
            revision="HEAD",
        )


@pytest.mark.parametrize("repository_path", ["/repo/src", "C:/repo/src", "//server/share/repo"])
def test_source_target_accepts_canonical_node_local_paths(repository_path: str) -> None:
    target = SourceTarget(
        repository_path=repository_path,
        kind=SourceTargetKind.WORKING_TREE,
        revision="HEAD",
    )
    assert target.repository_path == repository_path


@pytest.mark.parametrize("revision", ["", " HEAD", "-c", "HEAD\nmain", "x" * 1025])
def test_source_target_rejects_unsafe_revisions(revision: str) -> None:
    with pytest.raises(ValidationError, match="revision"):
        SourceTarget(
            repository_path="/repo",
            kind=SourceTargetKind.WORKING_TREE,
            revision=revision,
        )


@pytest.mark.parametrize(
    "audit_id",
    [" audit-1", "audit-1 ", "audit/1", "audit-1\n", "audit-1\x00tail"],
)
def test_audit_contract_rejects_noncanonical_or_malicious_ids(audit_id: str) -> None:
    with pytest.raises(ValidationError, match="audit_id"):
        _contract(audit_id=audit_id)


def test_revision_target_cannot_include_untracked_files() -> None:
    with pytest.raises(ValidationError, match="untracked"):
        SourceTarget(
            repository_path="/repo",
            kind=SourceTargetKind.REVISION,
            revision="HEAD",
            include_untracked=True,
        )


def test_diff_target_contract_requires_distinct_base_and_head() -> None:
    payload = _payload(_contract(mode=AuditMode.DIFF))
    source = dict(payload["source_target"])

    source["base_revision"] = None
    payload["source_target"] = source
    with pytest.raises(ValidationError, match="base revision"):
        AuditContract.model_validate(payload)

    source["base_revision"] = source["revision"]
    with pytest.raises(ValidationError, match="must differ"):
        AuditContract.model_validate(payload)


def test_non_diff_contract_rejects_base_revision() -> None:
    payload = _payload(_contract())
    source = dict(payload["source_target"])
    source["base_revision"] = "HEAD~1"
    payload["source_target"] = source
    with pytest.raises(ValidationError, match="only valid for Diff"):
        AuditContract.model_validate(payload)


def test_contract_and_scan_reject_self_baselines() -> None:
    contract_payload = _payload(_contract())
    contract_payload["baseline_audit_id"] = contract_payload["audit_id"]
    with pytest.raises(ValidationError, match="different Audit"):
        AuditContract.model_validate(contract_payload)

    scan_payload = _payload(_scan())
    scan_payload["baseline_audit_id"] = scan_payload["id"]
    with pytest.raises(ValidationError, match="different Audit"):
        AuditScan.model_validate(scan_payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("max_wall_seconds", 0),
        ("max_detector_jobs", 4_097),
        ("max_worker_jobs", 65),
        ("max_epochs", 9),
        ("max_model_calls", 101),
        ("max_input_tokens", 2_000_001),
        ("max_output_tokens", 200_001),
        ("max_read_bytes", 2_147_483_649),
        ("max_candidates", 1_001),
        ("max_signals", 16_001),
        ("max_dynamic_validations", 1_001),
        ("max_artifact_output_bytes", 268_435_457),
    ],
)
def test_budget_enforces_every_domain_hard_bound(field: str, invalid: int) -> None:
    payload = _payload(_budget())
    payload[field] = invalid
    with pytest.raises(ValidationError, match=field):
        AuditBudget.model_validate(payload)


def test_budget_digest_changes_for_every_semantic_dimension() -> None:
    budget = _budget()
    baseline = budget.digest
    for field in type(budget).model_fields:
        if field == "schema_version":
            continue
        payload = _payload(budget)
        payload[field] = payload[field] + 1  # type: ignore[operator]
        changed = AuditBudget.model_validate(payload)
        assert changed.digest != baseline, field


def test_deep_contract_requires_hybrid_and_two_epochs() -> None:
    payload = _payload(_contract(profile=AnalysisProfile.HYBRID, mode=AuditMode.DEEP))
    payload["analysis_profile"] = AnalysisProfile.DETERMINISTIC
    payload["model_profile"] = None
    payload["model_profile_digest"] = None
    payload["model_data_egress_policy"] = _egress()
    payload["budget"] = _budget(epochs=2)
    payload["capability_matrix"] = _capability_matrix(
        profile=AnalysisProfile.DETERMINISTIC,
        mode=AuditMode.DEEP,
        validation_policy=ValidationPolicy.STATIC_ONLY,
    )
    with pytest.raises(ValidationError, match="requires the hybrid"):
        AuditContract.model_validate(payload)

    payload = _payload(_contract(profile=AnalysisProfile.HYBRID, mode=AuditMode.DEEP))
    payload["budget"] = _budget(hybrid=True, epochs=1)
    with pytest.raises(ValidationError, match="at least two epochs"):
        AuditContract.model_validate(payload)


def test_validation_policy_and_dynamic_budget_must_agree() -> None:
    payload = _payload(_contract())
    payload["budget"] = _budget(dynamic=True)
    with pytest.raises(ValidationError, match="static_only.*zero dynamic"):
        AuditContract.model_validate(payload)

    payload = _payload(_contract(validation_policy=ValidationPolicy.ISOLATED_TEST))
    payload["budget"] = _budget()
    with pytest.raises(ValidationError, match="dynamic validation.*non-zero"):
        AuditContract.model_validate(payload)


def test_validation_policy_document_must_bind_schema_and_selected_policy() -> None:
    payload = _payload(_contract())
    payload["validation_policy_document"] = _policy("validation")
    with pytest.raises(ValidationError, match="unsupported schema"):
        AuditContract.model_validate(payload)

    payload = _payload(_contract())
    payload["validation_policy_document"] = _validation_policy_document(
        ValidationPolicy.ISOLATED_TEST
    )
    with pytest.raises(ValidationError, match="does not bind validation_policy"):
        AuditContract.model_validate(payload)


def test_deterministic_and_hybrid_model_contracts_fail_closed() -> None:
    payload = _payload(_contract())
    payload["model_profile"] = "primary"
    payload["model_profile_digest"] = _digest("model")
    with pytest.raises(ValidationError, match="deterministic"):
        AuditContract.model_validate(payload)

    payload = _payload(_contract(profile=AnalysisProfile.HYBRID))
    payload["model_profile"] = None
    payload["model_profile_digest"] = None
    with pytest.raises(ValidationError, match="hybrid analysis requires"):
        AuditContract.model_validate(payload)


def test_model_profile_matches_the_authoritative_run_storage_bound() -> None:
    payload = _payload(_contract(profile=AnalysisProfile.HYBRID))
    payload["model_profile"] = "m" * 255
    bounded = AuditContract.model_validate(payload)
    assert len(bounded.model_profile or "") == 255

    record = AuditContractRecord.from_contract(bounded, created_at=NOW)
    scan = _scan(bounded, record)
    assert len(scan.model_profile or "") == 255

    payload["model_profile"] = "m" * 256
    with pytest.raises(ValidationError, match="at most 255"):
        AuditContract.model_validate(payload)
    with pytest.raises(ValidationError, match="at most 255"):
        AuditScan.model_validate(
            {
                **scan.model_dump(mode="python"),
                "model_profile": "m" * 256,
            }
        )


def test_remote_egress_requires_canonical_origin_and_redaction() -> None:
    origins = ("https://model.example",)
    valid = ModelDataEgressPolicy(
        mode=ModelDataEgressMode.REMOTE_REDACTED,
        model_profile_digest=_digest("remote-model-profile"),
        endpoint_origin_digest=_domain_digest(
            "riftx.model-endpoint-origins/v1",
            json.dumps(origins, separators=(",", ":")).encode(),
        ),
        provider_display_name="Example Remote Model",
        execution_locality=ModelExecutionLocality.REMOTE_PROVIDER,
        retention_training_disclosure=_model_disclosure(remote=True),
        allowed_scope_classes=("application_code",),
        allowed_remote_origins=origins,
        max_bytes_per_call=1_000,
        max_bytes_per_audit=10_000,
        redaction_policy_version="v1",
        redaction_policy_digest=_digest("redaction"),
        operator_consent_at=NOW,
    )
    assert valid.mode is ModelDataEgressMode.REMOTE_REDACTED

    for origin in (
        "http://model.example",
        "https://MODEL.example",
        "https://model.example/",
        "https://user@model.example",
        "https://model.example:443",
    ):
        payload = _payload(valid)
        payload["allowed_remote_origins"] = (origin,)
        with pytest.raises(ValidationError, match="remote origin"):
            ModelDataEgressPolicy.model_validate(payload)

    payload = _payload(valid)
    payload["redaction_policy_digest"] = None
    with pytest.raises(ValidationError, match="redaction"):
        ModelDataEgressPolicy.model_validate(payload)

    payload = _payload(valid)
    payload["endpoint_origin_digest"] = _digest("wrong-origin-set")
    with pytest.raises(ValidationError, match="endpoint_origin_digest"):
        ModelDataEgressPolicy.model_validate(payload)


def test_model_egress_metadata_and_policy_digest_fail_closed() -> None:
    active = _egress(hybrid=True)
    assert active.model_profile_digest == _digest("model-profile")
    assert active.execution_locality is ModelExecutionLocality.LOCAL_CONTROLLED
    assert active.retention_training_disclosure is not None
    assert active.operator_consent_at == NOW
    assert active.operator_consent_requirement_digest is not None

    payload = _payload(active)
    payload["provider_display_name"] = None
    with pytest.raises(ValidationError, match="complete egress disclosure"):
        ModelDataEgressPolicy.model_validate(payload)

    payload = _payload(active)
    payload["policy_digest"] = _digest("tampered-egress-policy")
    with pytest.raises(ValidationError, match="policy_digest"):
        ModelDataEgressPolicy.model_validate(payload)

    payload = _payload(active)
    payload["operator_consent_requirement_digest"] = _digest("unbound-consent")
    with pytest.raises(ValidationError, match="consent requirement"):
        ModelDataEgressPolicy.model_validate(payload)

    payload = _payload(active)
    payload["retention_training_disclosure"] = _payload(_policy("arbitrary-disclosure"))
    with pytest.raises(ValidationError, match="retention_training_disclosure"):
        ModelDataEgressPolicy.model_validate(payload)

    disclosure_payload = _payload(_model_disclosure())
    disclosure_payload["data_residency_regions"] = ("unknown",)
    with pytest.raises(ValidationError, match="cannot be unknown"):
        ModelRetentionTrainingDisclosure.model_validate(disclosure_payload)

    payload = _payload(_egress())
    payload["provider_display_name"] = "Unexpected Provider"
    with pytest.raises(ValidationError, match="inactive model egress"):
        ModelDataEgressPolicy.model_validate(payload)


def test_capability_requirement_outcome_and_proof_are_closed() -> None:
    required = _required_capability("source_ingest")
    payload = _payload(required)
    payload["proof_digest"] = None
    with pytest.raises(ValidationError, match="supplied together"):
        AuditCapabilityRequirement.model_validate(payload)

    payload = _payload(required)
    payload["missing_outcome"] = CapabilityMissingOutcome(
        start=AuditStartMissingOutcome.CONTINUE_WITHOUT_CLAIM,
        runtime=AuditRuntimeMissingOutcome.PARTIAL_CAPABILITY,
    )
    with pytest.raises(ValidationError, match="reject Start"):
        AuditCapabilityRequirement.model_validate(payload)

    not_applicable = _not_applicable_capability("agent_hunt")
    payload = _payload(not_applicable)
    payload["provider_id"] = "fake-provider"
    with pytest.raises(ValidationError, match="cannot carry implementation proof"):
        AuditCapabilityRequirement.model_validate(payload)

    payload = _payload(not_applicable)
    payload["reason_code"] = None
    with pytest.raises(ValidationError, match="reason_code"):
        AuditCapabilityRequirement.model_validate(payload)


def test_capability_scope_and_language_dimensions_are_canonical() -> None:
    payload = _payload(_required_capability("parser"))
    payload["scope_classes"] = ("source", "config")
    with pytest.raises(ValidationError, match="sorted order"):
        AuditCapabilityRequirement.model_validate(payload)

    payload = _payload(_required_capability("parser"))
    payload["language_tiers"] = (AuditLanguageTier.TIER_B, AuditLanguageTier.TIER_A)
    with pytest.raises(ValidationError, match="sorted order"):
        AuditCapabilityRequirement.model_validate(payload)


def test_capability_matrix_rejects_duplicates_and_unsorted_entries() -> None:
    first = _required_capability("closure", phase=AuditPhase.VALIDATE_CLOSURE)
    second = _required_capability("source_ingest")
    sorted_entries = tuple(sorted((first, second), key=lambda entry: entry.identity))

    with pytest.raises(ValidationError, match="duplicate"):
        AuditCapabilityMatrix(entries=(sorted_entries[0], sorted_entries[0]))
    with pytest.raises(ValidationError, match="sorted order"):
        AuditCapabilityMatrix(entries=tuple(reversed(sorted_entries)))


def test_contract_rejects_missing_or_neutral_required_capability() -> None:
    contract = _contract()
    payload = _payload(contract)
    matrix = contract.capability_matrix
    entries = tuple(entry for entry in matrix.entries if entry.capability_id != "core_seal")
    payload["capability_matrix"] = AuditCapabilityMatrix(entries=entries)
    with pytest.raises(ValidationError, match="core_seal must be globally required"):
        AuditContract.model_validate(payload)


def test_static_profile_requires_dynamic_capabilities_to_be_not_applicable() -> None:
    contract = _contract()
    payload = _payload(contract)
    entries = tuple(
        entry
        for entry in contract.capability_matrix.entries
        if entry.capability_id != "isolated_poc"
    )
    payload["capability_matrix"] = AuditCapabilityMatrix(entries=entries)
    with pytest.raises(ValidationError, match="isolated_poc.*not_applicable"):
        AuditContract.model_validate(payload)


def test_dynamic_policy_keeps_unselected_isolated_capabilities_not_applicable() -> None:
    contract = _contract(validation_policy=ValidationPolicy.ISOLATED_TEST)
    entries = tuple(
        entry
        for entry in contract.capability_matrix.entries
        if entry.capability_id != "isolated_poc"
    )
    payload = _payload(contract)
    payload["capability_matrix"] = AuditCapabilityMatrix(entries=entries)
    with pytest.raises(ValidationError, match="isolated_poc.*globally not_applicable"):
        AuditContract.model_validate(payload)

    scoped_payload = _payload(
        _required_capability("isolated_poc", phase=AuditPhase.PROVE)
    )
    scoped_payload["scope_classes"] = ("application_code",)
    scoped = AuditCapabilityRequirement.model_validate(scoped_payload)
    payload = _payload(contract)
    payload["capability_matrix"] = AuditCapabilityMatrix(
        entries=tuple(
            sorted(
                (*contract.capability_matrix.entries, scoped),
                key=lambda entry: entry.identity,
            )
        )
    )
    with pytest.raises(ValidationError, match="isolated_poc cannot be enabled by scope"):
        AuditContract.model_validate(payload)


def test_capabilities_must_bind_frozen_execution_and_component_selection() -> None:
    contract = _contract()
    payload = _payload(contract)
    payload["capability_matrix"] = _replace_capability(
        contract.capability_matrix,
        "source_ingest",
        node_id="source-other",
    )
    with pytest.raises(ValidationError, match="source_ingest.*source node"):
        AuditContract.model_validate(payload)

    payload = _payload(contract)
    selection = dict(payload["execution_selection"])
    selection["analysis_prepare_proof_digest"] = _digest("different-analysis-proof")
    payload["execution_selection"] = selection
    with pytest.raises(ValidationError, match="analysis_backend.*analysis node"):
        AuditContract.model_validate(payload)

    payload = _payload(contract)
    payload["capability_matrix"] = _replace_capability(
        contract.capability_matrix,
        "detector:riftx_inventory",
        backend_id="analysis-other",
    )
    with pytest.raises(ValidationError, match="detector:riftx_inventory.*analysis backend"):
        AuditContract.model_validate(payload)

    hybrid = _contract(profile=AnalysisProfile.HYBRID)
    payload = _payload(hybrid)
    payload["capability_matrix"] = _replace_capability(
        hybrid.capability_matrix,
        "model_transport",
        backend_id="model-transport-other",
    )
    with pytest.raises(ValidationError, match="model_transport.*analysis backend"):
        AuditContract.model_validate(payload)

    dynamic = _contract(validation_policy=ValidationPolicy.ISOLATED_TEST)
    payload = _payload(dynamic)
    payload["capability_matrix"] = _replace_capability(
        dynamic.capability_matrix,
        "isolated_test",
        node_id="analysis-other",
    )
    with pytest.raises(ValidationError, match="isolated_test.*analysis backend"):
        AuditContract.model_validate(payload)


@pytest.mark.parametrize("field", ("source_node_id", "selected_node_id"))
def test_audit_execution_node_ids_fit_the_run_persistence_contract(field: str) -> None:
    payload = _payload(_execution_selection())
    payload[field] = "n" * 65
    with pytest.raises(ValidationError):
        AuditExecutionSelection.model_validate(payload)


def test_scoped_capability_cannot_downgrade_a_global_requirement() -> None:
    contract = _contract()
    for capability_id, phase in (
        ("core_seal", AuditPhase.SEAL_CORE),
        ("detector:riftx_inventory", AuditPhase.DETERMINISTIC_PROBE),
        ("parser:python", AuditPhase.DETERMINISTIC_PROBE),
    ):
        scoped_payload = _payload(_not_applicable_capability(capability_id, phase=phase))
        scoped_payload["scope_classes"] = ("application_code",)
        scoped = AuditCapabilityRequirement.model_validate(scoped_payload)
        entries = tuple(
            sorted(
                (*contract.capability_matrix.entries, scoped),
                key=lambda entry: entry.identity,
            )
        )
        payload = _payload(contract)
        payload["capability_matrix"] = AuditCapabilityMatrix(entries=entries)
        with pytest.raises(
            ValidationError,
            match=rf"{capability_id} cannot be downgraded by scope",
        ):
            AuditContract.model_validate(payload)


@pytest.mark.parametrize(
    ("capability_id", "component_digest"),
    [
        ("detector:riftx_inventory", _digest("detector")),
        ("parser:python", _digest("parser")),
    ],
)
def test_scoped_content_capabilities_must_remain_in_deterministic_probe(
    capability_id: str,
    component_digest: str,
) -> None:
    contract = _contract()
    scoped_payload = _payload(
        _required_capability(
            capability_id,
            phase=AuditPhase.MAP_SCOPE,
            node_id="analysis-node",
            backend_id="linux_container",
            component_digest=component_digest,
        )
    )
    scoped_payload["scope_classes"] = ("application_code",)
    scoped = AuditCapabilityRequirement.model_validate(scoped_payload)
    payload = _payload(contract)
    payload["capability_matrix"] = AuditCapabilityMatrix(
        entries=tuple(
            sorted(
                (*contract.capability_matrix.entries, scoped),
                key=lambda entry: entry.identity,
            )
        )
    )
    with pytest.raises(ValidationError, match=rf"{capability_id}.*wrong phase"):
        AuditContract.model_validate(payload)


@pytest.mark.parametrize(
    ("capability_id", "override_field"),
    [
        ("core_seal", "node_id"),
        ("source_ingest", "backend_id"),
        ("detector:riftx_inventory", "component_digest"),
        ("parser:python", "proof_digest"),
    ],
)
def test_scoped_required_capability_cannot_override_global_binding(
    capability_id: str,
    override_field: str,
) -> None:
    contract = _contract()
    global_entry = contract.capability_matrix.global_requirement(capability_id)
    assert global_entry is not None
    scoped_payload = _payload(global_entry)
    scoped_payload["scope_classes"] = ("application_code",)
    if override_field == "component_digest":
        version = dict(scoped_payload["min_version_and_digest"])
        version["component_digest"] = _digest("evil-component")
        scoped_payload["min_version_and_digest"] = version
    elif override_field == "proof_digest":
        scoped_payload["proof_digest"] = _digest("evil-proof")
    elif override_field == "node_id":
        scoped_payload["node_id"] = "evil-node"
    else:
        scoped_payload["backend_id"] = "evil-backend"
    scoped = AuditCapabilityRequirement.model_validate(scoped_payload)
    payload = _payload(contract)
    payload["capability_matrix"] = AuditCapabilityMatrix(
        entries=tuple(
            sorted(
                (*contract.capability_matrix.entries, scoped),
                key=lambda entry: entry.identity,
            )
        )
    )
    with pytest.raises(ValidationError, match=rf"{capability_id} cannot override"):
        AuditContract.model_validate(payload)


def test_versioned_policy_canonicalizes_order_and_rejects_tampering() -> None:
    first = VersionedCanonicalPolicy.from_value(
        policy_schema_version="riftx.demo/v1",
        value={"b": 2, "a": 1},
    )
    second = VersionedCanonicalPolicy.from_value(
        policy_schema_version="riftx.demo/v1",
        value={"a": 1, "b": 2},
    )
    assert first == second
    assert first.canonical_json == '{"a":1,"b":2}'

    payload = _payload(first)
    payload["canonical_json"] = '{"b":2,"a":1}'
    with pytest.raises(ValidationError, match="canonical JSON form"):
        VersionedCanonicalPolicy.model_validate(payload)

    payload = _payload(first)
    payload["digest"] = _digest("wrong")
    with pytest.raises(ValidationError, match="digest"):
        VersionedCanonicalPolicy.model_validate(payload)


def test_identical_canonical_bytes_have_domain_separated_policy_and_contract_digests() -> None:
    contract = _contract()
    policy = VersionedCanonicalPolicy.from_value(
        policy_schema_version="riftx.audit-contract/v1",
        value=json.loads(contract.canonical_json()),
    )
    assert policy.canonical_json == contract.canonical_json()
    assert policy.digest != contract.contract_digest


def test_canonical_json_rejects_extreme_nesting_without_recursing_unboundedly() -> None:
    nested_contract = '{"nested":' * 9_000 + "null" + "}" * 9_000
    payload = _payload(_record())
    payload["canonical_contract_json"] = nested_contract
    with pytest.raises(ValidationError, match="JSON depth limit"):
        AuditContractRecord.model_validate(payload)

    nested_value: dict[str, Any] = {}
    for _ in range(9_000):
        nested_value = {"nested": nested_value}
    with pytest.raises(ValueError, match="depth limit"):
        VersionedCanonicalPolicy.from_value(
            policy_schema_version="riftx.deep-policy/v1",
            value=nested_value,
        )


def test_contract_digest_is_stable_and_sensitive_to_all_frozen_inputs() -> None:
    contract = _contract()
    restored = AuditContract.model_validate_json(contract.canonical_json())
    assert restored.contract_digest == contract.contract_digest

    payload = _payload(contract)
    payload["config_digest"] = _digest("changed-config")
    changed = AuditContract.model_validate(payload)
    assert changed.contract_digest != contract.contract_digest

    payload = _payload(contract)
    selection = _payload(contract.execution_selection)
    selection["eligible_candidates_digest"] = _digest("changed-candidates")
    payload["execution_selection"] = AuditExecutionSelection.model_validate(selection)
    changed = AuditContract.model_validate(payload)
    assert changed.contract_digest != contract.contract_digest


def test_contract_record_revalidates_canonical_bytes_and_redundant_columns() -> None:
    contract = _contract()
    record = _record(contract)
    assert record.contract() == contract

    for field in (
        "contract_digest",
        "source_target_digest",
        "source_ingest_backend_digest",
        "source_prepare_proof_digest",
        "snapshot_hydration_policy_digest",
    ):
        payload = _payload(record)
        payload[field] = _digest(f"tampered:{field}")
        with pytest.raises(ValidationError, match=field):
            AuditContractRecord.model_validate(payload)

    for field, value in (
        ("audit_id", "audit-other"),
        ("source_node_id", "source-other"),
        ("selected_node_id", "analysis-other"),
        ("required_backend_id", "vm_backend"),
    ):
        payload = _payload(record)
        payload[field] = value
        with pytest.raises(ValidationError, match=field):
            AuditContractRecord.model_validate(payload)


def test_contract_record_rejects_noncanonical_duplicate_and_unknown_json() -> None:
    record = _record()
    payload = _payload(record)
    payload["canonical_contract_json"] = record.canonical_contract_json.replace(
        "{", "{ ", 1
    )
    with pytest.raises(ValidationError, match="canonical JSON form"):
        AuditContractRecord.model_validate(payload)

    payload = _payload(record)
    payload["canonical_contract_json"] = record.canonical_contract_json.replace(
        '"schema_version":"riftx.audit-contract/v1"',
        '"schema_version":"riftx.audit-contract/v1","schema_version":"riftx.audit-contract/v1"',
        1,
    )
    with pytest.raises(ValidationError, match="duplicate JSON key"):
        AuditContractRecord.model_validate(payload)

    parsed = json.loads(record.canonical_contract_json)
    parsed["unexpected"] = True
    payload = _payload(record)
    payload["canonical_contract_json"] = json.dumps(
        parsed,
        sort_keys=True,
        separators=(",", ":"),
    )
    with pytest.raises(ValidationError, match="unexpected"):
        AuditContractRecord.model_validate(payload)


def test_contract_record_checks_utf8_bytes_before_parsing() -> None:
    payload = _payload(_record())
    payload["canonical_contract_json"] = '"' + ("界" * 100_000) + '"'
    with pytest.raises(ValidationError, match="byte limit"):
        AuditContractRecord.model_validate(payload)


def test_contract_record_rejects_naive_or_reversed_seal_time() -> None:
    contract = _contract()
    with pytest.raises(ValidationError, match="timezone"):
        AuditContractRecord.from_contract(
            contract,
            created_at=NOW.replace(tzinfo=None),
        )
    with pytest.raises(ValidationError, match="must not precede"):
        AuditContractRecord.from_contract(
            contract,
            created_at=NOW,
            sealed_at=NOW - timedelta(seconds=1),
        )


def test_sensitive_path_and_contract_json_do_not_appear_in_repr_or_errors() -> None:
    canary = "/srv/authorized/CANARY_PRIVATE_REPOSITORY"
    target = SourceTarget(
        repository_path=canary,
        kind=SourceTargetKind.WORKING_TREE,
        revision="HEAD",
    )
    assert canary not in repr(target)

    record = _record()
    assert record.canonical_contract_json not in repr(record)
    payload = _payload(record)
    payload["contract_digest"] = "INVALID-CANARY-DIGEST"
    with pytest.raises(ValidationError) as exc_info:
        AuditContractRecord.model_validate(payload)
    assert record.canonical_contract_json not in str(exc_info.value)


def test_contract_record_seal_is_immutable_and_atomic() -> None:
    record = _record()
    sealed = record.seal(at=NOW + timedelta(seconds=1))
    assert record.sealed_at is None
    assert sealed.sealed_at == NOW + timedelta(seconds=1)
    assert sealed.seal(at=NOW + timedelta(seconds=2)) is sealed

    with pytest.raises(ValidationError, match="must not precede"):
        record.seal(at=NOW - timedelta(seconds=1))
    assert record.sealed_at is None


def test_audit_scan_initial_projection_and_contract_binding() -> None:
    contract = _contract()
    record = _record(contract)
    scan = _scan(contract, record)
    assert scan.lifecycle_status is AuditLifecycleStatus.DRAFT
    assert scan.current_phase is AuditPhase.AUTHORIZE_AND_FREEZE
    assert scan.publication_status is AuditPublicationStatus.NOT_STARTED
    assert scan.validate_contract_record(record) == contract

    payload = _payload(scan)
    payload["contract_id"] = "contract-other"
    mismatched = AuditScan.model_validate(payload)
    with pytest.raises(ValueError, match="contract_id"):
        mismatched.validate_contract_record(record)


def test_audit_scan_accepts_deterministic_workflow_id_for_maximum_length_audit_id() -> None:
    scan = _scan()
    audit_id = "a" * 128
    workflow_id = f"riftx-code-audit-{audit_id}"

    persisted = AuditScan.model_validate(
        {
            **scan.model_dump(mode="python"),
            "id": audit_id,
            "temporal_workflow_id": workflow_id,
        }
    )

    assert persisted.temporal_workflow_id == workflow_id

    with pytest.raises(ValidationError, match="must be deterministic"):
        AuditScan.model_validate(
            {
                **scan.model_dump(mode="python"),
                "temporal_workflow_id": "riftx-code-audit-wrong",
            }
        )


def test_contract_binding_revalidates_model_constructed_record() -> None:
    contract = _contract()
    record = _record(contract)
    scan = _scan(contract, record)
    canonical_payload = json.loads(record.canonical_contract_json)
    canonical_payload["source_target"]["revision"] = "refs/heads/attacker-selected"
    forged_payload = _payload(record)
    forged_payload["canonical_contract_json"] = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    forged = AuditContractRecord.model_construct(**forged_payload)

    assert forged.contract_digest == record.contract_digest
    assert forged.source_target_digest == record.source_target_digest
    with pytest.raises(ValidationError, match="does not match canonical contract"):
        scan.validate_contract_record(forged)


def test_contract_binding_revalidates_model_constructed_scan() -> None:
    contract = _contract()
    record = _record(contract)
    scan_payload = _payload(_scan(contract, record))
    scan_payload.update(
        lifecycle_status=AuditLifecycleStatus.COMPLETED,
        terminal_outcome=AuditTerminalOutcome.COMPLETE,
        current_phase=AuditPhase.PACKAGE_AND_PUBLISH,
    )
    forged = AuditScan.model_construct(**scan_payload)

    with pytest.raises(ValidationError):
        forged.validate_contract_record(record)


def test_contract_record_must_be_sealed_before_audit_start() -> None:
    contract = _contract()
    record = _record(contract)
    scan = _scan(contract, record).transition_to(
        AuditLifecycleStatus.QUEUED,
        at=NOW + timedelta(seconds=2),
    )
    with pytest.raises(ValueError, match="requires a sealed"):
        scan.validate_contract_record(record)

    sealed = record.seal(at=NOW + timedelta(seconds=1))
    assert scan.validate_contract_record(sealed) == contract

    sealed_too_late = record.seal(at=NOW + timedelta(seconds=3))
    with pytest.raises(ValueError, match="sealed before Audit start"):
        scan.validate_contract_record(sealed_too_late)


def test_audit_scan_purpose_requires_the_correct_parent_relationship() -> None:
    payload = _payload(_scan())
    payload["purpose"] = AuditPurpose.VALIDATION_FOLLOWUP
    with pytest.raises(ValidationError, match="parent_audit_id"):
        AuditScan.model_validate(payload)

    payload["parent_audit_id"] = "audit-parent"
    followup = AuditScan.model_validate(payload)
    assert followup.purpose is AuditPurpose.VALIDATION_FOLLOWUP

    payload = _payload(_scan())
    payload["parent_audit_id"] = "audit-parent"
    with pytest.raises(ValidationError, match="primary Audit"):
        AuditScan.model_validate(payload)


def test_snapshot_binding_is_one_time_idempotent_and_mode_aware() -> None:
    scan = _scan()
    bound = scan.bind_snapshots(snapshot_id="snapshot-head")
    assert scan.snapshot_id is None
    assert bound.snapshot_id == "snapshot-head"
    assert bound.bind_snapshots(snapshot_id="snapshot-head") is bound
    with pytest.raises(ValueError, match="immutable"):
        bound.bind_snapshots(snapshot_id="snapshot-other")

    diff_contract = _contract(mode=AuditMode.DIFF)
    diff_scan = _scan(diff_contract)
    with pytest.raises(ValueError, match="both head and base"):
        diff_scan.bind_snapshots(snapshot_id="snapshot-head")
    diff_bound = diff_scan.bind_snapshots(
        snapshot_id="snapshot-head",
        base_snapshot_id="snapshot-base",
    )
    assert diff_bound.base_snapshot_id == "snapshot-base"


def test_running_requires_snapshot_and_diff_requires_base_snapshot() -> None:
    scan = _scan().transition_to(AuditLifecycleStatus.QUEUED, at=NOW)
    scan = scan.transition_to(AuditLifecycleStatus.PREFLIGHTING)
    scan = scan.transition_to(AuditLifecycleStatus.SNAPSHOTTING)
    with pytest.raises(ValidationError, match="sealed snapshot"):
        scan.transition_to(AuditLifecycleStatus.RUNNING)
    assert scan.lifecycle_status is AuditLifecycleStatus.SNAPSHOTTING

    diff_contract = _contract(mode=AuditMode.DIFF)
    diff_scan = _scan(diff_contract).transition_to(AuditLifecycleStatus.QUEUED, at=NOW)
    diff_scan = diff_scan.transition_to(AuditLifecycleStatus.PREFLIGHTING)
    diff_scan = diff_scan.transition_to(AuditLifecycleStatus.SNAPSHOTTING)
    payload = _payload(diff_scan)
    payload["snapshot_id"] = "snapshot-head"
    with pytest.raises(ValidationError, match="base snapshot"):
        AuditScan.model_validate({**payload, "lifecycle_status": AuditLifecycleStatus.RUNNING})


@pytest.mark.parametrize(
    ("lifecycle", "outcome"),
    [
        (AuditLifecycleStatus.FAILING, AuditTerminalOutcome.FAILED),
        (AuditLifecycleStatus.CANCELLING, AuditTerminalOutcome.CANCELLED),
    ],
)
def test_analysis_phase_effect_stop_still_requires_start_and_snapshot(
    lifecycle: AuditLifecycleStatus,
    outcome: AuditTerminalOutcome,
) -> None:
    payload = _payload(_scan())
    payload.update(
        lifecycle_status=lifecycle,
        current_phase=AuditPhase.PROVE,
        terminal_outcome=outcome,
    )
    with pytest.raises(ValidationError, match="sealed snapshot"):
        AuditScan.model_validate(payload)

    payload["snapshot_id"] = "snapshot-head"
    with pytest.raises(ValidationError, match="requires started_at"):
        AuditScan.model_validate(payload)

    payload["started_at"] = NOW
    assert AuditScan.model_validate(payload).lifecycle_status is lifecycle


def _started_running_scan() -> AuditScan:
    scan = _scan().transition_to(AuditLifecycleStatus.QUEUED, at=NOW + timedelta(seconds=1))
    scan = scan.transition_to(AuditLifecycleStatus.PREFLIGHTING)
    scan = scan.transition_to(AuditLifecycleStatus.SNAPSHOTTING)
    scan = scan.bind_snapshots(snapshot_id="snapshot-head")
    return scan.transition_to(AuditLifecycleStatus.RUNNING)


def _running_scan() -> AuditScan:
    scan = _started_running_scan()
    for phase in (
        AuditPhase.DETERMINISTIC_PROBE,
        AuditPhase.THREAT_MODEL,
        AuditPhase.AGENT_HUNT,
        AuditPhase.RECONCILE,
        AuditPhase.PROVE,
        AuditPhase.COMPOSE_RISK,
        AuditPhase.COMPARE_BASELINE,
        AuditPhase.VALIDATE_CLOSURE,
    ):
        scan = scan.transition_phase_to(phase)
    return scan


def _converge_cleanup(scan: AuditScan) -> AuditScan:
    assert scan.terminal_outcome is not None
    run_status = {
        AuditTerminalOutcome.COMPLETE: RunStatus.COMPLETED,
        AuditTerminalOutcome.PARTIAL: RunStatus.COMPLETED,
        AuditTerminalOutcome.FAILED: RunStatus.FAILED,
        AuditTerminalOutcome.CANCELLED: RunStatus.CANCELLED,
    }[scan.terminal_outcome]
    return scan.record_cleanup_convergence(
        cleanup_proof_digest=_digest(f"cleanup:{scan.id}:{scan.terminal_outcome.value}"),
        run_terminal_status=run_status,
    )


def test_happy_lifecycle_seals_publishes_and_completes() -> None:
    scan = _running_scan()
    scan = scan.transition_to(
        AuditLifecycleStatus.FINALIZING,
        terminal_outcome=AuditTerminalOutcome.COMPLETE,
    )
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    scan = _converge_cleanup(scan)
    scan = scan.record_closure(AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE)
    scan = scan.transition_to(AuditLifecycleStatus.SEALING_CORE, at=NOW + timedelta(minutes=1))
    scan = scan.record_core_seal(
        core_seal_root=_digest("core-seal"),
        at=NOW + timedelta(minutes=2),
    )
    scan = scan.transition_to(AuditLifecycleStatus.REPORTING)
    scan = scan.transition_to(AuditLifecycleStatus.PACKAGING)
    scan = scan.record_distribution_revision(
        revision_id="distribution-1",
        at=NOW + timedelta(minutes=3),
    )
    scan = scan.transition_to(AuditLifecycleStatus.COMPLETED)

    assert scan.lifecycle_status is AuditLifecycleStatus.COMPLETED
    assert scan.publication_status is AuditPublicationStatus.PUBLISHED
    assert scan.current_phase is AuditPhase.PACKAGE_AND_PUBLISH
    assert scan.core_seal_root == _digest("core-seal")
    assert not scan.can_transition_to(AuditLifecycleStatus.RUNNING)


def test_early_cancellation_can_package_partial_facts_without_snapshot() -> None:
    scan = _scan().transition_to(AuditLifecycleStatus.CANCELLING)
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    scan = _converge_cleanup(scan)
    scan = scan.record_closure(AuditClosureStatus.CANCELLED)
    scan = scan.transition_to(AuditLifecycleStatus.SEALING_CORE)
    scan = scan.record_core_seal(core_seal_root=_digest("cancelled-core"))
    scan = scan.transition_to(AuditLifecycleStatus.REPORTING)
    scan = scan.transition_to(AuditLifecycleStatus.PACKAGING)
    scan = scan.record_distribution_revision(revision_id="cancelled-report-1")
    scan = scan.transition_to(AuditLifecycleStatus.CANCELLED)

    assert scan.snapshot_id is None
    assert scan.lifecycle_status is AuditLifecycleStatus.CANCELLED
    assert scan.closure_status is AuditClosureStatus.CANCELLED


def test_report_failure_preserves_complete_closure_but_projects_partial_lifecycle() -> None:
    scan = _running_scan().transition_to(
        AuditLifecycleStatus.FINALIZING,
        terminal_outcome=AuditTerminalOutcome.COMPLETE,
    )
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    scan = _converge_cleanup(scan)
    scan = scan.record_closure(AuditClosureStatus.COMPLETE_WITH_POLICY_EXCLUSIONS)
    scan = scan.transition_to(AuditLifecycleStatus.SEALING_CORE)
    scan = scan.record_core_seal(core_seal_root=_digest("complete-core"))
    scan = scan.transition_to(AuditLifecycleStatus.REPORTING)
    scan = scan.record_publication_failure(AuditPublicationStatus.REPORT_FAILED)
    scan = scan.transition_to(AuditLifecycleStatus.COMPLETED_PARTIAL)

    assert scan.terminal_outcome is AuditTerminalOutcome.COMPLETE
    assert scan.closure_status is AuditClosureStatus.COMPLETE_WITH_POLICY_EXCLUSIONS
    assert scan.publication_status is AuditPublicationStatus.REPORT_FAILED


def test_terminal_publication_retry_resumes_only_failed_publication() -> None:
    scan = _running_scan().transition_to(
        AuditLifecycleStatus.FINALIZING,
        terminal_outcome=AuditTerminalOutcome.COMPLETE,
    )
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    scan = _converge_cleanup(scan)
    scan = scan.record_closure(AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE)
    scan = scan.transition_to(AuditLifecycleStatus.SEALING_CORE)
    scan = scan.record_core_seal(core_seal_root=_digest("retry-core"))

    with pytest.raises(InvalidStateTransitionError):
        scan.record_publication_failure(AuditPublicationStatus.REPORT_FAILED)
    with pytest.raises(ValueError, match="packaging publication state"):
        scan.record_distribution_revision(revision_id="premature-distribution")

    scan = scan.transition_to(AuditLifecycleStatus.REPORTING)
    scan = scan.record_publication_failure(AuditPublicationStatus.REPORT_FAILED)
    terminal = scan.transition_to(AuditLifecycleStatus.COMPLETED_PARTIAL)
    assert terminal.publication_status is AuditPublicationStatus.REPORT_FAILED

    retry = terminal.begin_publication_retry()
    assert retry.lifecycle_status is AuditLifecycleStatus.COMPLETED_PARTIAL
    assert retry.terminal_outcome is AuditTerminalOutcome.COMPLETE
    assert retry.closure_status is AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE
    assert retry.publication_status is AuditPublicationStatus.REPORT_PENDING
    assert retry.current_phase is AuditPhase.GENERATE_REPORTS

    retry = retry.transition_terminal_publication_to(AuditPublicationStatus.REPORTING)
    retry = retry.transition_terminal_publication_to(AuditPublicationStatus.PACKAGING)
    published = retry.record_distribution_revision(revision_id="retry-distribution")

    assert published.lifecycle_status is AuditLifecycleStatus.COMPLETED
    assert published.publication_status is AuditPublicationStatus.PUBLISHED
    assert published.initial_distribution_revision_id == "retry-distribution"
    assert published.latest_distribution_revision_id == "retry-distribution"
    with pytest.raises(ValueError, match="failed publication state"):
        published.begin_publication_retry()


def test_failed_terminal_retries_seal_report_and_package_without_reopening_analysis() -> None:
    scan = _scan().transition_to(AuditLifecycleStatus.FAILING)
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    scan = _converge_cleanup(scan)
    scan = scan.record_closure(AuditClosureStatus.FAILED)
    scan = scan.transition_to(
        AuditLifecycleStatus.SEALING_CORE,
        at=NOW + timedelta(minutes=1),
    )
    scan = scan.record_publication_failure(AuditPublicationStatus.SEAL_FAILED)
    terminal = scan.transition_to(AuditLifecycleStatus.FAILED)
    analysis_finished_at = terminal.analysis_finished_at

    retry = terminal.begin_publication_retry()
    assert retry.lifecycle_status is AuditLifecycleStatus.FAILED
    assert retry.terminal_outcome is AuditTerminalOutcome.FAILED
    assert retry.closure_status is AuditClosureStatus.FAILED
    assert retry.snapshot_id is None
    assert retry.core_seal_root is None

    retry = retry.record_core_seal(
        core_seal_root=_digest("failed-retry-core"),
        at=NOW + timedelta(minutes=2),
    )
    assert retry.record_core_seal(core_seal_root=_digest("failed-retry-core")) is retry
    with pytest.raises(ValueError, match="active sealing_core"):
        retry.record_core_seal(core_seal_root=_digest("rewritten-core"))

    retry = retry.transition_terminal_publication_to(AuditPublicationStatus.REPORTING)
    retry = retry.transition_terminal_publication_to(AuditPublicationStatus.PACKAGING)
    published = retry.record_distribution_revision(
        revision_id="failed-retry-distribution",
        at=NOW + timedelta(minutes=3),
    )

    assert published.lifecycle_status is AuditLifecycleStatus.FAILED
    assert published.terminal_outcome is AuditTerminalOutcome.FAILED
    assert published.closure_status is AuditClosureStatus.FAILED
    assert published.core_seal_root == _digest("failed-retry-core")
    assert published.analysis_finished_at == analysis_finished_at
    assert published.publication_status is AuditPublicationStatus.PUBLISHED


def test_cancelled_terminal_package_retry_preserves_closure_and_core() -> None:
    scan = _scan().transition_to(AuditLifecycleStatus.CANCELLING)
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    scan = _converge_cleanup(scan)
    scan = scan.record_closure(AuditClosureStatus.CANCELLED)
    scan = scan.transition_to(
        AuditLifecycleStatus.SEALING_CORE,
        at=NOW + timedelta(minutes=1),
    )
    scan = scan.record_core_seal(
        core_seal_root=_digest("cancelled-retry-core"),
        at=NOW + timedelta(minutes=2),
    )
    scan = scan.transition_to(AuditLifecycleStatus.REPORTING)
    scan = scan.transition_to(AuditLifecycleStatus.PACKAGING)
    scan = scan.record_publication_failure(AuditPublicationStatus.PACKAGE_FAILED)
    terminal = scan.transition_to(AuditLifecycleStatus.CANCELLED)
    analysis_finished_at = terminal.analysis_finished_at

    retry = terminal.begin_publication_retry()
    assert retry.lifecycle_status is AuditLifecycleStatus.CANCELLED
    assert retry.terminal_outcome is AuditTerminalOutcome.CANCELLED
    assert retry.closure_status is AuditClosureStatus.CANCELLED
    assert retry.core_seal_root == _digest("cancelled-retry-core")
    with pytest.raises(ValueError, match="active sealing_core"):
        retry.record_core_seal(core_seal_root=_digest("rewritten-core"))

    published = retry.record_distribution_revision(
        revision_id="cancelled-retry-distribution",
        at=NOW + timedelta(minutes=3),
    )
    assert published.lifecycle_status is AuditLifecycleStatus.CANCELLED
    assert published.terminal_outcome is AuditTerminalOutcome.CANCELLED
    assert published.closure_status is AuditClosureStatus.CANCELLED
    assert published.core_seal_root == _digest("cancelled-retry-core")
    assert published.analysis_finished_at == analysis_finished_at
    assert published.publication_status is AuditPublicationStatus.PUBLISHED


def test_terminal_transition_requires_final_publication_projection() -> None:
    scan = _running_scan().transition_to(
        AuditLifecycleStatus.FINALIZING,
        terminal_outcome=AuditTerminalOutcome.COMPLETE,
    )
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    scan = _converge_cleanup(scan)
    scan = scan.record_closure(AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE)
    scan = scan.transition_to(AuditLifecycleStatus.SEALING_CORE)
    with pytest.raises(ValueError, match="published or failed publication"):
        scan.transition_to(AuditLifecycleStatus.COMPLETED_PARTIAL)


def test_active_publication_states_reject_prefilled_distribution_facts() -> None:
    scan = _running_scan().transition_to(
        AuditLifecycleStatus.FINALIZING,
        terminal_outcome=AuditTerminalOutcome.COMPLETE,
    )
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    scan = _converge_cleanup(scan)
    scan = scan.record_closure(AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE)
    sealing = scan.transition_to(
        AuditLifecycleStatus.SEALING_CORE,
        at=NOW + timedelta(minutes=1),
    )
    reporting = sealing.record_core_seal(
        core_seal_root=_digest("distribution-guard-core"),
        at=NOW + timedelta(minutes=2),
    ).transition_to(AuditLifecycleStatus.REPORTING)

    for active in (sealing, reporting):
        payload = _payload(active)
        payload.update(
            initial_distribution_revision_id="premature-revision",
            latest_distribution_revision_id="premature-revision",
            publication_finished_at=NOW + timedelta(minutes=3),
        )
        with pytest.raises(ValidationError, match="require published publication_status"):
            AuditScan.model_validate(payload)


def test_lifecycle_transition_failure_is_atomic_for_naive_and_backward_times() -> None:
    scan = _scan()
    with pytest.raises(ValidationError, match="timezone"):
        scan.transition_to(
            AuditLifecycleStatus.QUEUED,
            at=NOW.replace(tzinfo=None),
        )
    assert scan.lifecycle_status is AuditLifecycleStatus.DRAFT
    assert scan.started_at is None

    with pytest.raises(ValidationError, match="must not precede"):
        scan.transition_to(
            AuditLifecycleStatus.QUEUED,
            at=NOW - timedelta(seconds=1),
        )
    assert scan.lifecycle_status is AuditLifecycleStatus.DRAFT


def test_pause_and_approval_paths_preserve_current_phase() -> None:
    scan = _started_running_scan().transition_phase_to(AuditPhase.DETERMINISTIC_PROBE)
    phase = scan.current_phase
    for target in (
        AuditLifecycleStatus.WAITING_APPROVAL,
        AuditLifecycleStatus.RUNNING,
        AuditLifecycleStatus.PAUSING,
        AuditLifecycleStatus.PAUSED,
        AuditLifecycleStatus.RUNNING,
    ):
        scan = scan.transition_to(target)
        assert scan.current_phase is phase


def test_pausing_can_fail_and_failure_can_escalate_to_cancellation() -> None:
    scan = _started_running_scan().transition_to(AuditLifecycleStatus.PAUSING)
    failing = scan.transition_to(AuditLifecycleStatus.FAILING)
    assert failing.terminal_outcome is AuditTerminalOutcome.FAILED

    cancelling = failing.transition_to(AuditLifecycleStatus.CANCELLING)
    assert cancelling.terminal_outcome is AuditTerminalOutcome.CANCELLED


def test_phase_progression_is_monotonic_with_explicit_cleanup_jump() -> None:
    scan = _started_running_scan()
    advanced = scan.transition_phase_to(AuditPhase.DETERMINISTIC_PROBE)
    with pytest.raises(InvalidStateTransitionError):
        advanced.transition_phase_to(AuditPhase.PROVE)
    with pytest.raises(InvalidStateTransitionError):
        advanced.transition_phase_to(AuditPhase.AUTHORIZE_AND_FREEZE)

    failing = advanced.transition_to(AuditLifecycleStatus.FAILING)
    cleanup = failing.transition_to(AuditLifecycleStatus.CLEANING)
    assert cleanup.current_phase is AuditPhase.CLEANUP
    cleanup = _converge_cleanup(cleanup)
    cleanup = cleanup.record_closure(AuditClosureStatus.FAILED)
    sealing = cleanup.transition_to(AuditLifecycleStatus.SEALING_CORE)
    assert sealing.current_phase is AuditPhase.SEAL_CORE


def test_closure_and_terminal_lifecycle_cannot_be_cross_wired() -> None:
    scan = _running_scan().transition_to(
        AuditLifecycleStatus.FINALIZING,
        terminal_outcome=AuditTerminalOutcome.COMPLETE,
    )
    with pytest.raises(ValueError, match="cleanup convergence"):
        scan.record_closure(AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE)
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    with pytest.raises(ValueError, match="Run terminal proof"):
        scan.record_closure(AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE)
    scan = _converge_cleanup(scan)
    with pytest.raises(ValueError, match="does not match"):
        scan.record_closure(AuditClosureStatus.PARTIAL_BUDGET)

    payload = _payload(scan)
    payload.update(
        lifecycle_status=AuditLifecycleStatus.FAILED,
        current_phase=AuditPhase.GENERATE_REPORTS,
        cleanup_proof_digest=_digest("cross-wired-cleanup"),
        run_terminal_status=RunStatus.COMPLETED,
        closure_status=AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE,
        publication_status=AuditPublicationStatus.REPORT_FAILED,
        core_seal_root=_digest("cross-wired-core"),
        analysis_finished_at=NOW + timedelta(seconds=1),
        sealed_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ValidationError, match="terminal lifecycle"):
        AuditScan.model_validate(payload)


def test_cleanup_convergence_requires_matching_immutable_run_terminal_proof() -> None:
    scan = _running_scan().transition_to(
        AuditLifecycleStatus.FINALIZING,
        terminal_outcome=AuditTerminalOutcome.COMPLETE,
    )
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)

    with pytest.raises(ValueError, match="does not match terminal_outcome"):
        scan.record_cleanup_convergence(
            cleanup_proof_digest=_digest("wrong-run-status"),
            run_terminal_status=RunStatus.FAILED,
        )
    with pytest.raises(ValidationError, match="cleanup_proof_digest"):
        scan.record_cleanup_convergence(
            cleanup_proof_digest="not-a-digest",
            run_terminal_status=RunStatus.COMPLETED,
        )

    converged = _converge_cleanup(scan)
    assert _converge_cleanup(converged) is converged
    with pytest.raises(ValueError, match="immutable"):
        converged.record_cleanup_convergence(
            cleanup_proof_digest=_digest("replacement-proof"),
            run_terminal_status=RunStatus.COMPLETED,
        )
    with pytest.raises(ValueError, match="converged cleanup outcome"):
        converged.transition_to(AuditLifecycleStatus.FAILING)


def test_cancelled_cleanup_outcome_cannot_be_downgraded_to_failure() -> None:
    scan = _scan().transition_to(AuditLifecycleStatus.CANCELLING)
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    with pytest.raises(ValueError, match="cannot be downgraded"):
        scan.transition_to(AuditLifecycleStatus.FAILING)


_EXPECTED_AUDIT_EDGES = {
    (AuditLifecycleStatus.DRAFT, AuditLifecycleStatus.QUEUED),
    (AuditLifecycleStatus.DRAFT, AuditLifecycleStatus.CANCELLING),
    (AuditLifecycleStatus.DRAFT, AuditLifecycleStatus.FAILING),
    (AuditLifecycleStatus.QUEUED, AuditLifecycleStatus.PREFLIGHTING),
    (AuditLifecycleStatus.QUEUED, AuditLifecycleStatus.CANCELLING),
    (AuditLifecycleStatus.QUEUED, AuditLifecycleStatus.FAILING),
    (AuditLifecycleStatus.PREFLIGHTING, AuditLifecycleStatus.SNAPSHOTTING),
    (AuditLifecycleStatus.PREFLIGHTING, AuditLifecycleStatus.CANCELLING),
    (AuditLifecycleStatus.PREFLIGHTING, AuditLifecycleStatus.FAILING),
    (AuditLifecycleStatus.SNAPSHOTTING, AuditLifecycleStatus.RUNNING),
    (AuditLifecycleStatus.SNAPSHOTTING, AuditLifecycleStatus.CANCELLING),
    (AuditLifecycleStatus.SNAPSHOTTING, AuditLifecycleStatus.FAILING),
    (AuditLifecycleStatus.RUNNING, AuditLifecycleStatus.WAITING_APPROVAL),
    (AuditLifecycleStatus.RUNNING, AuditLifecycleStatus.PAUSING),
    (AuditLifecycleStatus.RUNNING, AuditLifecycleStatus.FINALIZING),
    (AuditLifecycleStatus.RUNNING, AuditLifecycleStatus.CANCELLING),
    (AuditLifecycleStatus.RUNNING, AuditLifecycleStatus.FAILING),
    (AuditLifecycleStatus.WAITING_APPROVAL, AuditLifecycleStatus.RUNNING),
    (AuditLifecycleStatus.WAITING_APPROVAL, AuditLifecycleStatus.PAUSING),
    (AuditLifecycleStatus.WAITING_APPROVAL, AuditLifecycleStatus.CANCELLING),
    (AuditLifecycleStatus.WAITING_APPROVAL, AuditLifecycleStatus.FAILING),
    (AuditLifecycleStatus.PAUSING, AuditLifecycleStatus.PAUSED),
    (AuditLifecycleStatus.PAUSING, AuditLifecycleStatus.CANCELLING),
    (AuditLifecycleStatus.PAUSING, AuditLifecycleStatus.FAILING),
    (AuditLifecycleStatus.PAUSED, AuditLifecycleStatus.RUNNING),
    (AuditLifecycleStatus.PAUSED, AuditLifecycleStatus.CANCELLING),
    (AuditLifecycleStatus.PAUSED, AuditLifecycleStatus.FAILING),
    (AuditLifecycleStatus.FINALIZING, AuditLifecycleStatus.CLEANING),
    (AuditLifecycleStatus.FINALIZING, AuditLifecycleStatus.CANCELLING),
    (AuditLifecycleStatus.FINALIZING, AuditLifecycleStatus.FAILING),
    (AuditLifecycleStatus.CANCELLING, AuditLifecycleStatus.CLEANING),
    (AuditLifecycleStatus.FAILING, AuditLifecycleStatus.CANCELLING),
    (AuditLifecycleStatus.FAILING, AuditLifecycleStatus.CLEANING),
    (AuditLifecycleStatus.CLEANING, AuditLifecycleStatus.SEALING_CORE),
    (AuditLifecycleStatus.CLEANING, AuditLifecycleStatus.CANCELLING),
    (AuditLifecycleStatus.CLEANING, AuditLifecycleStatus.FAILING),
    (AuditLifecycleStatus.SEALING_CORE, AuditLifecycleStatus.REPORTING),
    (AuditLifecycleStatus.SEALING_CORE, AuditLifecycleStatus.COMPLETED_PARTIAL),
    (AuditLifecycleStatus.SEALING_CORE, AuditLifecycleStatus.FAILED),
    (AuditLifecycleStatus.SEALING_CORE, AuditLifecycleStatus.CANCELLED),
    (AuditLifecycleStatus.REPORTING, AuditLifecycleStatus.PACKAGING),
    (AuditLifecycleStatus.REPORTING, AuditLifecycleStatus.COMPLETED_PARTIAL),
    (AuditLifecycleStatus.REPORTING, AuditLifecycleStatus.FAILED),
    (AuditLifecycleStatus.REPORTING, AuditLifecycleStatus.CANCELLED),
    (AuditLifecycleStatus.PACKAGING, AuditLifecycleStatus.COMPLETED),
    (AuditLifecycleStatus.PACKAGING, AuditLifecycleStatus.COMPLETED_PARTIAL),
    (AuditLifecycleStatus.PACKAGING, AuditLifecycleStatus.FAILED),
    (AuditLifecycleStatus.PACKAGING, AuditLifecycleStatus.CANCELLED),
}


@pytest.mark.parametrize(("current", "target"), product(AuditLifecycleStatus, repeat=2))
def test_lifecycle_transition_predicate_is_an_exhaustive_allowlist(
    current: AuditLifecycleStatus,
    target: AuditLifecycleStatus,
) -> None:
    payload = _payload(_scan())
    payload["lifecycle_status"] = current
    if current is not AuditLifecycleStatus.DRAFT:
        payload["started_at"] = NOW

    active_analysis = {
        AuditLifecycleStatus.RUNNING,
        AuditLifecycleStatus.WAITING_APPROVAL,
        AuditLifecycleStatus.PAUSING,
        AuditLifecycleStatus.PAUSED,
    }
    complete_analysis = {
        AuditLifecycleStatus.FINALIZING,
        AuditLifecycleStatus.CLEANING,
        AuditLifecycleStatus.SEALING_CORE,
        AuditLifecycleStatus.REPORTING,
        AuditLifecycleStatus.PACKAGING,
        AuditLifecycleStatus.COMPLETED,
        AuditLifecycleStatus.COMPLETED_PARTIAL,
    }
    if current in active_analysis | complete_analysis:
        payload["snapshot_id"] = "snapshot-head"
    if current in active_analysis:
        payload["current_phase"] = AuditPhase.MAP_SCOPE
    elif current is AuditLifecycleStatus.FINALIZING:
        payload["current_phase"] = AuditPhase.VALIDATE_CLOSURE
    elif current is AuditLifecycleStatus.CLEANING:
        payload["current_phase"] = AuditPhase.CLEANUP
    elif current is AuditLifecycleStatus.SEALING_CORE:
        payload["current_phase"] = AuditPhase.SEAL_CORE
    elif current is AuditLifecycleStatus.REPORTING:
        payload["current_phase"] = AuditPhase.GENERATE_REPORTS
    elif current in {
        AuditLifecycleStatus.PACKAGING,
        AuditLifecycleStatus.COMPLETED,
        AuditLifecycleStatus.COMPLETED_PARTIAL,
        AuditLifecycleStatus.FAILED,
        AuditLifecycleStatus.CANCELLED,
    }:
        payload["current_phase"] = AuditPhase.PACKAGE_AND_PUBLISH

    if current in {
        AuditLifecycleStatus.FINALIZING,
        AuditLifecycleStatus.CLEANING,
        AuditLifecycleStatus.SEALING_CORE,
        AuditLifecycleStatus.REPORTING,
        AuditLifecycleStatus.PACKAGING,
        AuditLifecycleStatus.COMPLETED,
    }:
        payload["terminal_outcome"] = AuditTerminalOutcome.COMPLETE
    elif current is AuditLifecycleStatus.COMPLETED_PARTIAL:
        payload["terminal_outcome"] = AuditTerminalOutcome.PARTIAL
    elif current in {AuditLifecycleStatus.FAILING, AuditLifecycleStatus.FAILED}:
        payload["terminal_outcome"] = AuditTerminalOutcome.FAILED
    elif current in {AuditLifecycleStatus.CANCELLING, AuditLifecycleStatus.CANCELLED}:
        payload["terminal_outcome"] = AuditTerminalOutcome.CANCELLED
    if current in {
        AuditLifecycleStatus.SEALING_CORE,
        AuditLifecycleStatus.REPORTING,
        AuditLifecycleStatus.PACKAGING,
        AuditLifecycleStatus.COMPLETED,
        AuditLifecycleStatus.COMPLETED_PARTIAL,
        AuditLifecycleStatus.FAILED,
        AuditLifecycleStatus.CANCELLED,
    }:
        payload["analysis_finished_at"] = NOW

    if current in {
        AuditLifecycleStatus.SEALING_CORE,
        AuditLifecycleStatus.REPORTING,
        AuditLifecycleStatus.PACKAGING,
        AuditLifecycleStatus.COMPLETED,
        AuditLifecycleStatus.COMPLETED_PARTIAL,
        AuditLifecycleStatus.FAILED,
        AuditLifecycleStatus.CANCELLED,
    }:
        outcome = payload["terminal_outcome"]
        assert isinstance(outcome, AuditTerminalOutcome)
        payload["cleanup_proof_digest"] = _digest(f"cleanup:{current.value}")
        payload["run_terminal_status"] = {
            AuditTerminalOutcome.COMPLETE: RunStatus.COMPLETED,
            AuditTerminalOutcome.PARTIAL: RunStatus.COMPLETED,
            AuditTerminalOutcome.FAILED: RunStatus.FAILED,
            AuditTerminalOutcome.CANCELLED: RunStatus.CANCELLED,
        }[outcome]

    if current in {
        AuditLifecycleStatus.SEALING_CORE,
        AuditLifecycleStatus.REPORTING,
        AuditLifecycleStatus.PACKAGING,
    }:
        payload["closure_status"] = AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE
    if current is AuditLifecycleStatus.SEALING_CORE:
        payload["publication_status"] = AuditPublicationStatus.SEALING_CORE
    elif current in {AuditLifecycleStatus.REPORTING, AuditLifecycleStatus.PACKAGING}:
        payload["core_seal_root"] = _digest("core")
        payload["sealed_at"] = NOW
        payload["publication_status"] = (
            AuditPublicationStatus.REPORTING
            if current is AuditLifecycleStatus.REPORTING
            else AuditPublicationStatus.PACKAGING
        )
    if current in {
        AuditLifecycleStatus.COMPLETED,
        AuditLifecycleStatus.COMPLETED_PARTIAL,
        AuditLifecycleStatus.FAILED,
        AuditLifecycleStatus.CANCELLED,
    }:
        payload["closure_status"] = {
            AuditLifecycleStatus.COMPLETED: AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE,
            AuditLifecycleStatus.COMPLETED_PARTIAL: AuditClosureStatus.PARTIAL_BUDGET,
            AuditLifecycleStatus.FAILED: AuditClosureStatus.FAILED,
            AuditLifecycleStatus.CANCELLED: AuditClosureStatus.CANCELLED,
        }[current]
        payload["publication_status"] = AuditPublicationStatus.PUBLISHED
        payload["core_seal_root"] = _digest("core")
        payload["sealed_at"] = NOW
        payload["initial_distribution_revision_id"] = "revision-1"
        payload["latest_distribution_revision_id"] = "revision-1"
        payload["publication_finished_at"] = NOW
    scan = AuditScan.model_validate(payload)
    assert scan.can_transition_to(target) is ((current, target) in _EXPECTED_AUDIT_EDGES)


_EXPECTED_CANDIDATE_EDGES = {
    (CandidateStatus.NEW, CandidateStatus.NORMALIZED),
    (CandidateStatus.NORMALIZED, CandidateStatus.VALIDATING),
    (CandidateStatus.VALIDATING, CandidateStatus.CONFIRMED),
    (CandidateStatus.VALIDATING, CandidateStatus.REJECTED),
    (CandidateStatus.VALIDATING, CandidateStatus.DEFERRED),
    (CandidateStatus.VALIDATING, CandidateStatus.MERGED),
}


@pytest.mark.parametrize(("current", "target"), product(CandidateStatus, repeat=2))
def test_candidate_transition_state_machine_is_an_exhaustive_allowlist(
    current: CandidateStatus,
    target: CandidateStatus,
) -> None:
    expected = (current, target) in _EXPECTED_CANDIDATE_EDGES
    assert candidate_can_transition_to(current, target) is expected
    if expected:
        assert validate_candidate_transition(current, target) is target
    else:
        with pytest.raises(InvalidStateTransitionError):
            validate_candidate_transition(current, target)
