from sqlalchemy import UniqueConstraint

from riftx.persistence.audit_preflight import AuditPreflightJobRecord
from riftx.persistence.audit_preflight_plan import AuditPreflightPlanRecord
from riftx.persistence.audit_static_effect import (
    AuditStaticEffectPlanRecord,
    SnapshotMountLeaseRecord,
    SnapshotMountPinRecord,
    SnapshotMountStopProofRecord,
)
from riftx.persistence.capability_records import (
    CapabilityCandidateRecord,
    CapabilityDependencyRecord,
    CapabilityEvaluationResultRecord,
    CapabilityEvidenceContractRecord,
    CapabilityPackInstallRecord,
    CapabilityPackLockRecord,
    CapabilityPackMemberRecord,
    CapabilityPackRecord,
    CapabilityPermissionRecord,
    CapabilityPromotionRunRecord,
    CapabilityRecord,
    CapabilityVersionRecord,
)
from riftx.persistence.capability_selection_records import (
    AgentCapabilityScopeRecord,
    AgentCapabilitySelectionRecord,
)
from riftx.persistence.orm import Base
from riftx.persistence.skill_selection_records import (
    AgentSkillScopeRecord,
    AgentSkillSelectionRecord,
)
from riftx.persistence.workflow_signals import WorkflowSignalIntentRecord

assert AuditPreflightJobRecord.__table__.metadata is Base.metadata
assert AuditPreflightPlanRecord.__table__.metadata is Base.metadata
assert AuditStaticEffectPlanRecord.__table__.metadata is Base.metadata
assert SnapshotMountLeaseRecord.__table__.metadata is Base.metadata
assert SnapshotMountPinRecord.__table__.metadata is Base.metadata
assert SnapshotMountStopProofRecord.__table__.metadata is Base.metadata
assert CapabilityRecord.__table__.metadata is Base.metadata
assert CapabilityVersionRecord.__table__.metadata is Base.metadata
assert CapabilityDependencyRecord.__table__.metadata is Base.metadata
assert CapabilityPermissionRecord.__table__.metadata is Base.metadata
assert CapabilityEvidenceContractRecord.__table__.metadata is Base.metadata
assert CapabilityCandidateRecord.__table__.metadata is Base.metadata
assert CapabilityPromotionRunRecord.__table__.metadata is Base.metadata
assert CapabilityEvaluationResultRecord.__table__.metadata is Base.metadata
assert CapabilityPackRecord.__table__.metadata is Base.metadata
assert CapabilityPackMemberRecord.__table__.metadata is Base.metadata
assert CapabilityPackInstallRecord.__table__.metadata is Base.metadata
assert CapabilityPackLockRecord.__table__.metadata is Base.metadata
assert AgentCapabilityScopeRecord.__table__.metadata is Base.metadata
assert AgentCapabilitySelectionRecord.__table__.metadata is Base.metadata
assert AgentSkillScopeRecord.__table__.metadata is Base.metadata
assert AgentSkillSelectionRecord.__table__.metadata is Base.metadata
assert WorkflowSignalIntentRecord.__table__.metadata is Base.metadata

EXPECTED_TABLES = {
    "agent_checkpoints",
    "agent_capability_scopes",
    "agent_capability_selections",
    "agent_cycles",
    "agent_sessions",
    "agent_skill_scopes",
    "agent_skill_selections",
    "agent_steps",
    "agent_messages",
    "alembic_version",
    "approvals",
    "approval_grants",
    "artifacts",
    "audit_contracts",
    "audit_client_requests",
    "audit_phase_runs",
    "audit_preflight_exit_receipts",
    "audit_preflight_job_requests",
    "audit_preflight_jobs",
    "audit_preflight_plans",
    "audit_preflight_results",
    "audit_preflight_stop_receipts",
    "audit_projects",
    "audit_scans",
    "audit_security_context_bindings",
    "audit_scope_units",
    "audit_start_intents",
    "audit_static_effect_plans",
    "audit_work_items",
    "browser_actions",
    "browser_observations",
    "browser_pages",
    "browser_sessions",
    "browser_takeover_summaries",
    "capabilities",
    "capability_candidates",
    "capability_dependencies",
    "capability_evaluation_results",
    "capability_evidence_contracts",
    "capability_pack_installs",
    "capability_pack_locks",
    "capability_pack_members",
    "capability_packs",
    "capability_permissions",
    "capability_promotion_runs",
    "capability_versions",
    "connector_submissions",
    "context_compilations",
    "evidence_ledger",
    "context_checkpoints",
    "engagements",
    "engagement_facts",
    "executions",
    "findings",
    "local_audit_jobs",
    "fact_relations",
    "memories",
    "nodes",
    "provider_states",
    "reports",
    "runtime_approval_requests",
    "runner_commands",
    "runner_command_ownerships",
    "runner_credentials",
    "runner_effect_bindings",
    "runner_stop_projections",
    "runner_stop_receipts",
    "run_events",
    "run_leases",
    "runs",
    "source_snapshots",
    "snapshot_references",
    "snapshot_mount_leases",
    "snapshot_mount_pins",
    "snapshot_mount_stop_proofs",
    "source_references",
    "target_http_requests",
    "task_attempts",
    "task_budgets",
    "task_dependencies",
    "task_evidence_requirements",
    "task_graphs",
    "tasks",
    "terminal_sessions",
    "tool_call_intents",
    "tool_calls",
    "tool_states",
    "user_input_requests",
    "web_document_chunks",
    "web_documents",
    "web_research_notes",
    "web_research_packets",
    "web_search_queries",
    "web_search_results",
    "working_memories",
    "workflow_signal_intents",
}


def test_metadata_contains_v2_business_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES - {"alembic_version"}


def test_capability_schema_separates_candidates_versions_and_locks() -> None:
    candidates = Base.metadata.tables["capability_candidates"]
    versions = Base.metadata.tables["capability_versions"]
    locks = Base.metadata.tables["capability_pack_locks"]

    assert "candidate_digest" in candidates.columns
    assert "manifest_digest" in versions.columns
    assert candidates.c.capability_id.foreign_keys == set()
    assert {
        foreign_key.target_fullname
        for foreign_key in candidates.c.promoted_version_id.foreign_keys
    } == {"capability_versions.id"}
    assert {
        foreign_key.target_fullname
        for foreign_key in locks.c.capability_version_id.foreign_keys
    } == {"capability_versions.id"}
    assert {index.name for index in locks.indexes} >= {
        "ix_capability_pack_locks_owner_active",
        "ix_capability_pack_locks_version_active",
    }


def test_agent_skill_schema_pins_session_package_snapshots() -> None:
    scopes = Base.metadata.tables["agent_skill_scopes"]
    selections = Base.metadata.tables["agent_skill_selections"]

    assert {
        foreign_key.target_fullname
        for foreign_key in scopes.c.session_id.foreign_keys
    } == {"agent_sessions.id"}
    assert {
        "version",
        "skill_digest",
        "source",
        "reason",
        "document_json",
        "reference_json",
        "active",
        "references_loaded",
    } <= set(selections.columns.keys())


def test_agent_capability_schema_unifies_selectable_kinds() -> None:
    scopes = Base.metadata.tables["agent_capability_scopes"]
    selections = Base.metadata.tables["agent_capability_selections"]

    assert list(scopes.primary_key.columns.keys()) == ["session_id", "kind"]
    assert list(selections.primary_key.columns.keys()) == [
        "session_id",
        "kind",
        "capability_id",
    ]
    assert {
        "version",
        "capability_digest",
        "source",
        "reason",
        "snapshot_json",
        "state_json",
        "active",
    } <= set(selections.columns.keys())


def test_audit_preflight_plan_table_separates_token_and_lifecycle_facts() -> None:
    plans = Base.metadata.tables["audit_preflight_plans"]

    assert {
        "id",
        "canonical_json",
        "plan_digest",
        "preflight_job_id",
        "operator_principal_id",
        "authorization_scope_digest",
        "security_context_id",
        "security_context_digest",
        "token_verifier_schema_version",
        "token_key_id",
        "token_nonce",
        "token_hash",
        "status",
        "state_version",
        "reserved_audit_id",
        "reserved_client_request_id",
        "consumed_audit_id",
        "consumed_start_request_id",
    } <= set(plans.columns.keys())
    assert "preflight_token" not in plans.columns
    assert "raw_token" not in plans.columns

    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in plans.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("preflight_job_id",) in unique_columns
    assert ("token_hash",) in unique_columns
    assert (
        "id",
        "plan_digest",
        "operator_principal_id",
        "authorization_scope_digest",
        "security_context_id",
        "security_context_digest",
        "reserved_audit_id",
    ) in unique_columns


def test_run_table_matches_design_contract() -> None:
    runs = Base.metadata.tables["runs"]

    assert set(runs.columns.keys()) == {
        "id",
        "engagement_id",
        "kind",
        "node_id",
        "objective",
        "success_criteria_json",
        "entry_points_json",
        "scope_json",
        "status",
        "approval_mode",
        "model_profile",
        "workspace_path",
        "temporal_workflow_id",
        "created_at",
        "started_at",
        "finished_at",
    }
    assert runs.c.kind.nullable is False
    assert runs.c.kind.default is None
    assert runs.c.kind.server_default is None
    assert {index.name for index in runs.indexes} >= {"ix_runs_kind"}
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in runs.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert checks["ck_runs_kind"] == "kind IN ('general', 'code_audit')"


def test_event_sequence_is_unique_per_run() -> None:
    constraints = Base.metadata.tables["run_events"].constraints
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("run_id", "sequence") in unique_columns


def test_public_approval_schema_persists_the_authoritative_decision_tuple() -> None:
    approvals = Base.metadata.tables["approvals"]

    assert approvals.c.decision.type.length == 32
    assert approvals.c.decision.nullable is True
    assert approvals.c.decision_feedback.nullable is True


def test_web_source_registry_preserves_canonical_document_contract() -> None:
    assert {
        "requested_url",
        "final_url",
        "canonical_url",
        "raw_artifact_id",
        "normalized_artifact_id",
        "content_hash",
        "extraction_status",
        "cache_expires_at",
    } <= set(Base.metadata.tables["web_documents"].columns.keys())
    assert {
        "document_id",
        "heading_path_json",
        "content",
        "start_offset",
        "end_offset",
    } <= set(Base.metadata.tables["web_document_chunks"].columns.keys())
    assert {
        "document_id",
        "url",
        "domain",
        "source_type",
        "content_hash",
    } <= set(Base.metadata.tables["source_references"].columns.keys())


def test_web_research_schema_separates_candidates_from_sources() -> None:
    assert {
        "query",
        "search_type",
        "provider",
        "options_json",
        "status",
    } <= set(Base.metadata.tables["web_search_queries"].columns.keys())
    assert "query_id" in Base.metadata.tables["web_search_results"].columns
    assert "document_id" not in Base.metadata.tables["web_search_results"].columns
    assert {
        "document_id",
        "source_id",
        "evidence_spans_json",
        "content_trust",
    } <= set(Base.metadata.tables["web_research_notes"].columns.keys())
    assert {
        "claims_json",
        "source_ids_json",
        "document_ids_json",
        "artifact_ids_json",
        "content_trust",
    } <= set(Base.metadata.tables["web_research_packets"].columns.keys())


def test_target_http_schema_preserves_execution_identity_and_artifacts() -> None:
    assert {
        "execution_key",
        "run_id",
        "session_id",
        "tool_call_id",
        "node_id",
        "request_json",
        "result_json",
        "request_artifact_id",
        "response_artifact_id",
    } <= set(Base.metadata.tables["target_http_requests"].columns.keys())


def test_artifact_schema_persists_access_owner_and_ingest_integrity_facts() -> None:
    artifacts = Base.metadata.tables["artifacts"]

    assert {
        "audit_id",
        "access_class",
        "content_trust",
        "storage_key",
        "ingest_provenance_json",
    } <= set(artifacts.columns.keys())
    for column_name in (
        "access_class",
        "content_trust",
        "storage_key",
        "ingest_provenance_json",
    ):
        assert artifacts.c[column_name].nullable is False
        assert artifacts.c[column_name].server_default is None
    assert artifacts.c.audit_id.nullable is True
    assert {
        foreign_key.target_fullname for foreign_key in artifacts.c.audit_id.foreign_keys
    } == {"audit_scans.id"}
    [execution_foreign_key] = artifacts.c.execution_id.foreign_keys
    assert execution_foreign_key.target_fullname == "executions.id"
    assert execution_foreign_key.ondelete == "RESTRICT"
    assert {index.name for index in artifacts.indexes} >= {
        "ix_artifacts_public_run_created_id",
        "ix_artifacts_audit_run_execution_created_id",
    }
    assert {
        constraint.name
        for constraint in artifacts.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    } >= {
        "ck_artifacts_access_class",
        "ck_artifacts_content_trust",
        "ck_artifacts_owner_access",
        "ck_artifacts_canonical_storage_key",
        "ck_artifacts_safe_storage_components",
        "ck_artifacts_safe_mime_type",
        "ck_artifacts_sha256",
        "ck_artifacts_nonnegative_size",
    }


def test_browser_schema_preserves_bounded_observations_and_ownership() -> None:
    assert {
        "run_id",
        "agent_session_id",
        "node_id",
        "mode",
        "status",
        "owner",
        "profile_path",
        "cdp_endpoint",
        "takeover_observation_version",
    } <= set(Base.metadata.tables["browser_sessions"].columns.keys())
    assert {
        "browser_session_id",
        "page_id",
        "visible_text_excerpt",
        "interactive_elements_json",
        "forms_json",
        "network_summary_json",
        "screenshot_artifact_id",
        "network_artifact_id",
        "observation_version",
    } <= set(Base.metadata.tables["browser_observations"].columns.keys())
    assert {
        "action_key",
        "observation_version",
        "result_observation_id",
        "download_artifact_id",
    } <= set(Base.metadata.tables["browser_actions"].columns.keys())


def test_connector_schema_keeps_raw_http_in_artifacts() -> None:
    assert {
        "run_id",
        "source",
        "capture_id",
        "fingerprint",
        "request_artifact_id",
        "response_artifact_id",
        "manifest_artifact_id",
        "summary_json",
    } <= set(Base.metadata.tables["connector_submissions"].columns.keys())
    assert "request_body" not in Base.metadata.tables["connector_submissions"].columns
    assert "response_body" not in Base.metadata.tables["connector_submissions"].columns


def test_context_compilation_table_records_manifest_and_actual_usage() -> None:
    assert set(Base.metadata.tables["context_compilations"].columns.keys()) == {
        "id",
        "run_id",
        "session_id",
        "agent_id",
        "model_profile",
        "purpose",
        "manifest_json",
        "estimated_tokens",
        "actual_input_tokens",
        "actual_output_tokens",
        "loaded_memory_ids_json",
        "checkpoint_id",
        "created_at",
    }


def test_working_memory_table_is_versioned_structured_state() -> None:
    assert set(Base.metadata.tables["working_memories"].columns.keys()) == {
        "id",
        "run_id",
        "version",
        "state_json",
        "created_at",
        "updated_at",
    }


def test_task_graph_schema_separates_topology_attempts_budgets_and_evidence() -> None:
    assert set(Base.metadata.tables["task_graphs"].columns.keys()) == {
        "run_id",
        "version",
        "created_at",
        "updated_at",
    }
    assert {
        "run_id",
        "parent_task_id",
        "sequence",
        "status",
        "input_scope_json",
        "expected_output_schema_json",
        "required_capability_ids_json",
        "workspace_owner",
        "session_owner_id",
        "stop_condition",
        "reopen_history_json",
        "version",
    } <= set(Base.metadata.tables["tasks"].columns.keys())
    assert set(Base.metadata.tables["task_dependencies"].primary_key.columns.keys()) == {
        "run_id",
        "task_id",
        "depends_on_task_id",
    }
    assert {
        foreign_key.target_fullname
        for foreign_key in Base.metadata.tables["task_attempts"].c.task_id.foreign_keys
    } == {"tasks.id", "task_attempts.task_id"}
    assert {
        constraint.name for constraint in Base.metadata.tables["task_attempts"].constraints
    } >= {
        "uq_task_attempts_owner_id",
        "uq_task_attempts_owner_sequence",
    }
    assert set(Base.metadata.tables["task_budgets"].primary_key.columns.keys()) == {
        "run_id",
        "task_id",
    }
    assert "evidence_refs_json" in Base.metadata.tables["task_evidence_requirements"].columns.keys()


def test_evidence_ledger_schema_preserves_identity_scope_and_replay_metadata() -> None:
    assert set(Base.metadata.tables["evidence_ledger"].columns.keys()) == {
        "id",
        "schema_version",
        "run_id",
        "session_id",
        "task_id",
        "kind",
        "source_uri",
        "digest",
        "ledger_digest",
        "creator_type",
        "created_by",
        "trust_class",
        "scope_json",
        "redaction_status",
        "redaction_policy_ref",
        "replay_json",
        "locator_json",
        "artifact_id",
        "created_at",
    }
    assert {
        foreign_key.target_fullname
        for foreign_key in Base.metadata.tables["evidence_ledger"].c.task_id.foreign_keys
    } == {"tasks.id"}


def test_long_term_memory_table_tracks_scope_sources_and_lifecycle() -> None:
    assert set(Base.metadata.tables["memories"].columns.keys()) == {
        "id",
        "memory_type",
        "scope_type",
        "scope_id",
        "title",
        "content",
        "summary",
        "retrieval_keywords_json",
        "confidence",
        "importance",
        "source_refs_json",
        "valid_from",
        "valid_until",
        "supersedes",
        "status",
        "pinned",
        "created_by",
        "created_at",
        "updated_at",
    }


def test_node_table_matches_runner_lifecycle_contract() -> None:
    assert set(Base.metadata.tables["nodes"].columns.keys()) == {
        "id",
        "name",
        "platform",
        "architecture",
        "runner_version",
        "status",
        "capabilities_json",
        "labels_json",
        "current_runner_instance_id",
        "current_runner_epoch",
        "last_seen_at",
        "created_at",
        "updated_at",
    }


def test_runner_control_tables_match_durable_channel_contract() -> None:
    credentials = Base.metadata.tables["runner_credentials"]
    assert set(credentials.columns.keys()) == {
        "runner_instance_id",
        "node_id",
        "runner_epoch",
        "token_hash",
        "token_prefix",
        "protocol_capabilities_json",
        "created_at",
        "rotated_at",
        "revoked_at",
    }

    commands = Base.metadata.tables["runner_commands"]
    assert set(commands.columns.keys()) == {
        "id",
        "node_id",
        "kind",
        "idempotency_key",
        "target_runner_instance_id",
        "target_runner_epoch",
        "payload_json",
        "status",
        "attempts",
        "lease_id",
        "lease_expires_at",
        "result_json",
        "error",
        "state_version",
        "created_at",
        "updated_at",
        "completed_at",
    }
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in commands.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("node_id", "idempotency_key") in unique_columns

    credential_unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in credentials.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("node_id", "runner_epoch") in credential_unique_columns
    assert ("node_id", "token_hash") in credential_unique_columns
    assert tuple(column.name for column in credentials.primary_key.columns) == (
        "runner_instance_id",
    )

    command_indexes = {
        index.name: tuple(column.name for column in index.columns) for index in commands.indexes
    }
    assert command_indexes["ix_runner_commands_target_poll"] == (
        "node_id",
        "target_runner_instance_id",
        "target_runner_epoch",
        "status",
        "created_at",
    )


def test_execution_schema_tracks_owner_containment_and_stop_proof() -> None:
    assert {
        "owner_runner_instance_id",
        "owner_runner_epoch",
        "containment_id",
        "physical_stop_confirmed_at",
    } <= set(Base.metadata.tables["executions"].columns.keys())


def test_action_read_schema_has_durable_ordering_and_wide_intent_references() -> None:
    intents = Base.metadata.tables["tool_call_intents"]
    executions = Base.metadata.tables["executions"]
    runtime_approvals = Base.metadata.tables["runtime_approval_requests"]
    target_http = Base.metadata.tables["target_http_requests"]

    assert intents.c.id.type.length == 128
    assert runtime_approvals.c.tool_call_intent_id.type.length == 128
    assert executions.c.tool_call_id.type.length == 128
    assert target_http.c.tool_call_id.type.length == 128
    assert Base.metadata.tables["approvals"].c.tool_call_id.type.length == 64

    assert "created_at" in executions.c
    assert intents.c.updated_at.nullable is False
    assert executions.c.updated_at.nullable is False
    assert intents.c.updated_at.server_default is None
    assert executions.c.updated_at.server_default is None
    intent_indexes = {
        index.name: tuple(column.name for column in index.columns) for index in intents.indexes
    }
    execution_indexes = {
        index.name: tuple(column.name for column in index.columns) for index in executions.indexes
    }
    assert intent_indexes["ix_tool_call_intents_run_created_id"] == (
        "run_id",
        "created_at",
        "id",
    )
    assert execution_indexes["ix_executions_run_tool_created_id"] == (
        "run_id",
        "tool_call_id",
        "created_at",
        "id",
    )
