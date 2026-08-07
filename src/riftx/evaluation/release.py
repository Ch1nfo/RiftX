"""Executable release qualification gates for the current RiftX system."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from riftx.domain.base import DomainModel, utc_now


class ReleaseGate(StrEnum):
    CORE_PATH_EXCLUDES_DOCKER = "core_path_excludes_docker"
    CODE_AUDIT_INDEPENDENCE_BOUNDARY = "code_audit_independence_boundary"
    RUN_KIND_EFFECT_ISOLATION = "run_kind_effect_isolation"
    MODEL_CALLS_USE_CONTEXT_COMPILER = "model_calls_use_context_compiler"
    TOOL_CALL_PERSISTED_BEFORE_EXECUTION = "tool_call_persisted_before_execution"
    EXECUTION_HAS_IDEMPOTENCY_KEY = "execution_has_idempotency_key"
    TEMPORAL_HISTORY_EXCLUDES_LARGE_OUTPUT = "temporal_history_excludes_large_output"
    WORKER_RESTART_RECOVERS_RUN = "worker_restart_recovers_run"
    PROVIDER_STATE_EXPIRY_RECOVERS = "provider_state_expiry_recovers"
    MODEL_SWITCH_CONTINUES_RUN = "model_switch_continues_run"
    PTY_SUPPORTS_USER_TAKEOVER = "pty_supports_user_takeover"
    SUBAGENT_USES_INDEPENDENT_CONTEXT = "subagent_uses_independent_context"
    MEMORY_HAS_SCOPE_AND_SOURCE = "memory_has_scope_and_source"
    WEB_CLAIM_HAS_SOURCE = "web_claim_has_source"
    TARGET_HTTP_USES_HOST_NETWORK = "target_http_uses_host_network"
    WEBUI_AND_CLI_SHARE_RUNTIME_STATE = "webui_and_cli_share_runtime_state"
    LONG_HORIZON_EVAL_PASSES = "long_horizon_eval_passes"
    RECOVERY_INJECTION_PASSES = "recovery_injection_passes"


class ReleaseGateEvidence(DomainModel):
    gate: ReleaseGate
    passed: bool
    test_selectors: list[str] = Field(min_length=1)
    detail: str = Field(min_length=1)


class ReleaseGateReport(DomainModel):
    ready: bool
    gates: dict[ReleaseGate, ReleaseGateEvidence]
    generated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_every_gate(self) -> ReleaseGateReport:
        if set(self.gates) != set(ReleaseGate):
            raise ValueError("release report must contain every declared gate")
        if self.ready != all(item.passed for item in self.gates.values()):
            raise ValueError("release readiness must match the individual gates")
        return self


class ReleaseGateEvaluator:
    def evaluate(self, evidence: list[ReleaseGateEvidence]) -> ReleaseGateReport:
        by_gate: dict[ReleaseGate, ReleaseGateEvidence] = {}
        for item in evidence:
            if item.gate in by_gate:
                raise ValueError(f"duplicate release gate evidence for {item.gate.value!r}")
            by_gate[item.gate] = item
        missing = set(ReleaseGate) - set(by_gate)
        if missing:
            raise ValueError(
                f"missing release gate evidence: {sorted(item.value for item in missing)!r}"
            )
        return ReleaseGateReport(
            ready=all(item.passed for item in by_gate.values()),
            gates=by_gate,
        )


def release_gate_manifest() -> dict[ReleaseGate, tuple[str, tuple[str, ...]]]:
    """Map every release claim to executable pytest evidence."""

    return {
        ReleaseGate.CORE_PATH_EXCLUDES_DOCKER: (
            (
                "The distribution declares no Docker runtime contract, and the host-native "
                "Onboard, Doctor, Control Plane, and Pentest admission path works with an "
                "empty executable PATH."
            ),
            (
                "tests/evaluation/test_docker_independence.py::test_distribution_declares_no_docker_runtime_contract",
                "tests/integration/api/test_onboarded_pentest.py::test_clean_xdg_onboard_degrades_optional_tools_and_admits_pentest",
            ),
        ),
        ReleaseGate.CODE_AUDIT_INDEPENDENCE_BOUNDARY: (
            (
                "Repository production inputs and the synthetic artifact scanner contract "
                "pass the independence boundary."
            ),
            (
                "tests/evaluation/test_independence_gate.py::test_repository_production_inputs_pass_independence_boundary",
                "tests/evaluation/test_independence_gate.py::test_clean_explicit_bundle_passes_boundary",
                "tests/evaluation/test_independence_gate.py::test_forbidden_dependency_identity_is_rejected",
                "tests/evaluation/test_independence_gate.py::test_combined_dependency_source_and_archive_canary_is_rejected",
                "tests/evaluation/test_independence_gate.py::test_artifact_scanner_fail_closed_qualification_contract",
                "tests/evaluation/test_independence_gate.py::test_invalid_repository_root_fails_closed",
                "tests/evaluation/test_independence_gate.py::test_existing_empty_repository_fails_closed",
                "tests/evaluation/test_independence_gate.py::test_sparse_repository_missing_fixed_marker_fails_closed",
                "tests/evaluation/test_independence_gate.py::test_required_component_input_deletion_fails_closed",
                "tests/evaluation/test_independence_gate.py::test_repository_marker_type_is_checked",
                "tests/evaluation/test_independence_gate.py::test_bounded_encoding_variants_are_rejected",
                "tests/evaluation/test_independence_gate.py::test_utf16_bom_bundle_content_is_rejected",
                "tests/evaluation/test_independence_gate.py::test_production_source_symlink_fails_closed",
                "tests/evaluation/test_independence_gate.py::test_dependency_manifest_tree_symlink_fails_closed",
                "tests/evaluation/test_independence_gate.py::test_dependency_walk_error_fails_closed",
                "tests/evaluation/test_independence_gate.py::test_production_walk_error_fails_closed",
                "tests/evaluation/test_independence_gate.py::test_artifact_walk_error_fails_closed",
                "tests/evaluation/test_independence_gate.py::test_fifo_artifact_is_rejected_before_read",
                "tests/evaluation/test_independence_gate.py::test_explicit_artifact_symlink_fails_closed",
                "tests/evaluation/test_independence_gate.py::test_supported_compressed_tar_scans_forbidden_members",
                "tests/evaluation/test_independence_gate.py::test_compressed_tar_scans_link_target_metadata",
                "tests/evaluation/test_independence_gate.py::test_compressed_tar_scans_pax_metadata_and_allows_safe_link",
                "tests/evaluation/test_independence_gate.py::test_unsupported_archive_compression_fails_closed",
                "tests/evaluation/test_independence_gate.py::test_tar_sidecar_signature_is_not_misclassified_and_content_is_scanned",
            ),
        ),
        ReleaseGate.RUN_KIND_EFFECT_ISOLATION: (
            (
                "RunKind effect routing, immutable Runner ownership, durable Workflow "
                "signal sources, and long-lived read authorization fail closed."
            ),
            (
                "tests/unit/application/test_run_kind_effect_policy.py::test_managed_service_callback_and_reconciler_inventory_is_registered",
                "tests/unit/application/test_runner_control_policy.py::test_code_audit_m1_enqueue_is_zero_before_node_or_credential_state",
                "tests/integration/api/test_run_kind_bridge.py::test_code_audit_direct_effect_routes_reject_before_child_service",
                "tests/integration/persistence/test_workflow_signals.py::test_repository_rejects_missing_child_sources_without_writing",
                "tests/unit/temporal/test_workflow_signal_transport.py::test_transport_rejects_foreign_child_source_before_router_call",
                "tests/unit/api/test_event_stream.py::test_stream_reauthorizes_before_every_batch_and_denial_reads_or_emits_nothing",
                "tests/integration/persistence/test_runner_control_repository.py::test_pending_stop_receipt_converges_after_control_plane_restart",
                "tests/integration/persistence/test_runner_control_repository.py::test_resource_stop_receipt_projects_authoritative_state_after_restart",
                "tests/integration/persistence/test_runner_ownership_migration.py::test_runner_safe_downgrade_reupgrades_to_head_and_reopens",
                "tests/integration/api/test_control_plane.py::test_general_workflow_controls_keep_the_persisted_id_after_prefix_drift",
            ),
        ),
        ReleaseGate.MODEL_CALLS_USE_CONTEXT_COMPILER: (
            "Agent cycles compile bounded context before invoking the configured model.",
            (
                "tests/integration/agent/test_cycle.py::test_agent_cycle_runs_configured_tool_and_persists_timeline",
            ),
        ),
        ReleaseGate.TOOL_CALL_PERSISTED_BEFORE_EXECUTION: (
            "Execution submission rejects calls without a durable ToolCallIntent.",
            (
                "tests/execution/test_service.py::test_submit_requires_persisted_tool_call_before_runner_launch",
            ),
        ),
        ReleaseGate.EXECUTION_HAS_IDEMPOTENCY_KEY: (
            "Repeated submission reuses one durable Execution key and launch.",
            (
                "tests/execution/test_service.py::test_same_key_and_running_resubmission_launch_only_once",
            ),
        ),
        ReleaseGate.TEMPORAL_HISTORY_EXCLUDES_LARGE_OUTPUT: (
            "Large Tool output spills to Artifacts and Temporal payloads remain identifier-only.",
            (
                "tests/context/test_tool_results.py::test_fifty_megabytes_is_spilled_without_entering_context",
                "tests/evaluation/test_temporal_long_horizon.py::test_temporal_history_stays_identifier_only_across_100_tools_and_restart",
            ),
        ),
        ReleaseGate.WORKER_RESTART_RECOVERS_RUN: (
            "A new Temporal Worker replays and continues the same Run.",
            (
                "tests/unit/temporal/test_workflow.py::test_tool_running_survives_worker_restart_and_replays",
            ),
        ),
        ReleaseGate.PROVIDER_STATE_EXPIRY_RECOVERS: (
            "Canonical checkpoint recovery tolerates stale provider-native state.",
            (
                "tests/context/test_compaction.py::test_compaction_preserves_resume_state_and_repairs_crash_retry",
            ),
        ),
        ReleaseGate.MODEL_SWITCH_CONTINUES_RUN: (
            "Model switching checkpoints and recompiles the existing Run.",
            (
                "tests/unit/temporal/test_workflow.py::test_model_switch_checkpoints_then_waits_for_original_user_input",
                "tests/context/test_compaction.py::test_compaction_preserves_resume_state_and_repairs_crash_retry",
            ),
        ),
        ReleaseGate.PTY_SUPPORTS_USER_TAKEOVER: (
            "PTY ownership, takeover I/O, resize, interrupt, and release are executable.",
            (
                "tests/integration/api/test_control_plane.py::test_terminal_websocket_takeover_io_resize_interrupt_and_release",
            ),
        ),
        ReleaseGate.SUBAGENT_USES_INDEPENDENT_CONTEXT: (
            "Subagents receive selected facts and an isolated delegation contract only.",
            (
                "tests/subagents/test_context.py::test_subagent_context_contains_only_selected_facts_and_delegation_contract",
            ),
        ),
        ReleaseGate.MEMORY_HAS_SCOPE_AND_SOURCE: (
            "Memory rejects missing provenance and enforces scope-aware retrieval.",
            (
                "tests/memory/test_store.py::test_memory_rejects_missing_sources_and_invalid_ttl",
                "tests/memory/test_store.py::test_scope_ttl_supersede_pin_and_keyword_retrieval",
            ),
        ),
        ReleaseGate.WEB_CLAIM_HAS_SOURCE: (
            "Research packets contain canonical Sources and evidence offsets.",
            (
                "tests/web/test_research_pipeline.py::test_multi_query_pipeline_returns_only_bounded_canonical_packet",
                "tests/web/test_research_pipeline.py::test_focused_extraction_preserves_chunk_evidence_offsets",
            ),
        ),
        ReleaseGate.TARGET_HTTP_USES_HOST_NETWORK: (
            "Scoped Target HTTP executes through the Runner boundary and saves Artifacts.",
            (
                "tests/target_http/test_service.py::test_service_requires_scope_and_ready_intent_then_saves_artifacts",
                "tests/integration/api/test_control_plane.py::test_remote_target_http_command_uploads_bounded_response_body",
            ),
        ),
        ReleaseGate.WEBUI_AND_CLI_SHARE_RUNTIME_STATE: (
            "WebUI and CLI consume the same persisted control-plane endpoints.",
            (
                "tests/integration/api/test_control_plane.py::test_complete_agent_runner_sse_finding_report_lifecycle",
                "tests/unit/cli/test_client.py::test_api_client_uses_shared_run_endpoints",
            ),
        ),
        ReleaseGate.LONG_HORIZON_EVAL_PASSES: (
            "The complete QA-01 100-call long-horizon workload passes.",
            (
                "tests/evaluation/test_long_horizon_recovery.py::test_qa_01_long_horizon_and_recovery_gate",
            ),
        ),
        ReleaseGate.RECOVERY_INJECTION_PASSES: (
            "All nine mandatory recovery injection boundaries pass without duplication or loss.",
            (
                "tests/evaluation/test_long_horizon_recovery.py::test_qa_01_long_horizon_and_recovery_gate",
            ),
        ),
    }
