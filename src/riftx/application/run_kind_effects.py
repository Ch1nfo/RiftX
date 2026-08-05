"""Fail-closed RunKind effect policy and machine-readable ingress inventory.

The catalog in this module is deliberately independent from FastAPI.  API
policy validation compares :class:`OperationEffect` values with
``api.policy.RouteEffect`` values, while application services, workers and
reconcilers can consume the same catalog without introducing an application
to API dependency.

``audit_alternative`` is documentation and routing metadata only.  A denied
generic request is never rewritten to the named Audit operation.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from riftx.domain import LocalPrincipal, RunKind

_LOCAL_ADMINISTRATIVE_SCOPE_DOMAIN = b"riftx.local-administrative-scope/v1\0"


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_optional_text(value: object | None, name: str) -> None:
    if value is not None:
        _require_text(value, name)


def _require_digest(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a 64-character lower-hex digest")


class EffectOrigin(StrEnum):
    """Trust boundary or durable subsystem that originated an effect."""

    LOCAL_OPERATOR_API = "local_operator_api"
    ADMIN_API = "admin_api"
    RUNNER_API = "runner_api"
    WEBSOCKET = "websocket"
    APPLICATION_SERVICE = "application_service"
    TEMPORAL_WORKER = "temporal_worker"
    TEMPORAL_ACTIVITY = "temporal_activity"
    CONTROL_PLANE_RECONCILER = "control_plane_reconciler"
    WORKER_RECONCILER = "worker_reconciler"
    SAFETY_RECONCILER = "safety_reconciler"
    RUNNER_COMMAND = "runner_command"


class EffectOwnerKind(StrEnum):
    """Discriminant for the authoritative root that owns an effect."""

    GLOBAL = "global"
    RUN = "run"
    PREFLIGHT_JOB = "preflight_job"
    LEGACY_RUNNER_COMMAND = "legacy_runner_command"


class OwnershipClaim(StrEnum):
    """Typed facts a resolver must prove before a rule may be admitted."""

    ADMINISTRATIVE_SCOPE_DIGEST = "administrative_scope_digest"
    RUN_ID = "run_id"
    RUN_KIND = "run_kind"
    AUDIT_ID = "audit_id"
    PLAN_DIGEST = "plan_digest"
    EXECUTION_ID = "execution_id"
    EFFECT_EXECUTION_ID = "effect_execution_id"
    RESOURCE_KIND = "resource_kind"
    RESOURCE_ID = "resource_id"
    NODE_ID = "node_id"
    RUNNER_PRINCIPAL = "runner_principal"
    RUNNER_COMMAND_ID = "runner_command_id"
    PREFLIGHT_JOB_ID = "preflight_job_id"
    OPERATOR_PRINCIPAL_ID = "operator_principal_id"
    AUTHORIZATION_SCOPE_DIGEST = "authorization_scope_digest"
    REQUEST_DIGEST = "request_digest"
    CAPSULE_ID = "capsule_id"
    LEASE_IDENTITY = "lease_identity"
    QUARANTINE_STATE = "quarantine_state"


@dataclass(frozen=True, slots=True)
class GlobalEffectOwnership:
    """Global administrative owner; never inferred from nullable Run fields."""

    administrative_scope_digest: str
    node_id: str | None = None
    runner_principal: object | None = None
    owner_kind: EffectOwnerKind = field(default=EffectOwnerKind.GLOBAL, init=False)

    def __post_init__(self) -> None:
        _require_digest(self.administrative_scope_digest, "administrative_scope_digest")
        _require_optional_text(self.node_id, "node_id")


def global_effect_ownership_for_local_principal(
    principal: LocalPrincipal,
) -> GlobalEffectOwnership:
    """Bind global local-operator effects to one canonical authenticated scope."""

    canonical = json.dumps(
        {
            "capabilities": sorted(capability.value for capability in principal.capabilities),
            "namespace_id": principal.namespace_id,
            "principal_id": principal.id,
            "profile": principal.profile.value,
            "schema_version": "riftx.local-administrative-scope/v1",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return GlobalEffectOwnership(
        administrative_scope_digest=hashlib.sha256(
            _LOCAL_ADMINISTRATIVE_SCOPE_DOMAIN + canonical
        ).hexdigest()
    )


@dataclass(frozen=True, slots=True)
class RunEffectOwnership:
    """Resolved Run owner facts supplied by an authoritative bounded lookup."""

    run_id: str
    run_kind: RunKind | str
    audit_id: str | None = None
    plan_digest: str | None = None
    execution_id: str | None = None
    effect_execution_id: str | None = None
    resource_kind: str | None = None
    resource_id: str | None = None
    node_id: str | None = None
    runner_principal: object | None = None
    runner_command_id: str | None = None
    owner_kind: EffectOwnerKind = field(default=EffectOwnerKind.RUN, init=False)

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        for name in (
            "audit_id",
            "execution_id",
            "effect_execution_id",
            "resource_kind",
            "resource_id",
            "node_id",
            "runner_command_id",
        ):
            _require_optional_text(getattr(self, name), name)
        if self.plan_digest is not None:
            _require_digest(self.plan_digest, "plan_digest")
        if self.plan_digest is not None and self.audit_id is None:
            raise ValueError("plan_digest requires an Audit owner")
        try:
            resolved_kind = RunKind(self.run_kind)
        except (TypeError, ValueError):
            return
        if resolved_kind is RunKind.GENERAL and (
            self.audit_id is not None or self.plan_digest is not None
        ):
            raise ValueError("General Run ownership cannot carry Audit or plan identity")


@dataclass(frozen=True, slots=True)
class PreflightJobEffectOwnership:
    """Pre-Audit job owner; deliberately has no Run or Audit identity fields."""

    preflight_job_id: str
    operator_principal_id: str
    authorization_scope_digest: str
    request_digest: str
    node_id: str
    capsule_id: str | None = None
    lease_identity: str | None = None
    owner_kind: EffectOwnerKind = field(default=EffectOwnerKind.PREFLIGHT_JOB, init=False)

    def __post_init__(self) -> None:
        _require_text(self.preflight_job_id, "preflight_job_id")
        _require_text(self.operator_principal_id, "operator_principal_id")
        _require_digest(self.authorization_scope_digest, "authorization_scope_digest")
        _require_digest(self.request_digest, "request_digest")
        _require_text(self.node_id, "node_id")
        _require_optional_text(self.capsule_id, "capsule_id")
        _require_optional_text(self.lease_identity, "lease_identity")


@dataclass(frozen=True, slots=True)
class LegacyRunnerCommandEffectOwnership:
    """Narrow owner for quarantined pre-envelope stop acknowledgements."""

    node_id: str
    runner_principal: object
    runner_command_id: str
    lease_identity: str
    quarantine_state: str
    owner_kind: EffectOwnerKind = field(
        default=EffectOwnerKind.LEGACY_RUNNER_COMMAND,
        init=False,
    )

    def __post_init__(self) -> None:
        _require_text(self.node_id, "node_id")
        if self.runner_principal is None:
            raise ValueError("runner_principal must be present")
        _require_text(self.runner_command_id, "runner_command_id")
        _require_text(self.lease_identity, "lease_identity")
        if self.quarantine_state != "quarantined:legacy_ownership_missing":
            raise ValueError("legacy Runner owner requires ownership-missing quarantine")


type ResolvedEffectOwnership = (
    GlobalEffectOwnership
    | RunEffectOwnership
    | PreflightJobEffectOwnership
    | LegacyRunnerCommandEffectOwnership
)


class RunEffectFamily(StrEnum):
    """Stable operation families used by ownership envelopes and telemetry."""

    RUN_LIFECYCLE = "run_lifecycle"
    WORKFLOW_CONTROL = "workflow_control"
    APPROVAL = "approval"
    EXECUTION = "execution"
    ARTIFACT = "artifact"
    FINDING = "finding"
    REPORT = "report"
    MEMORY = "memory"
    TERMINAL = "terminal"
    BROWSER = "browser"
    TARGET_HTTP = "target_http"
    CONNECTOR = "connector"
    CONTEXT = "context"
    GRAPH = "graph"
    METRICS = "metrics"
    ACTION = "action"
    RUNNER_COMMAND = "runner_command"
    SAFETY_STOP = "safety_stop"
    ADMINISTRATION = "administration"


class OperationEffect(StrEnum):
    """Application-layer mirror of the public ``RouteEffect`` vocabulary."""

    READ_ONLY = "read_only"
    DURABLE_WRITE = "durable_write"
    WORKFLOW_CONTROL = "workflow_control"
    HOST_EXECUTION = "host_execution"
    HOST_CONTROL = "host_control"
    ADMINISTRATION = "administration"
    RUNNER_CALLBACK = "runner_callback"


class OwnershipResolverKind(StrEnum):
    """Authoritative bounded lookup used before an operation may take effect."""

    NONE = "none"
    REQUEST_RUN_KIND = "request_run_kind"
    RUN_QUERY = "run_query"
    RUN_ID = "run_id"
    AUDIT_CONTRACT = "audit_contract"
    AUDIT_QUERY = "audit_query"
    AUDIT_ID = "audit_id"
    EXECUTION_ID = "execution_id"
    APPROVAL_ID = "approval_id"
    ARTIFACT_ID = "artifact_id"
    FINDING_ID = "finding_id"
    REPORT_ID = "report_id"
    MEMORY_SCOPE = "memory_scope"
    TERMINAL_SESSION_ID = "terminal_session_id"
    BROWSER_SESSION_ID = "browser_session_id"
    TARGET_HTTP_ID = "target_http_id"
    CONNECTOR_RUN_ID = "connector_run_id"
    CONTEXT_ID = "context_id"
    TOOL_CALL_INTENT_ID = "tool_call_intent_id"
    CHILD_RUN_BINDING = "child_run_binding"
    NODE_PRINCIPAL = "node_principal"
    RUNNER_COMMAND_ENVELOPE = "runner_command_envelope"
    LEGACY_RUNNER_STOP_LEASE = "legacy_runner_stop_lease"
    EXECUTION_OWNERSHIP_ENVELOPE = "execution_ownership_envelope"
    AUDIT_OWNERSHIP_ENVELOPE = "audit_ownership_envelope"
    PREFLIGHT_JOB_OWNER_ENVELOPE = "preflight_job_owner_envelope"


class EffectMode(StrEnum):
    """Execution mode; modes are intentionally not privilege-equivalent."""

    READ_ONLY = "read_only"
    NORMAL = "normal"
    OWNERSHIP_CALLBACK = "ownership_callback"
    SAFETY_REDUCE_ONLY = "safety_reduce_only"
    STOP_PROOF = "stop_proof"
    RECONCILE = "reconcile"
    GLOBAL = "global"


class RunEffectOperation(StrEnum):
    """Stable operation IDs. API members intentionally equal FastAPI route names."""

    # Local operator and Audit API routes.
    ACT_BROWSER = "act_browser"
    API_NOT_FOUND = "api_not_found"
    APPEND_MESSAGE = "append_message"
    APPROVE = "approve"
    CANCEL_CONNECTOR_RUN = "cancel_connector_run"
    CANCEL_CURRENT_EXECUTION = "cancel_current_execution"
    CANCEL_EXECUTION = "cancel_execution"
    CANCEL_RUN = "cancel_run"
    CLOSE_BROWSER = "close_browser"
    CLOSE_TERMINAL = "close_terminal"
    COMPACT_RUN = "compact_run"
    CONNECTOR_EVENTS = "connector_events"
    CONNECTOR_WEBUI = "connector_webui"
    CREATE_AUDIT = "create_audit"
    CREATE_AUDIT_PREFLIGHT = "create_audit_preflight"
    ISSUE_AUDIT_PREFLIGHT_PLAN = "issue_audit_preflight_plan"
    CREATE_FINDING = "create_finding"
    CREATE_MEMORY = "create_memory"
    CREATE_RUN = "create_run"
    CREATE_TERMINAL = "create_terminal"
    DELETE_MEMORY = "delete_memory"
    DOWNLOAD_ARTIFACT = "download_artifact"
    DOWNLOAD_AUDIT_ARTIFACT = "download_audit_artifact"
    GENERATE_REPORTS = "generate_reports"
    GET_ARTIFACT = "get_artifact"
    GET_AUDIT = "get_audit"
    GET_LOCAL_AUDIT_FINDING = "get_local_audit_finding"
    GET_LOCAL_AUDIT_REPORT = "get_local_audit_report"
    GET_AUDIT_PREFLIGHT = "get_audit_preflight"
    GET_AUDIT_ARTIFACT = "get_audit_artifact"
    GET_BROWSER = "get_browser"
    GET_CONTEXT_COMPILATION = "get_context_compilation"
    GET_EXECUTION = "get_execution"
    GET_EXECUTION_OUTPUT = "get_execution_output"
    GET_FINDING = "get_finding"
    GET_MEMORY = "get_memory"
    GET_NODE = "get_node"
    GET_REPORT = "get_report"
    GET_RUN = "get_run"
    GET_RUN_ACTION = "get_run_action"
    GET_RUN_CONTEXT = "get_run_context"
    GET_RUN_GRAPH = "get_run_graph"
    GET_RUN_METRICS = "get_run_metrics"
    GET_SECURITY_PROFILE = "get_security_profile"
    GET_SESSION_CONTEXT = "get_session_context"
    GET_TARGET_HTTP_EXCHANGE = "get_target_http_exchange"
    GET_TERMINAL = "get_terminal"
    LIST_APPROVALS = "list_approvals"
    LIST_ARTIFACTS = "list_artifacts"
    LIST_AUDIT_ARTIFACTS = "list_audit_artifacts"
    LIST_AUDITS = "list_audits"
    LIST_LOCAL_AUDIT_FINDINGS = "list_local_audit_findings"
    LIST_CONNECTOR_RUNS = "list_connector_runs"
    LIST_EVENTS = "list_events"
    LIST_FINDINGS = "list_findings"
    LIST_MEMORIES = "list_memories"
    LIST_MODEL_PROFILES = "list_model_profiles"
    LIST_NODES = "list_nodes"
    LIST_REPORTS = "list_reports"
    LIST_RUN_ACTIONS = "list_run_actions"
    LIST_RUN_EXECUTIONS = "list_run_executions"
    LIST_RUNS = "list_runs"
    LIST_TARGET_HTTP_EXCHANGES = "list_target_http_exchanges"
    LIST_TOOLS = "list_tools"
    OBSERVE_BROWSER = "observe_browser"
    OPEN_BROWSER = "open_browser"
    PAUSE_AUDIT = "pause_audit"
    PAUSE_RUN = "pause_run"
    PIN_MEMORY = "pin_memory"
    REGISTER_ARTIFACT = "register_artifact"
    REJECT = "reject"
    RELEASE_BROWSER = "release_browser"
    RESUME_AUDIT = "resume_audit"
    RESUME_RUN = "resume_run"
    SEARCH_MEMORIES = "search_memories"
    STREAM_BROWSER = "stream_browser"
    STREAM_EVENTS = "stream_events"
    START_AUDIT = "start_audit"
    SUBMIT_HTTP_CAPTURE = "submit_http_capture"
    SWITCH_RUN_MODEL = "switch_run_model"
    TAKEOVER_BROWSER = "takeover_browser"
    TERMINAL_WEBSOCKET = "terminal_websocket"
    CANCEL_AUDIT = "cancel_audit"
    CANCEL_AUDIT_PREFLIGHT = "cancel_audit_preflight"
    UPDATE_FINDING = "update_finding"
    UPDATE_MEMORY = "update_memory"
    WAIT_EXECUTION = "wait_execution"

    # Admin API routes.
    DELETE_MODEL_PROFILE = "delete_model_profile"
    DISCONNECT_NODE = "disconnect_node"
    GET_MODEL_PROFILE = "get_model_profile"
    LIST_MODEL_PROFILES_FOR_ADMIN = "list_model_profiles_for_admin"
    LIST_TOOLS_FOR_ADMIN = "list_tools_for_admin"
    REFRESH_TOOLS = "refresh_tools"
    SET_DEFAULT_MODEL_PROFILE = "set_default_model_profile"
    UPDATE_TOOL = "update_tool"
    UPSERT_MODEL_PROFILE = "upsert_model_profile"

    # Runner API routes.
    FINISH_RUNNER_COMMAND = "finish_runner_command"
    FINISH_LEGACY_RUNNER_COMMAND = "finish_legacy_runner_command"
    FINISH_AUDIT_PREFLIGHT_JOB = "finish_audit_preflight_job"
    HEARTBEAT_NODE = "heartbeat_node"
    POLL_AUDIT_PREFLIGHT_JOB = "poll_audit_preflight_job"
    POLL_RUNNER_COMMAND = "poll_runner_command"
    REGISTER_NODE = "register_node"
    RENEW_AUDIT_PREFLIGHT_LEASE = "renew_audit_preflight_lease"
    RENEW_RUNNER_COMMAND_LEASE = "renew_runner_command_lease"
    REPORT_EXECUTION_OUTPUT = "report_execution_output"
    REPORT_EXECUTION_STATUS = "report_execution_status"
    REPORT_RUNNER_COMMAND_OUTPUT = "report_runner_command_output"
    START_AUDIT_PREFLIGHT_JOB = "start_audit_preflight_job"
    STOP_AUDIT_PREFLIGHT_JOB = "stop_audit_preflight_job"

    # Application service entrypoints.
    SERVICE_RUN_CREATE = "service.run.create"
    SERVICE_RUN_PAUSE = "service.run.pause"
    SERVICE_RUN_RESUME = "service.run.resume"
    SERVICE_RUN_CANCEL = "service.run.cancel"
    SERVICE_RUN_CANCEL_CURRENT_EXECUTION = "service.run.cancel_current_execution"
    SERVICE_RUN_COMPACT = "service.run.compact"
    SERVICE_RUN_SWITCH_MODEL = "service.run.switch_model"
    SERVICE_RUN_APPEND_MESSAGE = "service.run.append_message"
    SERVICE_RUN_CLEANUP = "service.run.cleanup"
    SERVICE_AUDIT_CREATE_DRAFT = "service.audit.create_draft"
    SERVICE_AUDIT_PREFLIGHT_CREATE = "service.audit_preflight.create"
    SERVICE_AUDIT_PREFLIGHT_GET = "service.audit_preflight.get"
    SERVICE_AUDIT_PREFLIGHT_CANCEL = "service.audit_preflight.cancel"
    SERVICE_AUDIT_PREFLIGHT_PLAN_ISSUE = "service.audit_preflight_plan.issue"
    SERVICE_AUDIT_PREFLIGHT_RUNNER_POLL = "service.audit_preflight_runner.poll"
    SERVICE_AUDIT_PREFLIGHT_RUNNER_RENEW = "service.audit_preflight_runner.renew"
    SERVICE_AUDIT_PREFLIGHT_RUNNER_START = "service.audit_preflight_runner.start"
    SERVICE_AUDIT_PREFLIGHT_RUNNER_FINISH = "service.audit_preflight_runner.finish"
    SERVICE_AUDIT_PREFLIGHT_RUNNER_STOP = "service.audit_preflight_runner.stop"
    SERVICE_AUDIT_PREFLIGHT_RECONCILE = "service.audit_preflight.reconcile"
    PERSIST_AUDIT_PREFLIGHT_MUTATION = "persistence.audit_preflight.mutation"
    PERSIST_AUDIT_PREFLIGHT_PLAN_MUTATION = "persistence.audit_preflight_plan.mutation"
    SERVICE_AUDIT_PAUSE = "service.audit.pause"
    SERVICE_AUDIT_RESUME = "service.audit.resume"
    SERVICE_AUDIT_CANCEL = "service.audit.cancel"
    SERVICE_AUDIT_RECONCILE = "service.audit.reconcile"
    PERSIST_AUDIT_CONTROL_TRANSITION = "persistence.audit_control.transition"
    PERSIST_AUDIT_CLEANUP_CONVERGENCE = "persistence.audit_control.cleanup_convergence"
    SERVICE_APPROVAL_RECORD = "service.approval.record"
    SERVICE_RUNTIME_APPROVAL_RECORD = "service.runtime_approval.record"
    SERVICE_APPROVAL_APPROVE = "service.approval.approve"
    SERVICE_APPROVAL_REJECT = "service.approval.reject"
    SERVICE_EXECUTION_CANCEL = "service.execution.cancel"
    SERVICE_EXECUTION_SUBMIT = "service.execution.submit"
    SERVICE_EXECUTION_MUTATION = "service.execution.mutation"
    SERVICE_DEFERRED_EXECUTION_PREPARE = "service.deferred_execution.prepare"
    SERVICE_DEFERRED_EXECUTION_DISPATCH = "service.deferred_execution.dispatch"
    SERVICE_DEFERRED_EXECUTION_MUTATION = "service.deferred_execution.mutation"
    SERVICE_DEFERRED_EXECUTION_APPROVE = "service.deferred_execution.approve"
    SERVICE_DEFERRED_EXECUTION_REJECT = "service.deferred_execution.reject"
    SERVICE_ARTIFACT_REGISTER = "service.artifact.register"
    SERVICE_ARTIFACT_REGISTER_CONTENT = "service.artifact.register_content"
    SERVICE_FINDING_CREATE = "service.finding.create"
    SERVICE_FINDING_UPDATE = "service.finding.update"
    SERVICE_REPORT_GENERATE = "service.report.generate"
    SERVICE_MEMORY_CREATE = "service.memory.create"
    SERVICE_MEMORY_UPDATE = "service.memory.update"
    SERVICE_MEMORY_DELETE = "service.memory.delete"
    SERVICE_MEMORY_PIN = "service.memory.pin"
    SERVICE_TERMINAL_CREATE = "service.terminal.create"
    SERVICE_TERMINAL_WRITE = "service.terminal.write"
    SERVICE_TERMINAL_RESIZE = "service.terminal.resize"
    SERVICE_TERMINAL_INTERRUPT = "service.terminal.interrupt"
    SERVICE_TERMINAL_TAKEOVER = "service.terminal.takeover"
    SERVICE_TERMINAL_RELEASE = "service.terminal.release"
    SERVICE_TERMINAL_CLOSE = "service.terminal.close"
    SERVICE_BROWSER_OPEN = "service.browser.open"
    SERVICE_BROWSER_OBSERVE = "service.browser.observe"
    SERVICE_BROWSER_ACT = "service.browser.act"
    SERVICE_BROWSER_TAKEOVER = "service.browser.takeover"
    SERVICE_BROWSER_RELEASE = "service.browser.release"
    SERVICE_BROWSER_CLOSE = "service.browser.close"
    SERVICE_BROWSER_STOP_RUN = "service.browser.stop_run"
    SERVICE_TARGET_HTTP_EXECUTE = "service.target_http.execute"
    SERVICE_TARGET_HTTP_STOP_RUN = "service.target_http.stop_run"
    SERVICE_CONNECTOR_INGEST = "service.connector.ingest"
    SERVICE_CONTEXT_CREATE = "service.context.create"
    SERVICE_CONTEXT_RECORD_USAGE = "service.context.record_usage"
    SERVICE_RUNNER_REGISTER = "service.runner.register"
    SERVICE_RUNNER_HEARTBEAT = "service.runner.heartbeat"
    SERVICE_RUNNER_ENQUEUE = "service.runner.enqueue"
    SERVICE_RUNNER_POLL = "service.runner.poll"
    SERVICE_RUNNER_FINISH = "service.runner.finish"
    SERVICE_RUNNER_RENEW_LEASE = "service.runner.renew_lease"
    SERVICE_RUNNER_COMMAND_OUTPUT = "service.runner.command_output"
    SERVICE_RUNNER_EXECUTION_STATUS = "service.runner.execution_status"
    SERVICE_RUNNER_EXECUTION_OUTPUT = "service.runner.execution_output"
    SERVICE_RUNNER_STOP_ACK = "service.runner.stop_ack"
    SERVICE_RUNNER_LEGACY_STOP_ACK = "service.runner.legacy_stop_ack"
    SERVICE_RUNNER_RECONCILE_STOP_RECEIPTS = "service.runner.reconcile_stop_receipts"
    SERVICE_RUNNER_RECONCILE_QUARANTINE = "service.runner.reconcile_quarantine"
    SERVICE_NODE_REGISTER = "service.node.register"
    SERVICE_NODE_HEARTBEAT = "service.node.heartbeat"
    SERVICE_NODE_DISCONNECT = "service.node.disconnect"
    SERVICE_NODE_REFRESH_LIVENESS = "service.node.refresh_liveness"
    SERVICE_MODEL_UPSERT = "service.model.upsert"
    SERVICE_MODEL_SET_DEFAULT = "service.model.set_default"
    SERVICE_MODEL_DELETE = "service.model.delete"
    SERVICE_TOOL_REFRESH = "service.tool.refresh"
    SERVICE_TOOL_UPDATE = "service.tool.update"
    SERVICE_WORKFLOW_SIGNAL_CREATE = "service.workflow_signal.create"
    RUNTIME_AGENT_CYCLE = "runtime.agent_cycle"

    # RunKind-aware Workflow protocol router entrypoints.
    WORKFLOW_START_RUN = "workflow.start_run"
    WORKFLOW_EXECUTION_COMPLETION = "workflow.execution_completion"

    # Worker, reconciler and runner-command entrypoints.
    TEMPORAL_EXECUTION_COMPLETION = "temporal.execution_completion"
    TEMPORAL_APPROVAL_DECISION = "temporal.approval_decision"
    ACTIVITY_PREPARE_CONVERSATION = "activity.prepare_conversation"
    ACTIVITY_PREPARE_RUN = "activity.prepare_run"
    ACTIVITY_AGENT_CYCLE = "activity.agent_cycle"
    ACTIVITY_COMPACT_CONTEXT = "activity.compact_context"
    ACTIVITY_SWITCH_MODEL = "activity.switch_model"
    ACTIVITY_GENERATE_REPORT = "activity.generate_report"
    ACTIVITY_CLEANUP_REPORT_FAILURE = "activity.cleanup_report_failure"
    ACTIVITY_CLEANUP_RUN = "activity.cleanup_run"
    EXECUTION_RECONCILE = "reconcile.execution"
    CONTROL_PLANE_CLEANUP_RECONCILE = "reconcile.control_plane_cleanup"
    WORKER_CLEANUP_RECONCILE = "reconcile.worker_cleanup"
    CONTROL_PLANE_RUNNER_RECONCILE = "reconcile.control_plane_runner"
    CONTROL_PLANE_AUDIT_PREFLIGHT_RECONCILE = "reconcile.control_plane_audit_preflight"
    WORKER_RUNNER_RECONCILE = "reconcile.worker_runner"
    WORKFLOW_SIGNAL_DISPATCH = "workflow_signal.dispatch"
    WORKFLOW_SIGNAL_RECONCILE = "workflow_signal.reconcile"
    WORKFLOW_SIGNAL_TRANSPORT_SEND = "workflow_signal.transport_send"
    WORKFLOW_SIGNAL_OUTCOME_PROBE = "workflow_signal.outcome_probe"
    CONTROL_PLANE_WORKFLOW_SIGNAL_RECONCILE = "reconcile.control_plane_workflow_signal"
    WORKER_WORKFLOW_SIGNAL_RECONCILE = "reconcile.worker_workflow_signal"
    SAFETY_STOP_RUN = "safety.stop_run"
    RUNNER_COMMAND_ENQUEUE = "runner_command.enqueue"
    RUNNER_COMMAND_REPLAY = "runner_command.replay"
    RUNNER_COMMAND_CLAIM = "runner_command.claim"
    RUNNER_COMMAND_RENEW = "runner_command.renew"
    RUNNER_COMMAND_OUTPUT = "runner_command.output"
    RUNNER_COMMAND_FINISH = "runner_command.finish"
    RUNNER_COMMAND_STOP_ACK = "runner_command.stop_ack"
    RUNNER_COMMAND_LEGACY_STOP_ACK = "runner_command.legacy_stop_ack"

    # Dedicated or future Audit product alternatives.  Merely naming these
    # operations never authorizes or rewrites a generic request.
    AUDIT_APPROVAL_DECISION = "audit.approval.decision"
    AUDIT_EXECUTION_CONTROL = "audit.execution.control"
    AUDIT_ARTIFACT_INGEST = "audit.artifact.ingest"
    CODE_FINDING_TRIAGE = "audit.code_finding.triage"
    AUDIT_REPORT_REBUILD = "audit.report.rebuild"
    AUDIT_WORKFLOW_CALLBACK = "audit.workflow.callback"


class AuditAlternativeDisposition(StrEnum):
    """What the product offers for an equivalent Code Audit intent."""

    SAME_OPERATION = "same_operation"
    SAFE_PROJECTION = "safe_projection"
    DEDICATED_OPERATION = "dedicated_operation"
    OWNERSHIP_ROUTED = "ownership_routed"
    UNSUPPORTED = "unsupported"
    NOT_RUN_SCOPED = "not_run_scoped"


@dataclass(frozen=True, slots=True)
class AuditAlternative:
    """Non-executable pointer to the Code Audit product surface."""

    disposition: AuditAlternativeDisposition
    operation: RunEffectOperation | None = None

    def __post_init__(self) -> None:
        has_operation = self.operation is not None
        requires_operation = self.disposition in {
            AuditAlternativeDisposition.DEDICATED_OPERATION,
            AuditAlternativeDisposition.OWNERSHIP_ROUTED,
        }
        if has_operation is not requires_operation:
            raise ValueError("Audit alternative operation pointer is inconsistent")


@dataclass(frozen=True, slots=True)
class RunKindEffectPolicy:
    """One exact operation/origin admission rule."""

    operation: RunEffectOperation
    origin: EffectOrigin
    family: RunEffectFamily
    owner_kind: EffectOwnerKind
    allowed_run_kinds: frozenset[RunKind]
    required_effect: OperationEffect
    ownership_resolver: OwnershipResolverKind
    required_claims: frozenset[OwnershipClaim]
    effect_mode: EffectMode
    audit_alternative: AuditAlternative

    def __post_init__(self) -> None:
        if not isinstance(self.owner_kind, EffectOwnerKind):
            raise TypeError("owner_kind must be an EffectOwnerKind")
        if not isinstance(self.allowed_run_kinds, frozenset):
            raise TypeError("allowed_run_kinds must be an immutable frozenset")
        if not isinstance(self.required_claims, frozenset):
            raise TypeError("required_claims must be an immutable frozenset")
        if not all(isinstance(claim, OwnershipClaim) for claim in self.required_claims):
            raise TypeError("required_claims must contain only OwnershipClaim values")
        baseline_claims = _OWNER_BASELINE_CLAIMS[self.owner_kind]
        resolver_claims = _RESOLVER_REQUIRED_CLAIMS[self.ownership_resolver]
        missing_claims = (baseline_claims | resolver_claims) - self.required_claims
        if missing_claims:
            raise ValueError(
                "effect policy is missing ownership claims: "
                f"{sorted(claim.value for claim in missing_claims)}"
            )
        if self.owner_kind is EffectOwnerKind.RUN:
            if not self.allowed_run_kinds:
                raise ValueError("run-owned operations must declare at least one RunKind")
            if self.effect_mode is EffectMode.GLOBAL:
                raise ValueError("run-owned operations cannot use global mode")
            if self.ownership_resolver in {
                OwnershipResolverKind.NONE,
                OwnershipResolverKind.RUN_QUERY,
                OwnershipResolverKind.AUDIT_CONTRACT,
                OwnershipResolverKind.AUDIT_QUERY,
                OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
            }:
                raise ValueError("run-owned operation uses a non-Run ownership resolver")
        else:
            if self.allowed_run_kinds:
                raise ValueError("non-Run operations cannot claim RunKind admission")
            if self.owner_kind is EffectOwnerKind.GLOBAL:
                if self.effect_mode is not EffectMode.GLOBAL:
                    raise ValueError("global-owned operations must use global mode")
                if self.ownership_resolver is OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE:
                    raise ValueError("global operations cannot use a Preflight owner resolver")
            elif self.owner_kind is EffectOwnerKind.PREFLIGHT_JOB:
                if (
                    self.ownership_resolver
                    is not OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE
                ):
                    raise ValueError("Preflight operations require the Preflight owner resolver")
                if self.effect_mode is EffectMode.GLOBAL:
                    raise ValueError("Preflight operations cannot use global mode")
            else:
                if self.ownership_resolver is not OwnershipResolverKind.LEGACY_RUNNER_STOP_LEASE:
                    raise ValueError(
                        "legacy Runner operations require the original stop lease resolver"
                    )
                if self.effect_mode is not EffectMode.STOP_PROOF:
                    raise ValueError("legacy Runner operations are stop-proof only")
        if self.effect_mode is EffectMode.READ_ONLY and (
            self.required_effect is not OperationEffect.READ_ONLY
        ):
            raise ValueError("read_only mode requires the read_only effect")
        if self.required_effect is OperationEffect.READ_ONLY and self.effect_mode not in {
            EffectMode.READ_ONLY,
            EffectMode.GLOBAL,
        }:
            raise ValueError("read_only effects cannot use a mutating mode")
        if self.effect_mode is EffectMode.OWNERSHIP_CALLBACK and self.ownership_resolver not in {
            OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE,
            OwnershipResolverKind.EXECUTION_OWNERSHIP_ENVELOPE,
            OwnershipResolverKind.AUDIT_OWNERSHIP_ENVELOPE,
            OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        }:
            raise ValueError("ownership callbacks require an immutable ownership envelope")
        if self.effect_mode is EffectMode.STOP_PROOF and self.ownership_resolver not in {
            OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE,
            OwnershipResolverKind.EXECUTION_OWNERSHIP_ENVELOPE,
            OwnershipResolverKind.AUDIT_OWNERSHIP_ENVELOPE,
            OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
            OwnershipResolverKind.LEGACY_RUNNER_STOP_LEASE,
        }:
            raise ValueError("stop proof requires an immutable ownership envelope")


class PolicyDenialReason(StrEnum):
    UNKNOWN_OPERATION = "unknown_operation"
    UNKNOWN_ORIGIN = "unknown_origin"
    UNREGISTERED_OPERATION_ORIGIN = "unregistered_operation_origin"
    UNKNOWN_EFFECT = "unknown_effect"
    EFFECT_MISMATCH = "effect_mismatch"
    UNKNOWN_MODE = "unknown_mode"
    MODE_MISMATCH = "mode_mismatch"
    UNKNOWN_OWNER_KIND = "unknown_owner_kind"
    OWNER_KIND_MISMATCH = "owner_kind_mismatch"
    OWNERSHIP_VARIANT_INVALID = "ownership_variant_invalid"
    OWNERSHIP_CLAIM_MISSING = "ownership_claim_missing"
    RUN_KIND_REQUIRED = "run_kind_required"
    RUN_KIND_NOT_APPLICABLE = "run_kind_not_applicable"
    UNKNOWN_RUN_KIND = "unknown_run_kind"
    RUN_KIND_UNSUPPORTED = "run_kind_unsupported"


class RunKindEffectPolicyDenied(PermissionError):
    """Safe fail-closed denial without reflecting untrusted lookup values."""

    def __init__(self, reason: PolicyDenialReason) -> None:
        super().__init__("RunKind effect policy denied the requested operation")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RouteEffectBinding:
    operation: RunEffectOperation
    origin: EffectOrigin


class EffectEntrypointSurface(StrEnum):
    SERVICE = "service"
    CALLBACK = "callback"
    RECONCILER = "reconciler"
    ACTIVITY = "activity"
    RUNNER_COMMAND = "runner_command"


@dataclass(frozen=True, slots=True)
class ManagedEffectEntrypoint:
    qualified_name: str
    operation: RunEffectOperation
    origin: EffectOrigin
    surface: EffectEntrypointSurface = EffectEntrypointSurface.SERVICE


@dataclass(frozen=True, slots=True)
class ManagedOutOfScopeMethod:
    method_name: str
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.method_name, "method_name")
        _require_text(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class ManagedEffectType:
    """One class whose public async methods must be classified exactly once."""

    qualified_name: str
    read_only_methods: frozenset[str] = frozenset()
    out_of_scope_methods: tuple[ManagedOutOfScopeMethod, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.qualified_name, "qualified_name")
        if not isinstance(self.read_only_methods, frozenset):
            raise TypeError("read_only_methods must be an immutable frozenset")


_ALL_RUN_KINDS = frozenset(RunKind)
_GENERAL_ONLY = frozenset({RunKind.GENERAL})
_AUDIT_ONLY = frozenset({RunKind.CODE_AUDIT})
_NO_RUN_KIND: frozenset[RunKind] = frozenset()

_OWNER_BASELINE_CLAIMS: Mapping[EffectOwnerKind, frozenset[OwnershipClaim]] = MappingProxyType(
    {
        EffectOwnerKind.GLOBAL: frozenset({OwnershipClaim.ADMINISTRATIVE_SCOPE_DIGEST}),
        EffectOwnerKind.RUN: frozenset({OwnershipClaim.RUN_ID, OwnershipClaim.RUN_KIND}),
        EffectOwnerKind.PREFLIGHT_JOB: frozenset(
            {
                OwnershipClaim.PREFLIGHT_JOB_ID,
                OwnershipClaim.OPERATOR_PRINCIPAL_ID,
                OwnershipClaim.AUTHORIZATION_SCOPE_DIGEST,
                OwnershipClaim.REQUEST_DIGEST,
                OwnershipClaim.NODE_ID,
            }
        ),
        EffectOwnerKind.LEGACY_RUNNER_COMMAND: frozenset(
            {
                OwnershipClaim.NODE_ID,
                OwnershipClaim.RUNNER_PRINCIPAL,
                OwnershipClaim.RUNNER_COMMAND_ID,
                OwnershipClaim.LEASE_IDENTITY,
                OwnershipClaim.QUARANTINE_STATE,
            }
        ),
    }
)

_RESOLVER_REQUIRED_CLAIMS: Mapping[OwnershipResolverKind, frozenset[OwnershipClaim]] = (
    MappingProxyType(
        {
            OwnershipResolverKind.NONE: frozenset(),
            OwnershipResolverKind.REQUEST_RUN_KIND: frozenset({OwnershipClaim.RUN_KIND}),
            OwnershipResolverKind.RUN_QUERY: frozenset(),
            OwnershipResolverKind.RUN_ID: frozenset({OwnershipClaim.RUN_ID}),
            OwnershipResolverKind.AUDIT_CONTRACT: frozenset(),
            OwnershipResolverKind.AUDIT_QUERY: frozenset(),
            OwnershipResolverKind.AUDIT_ID: frozenset(
                {OwnershipClaim.RUN_ID, OwnershipClaim.AUDIT_ID}
            ),
            OwnershipResolverKind.EXECUTION_ID: frozenset({OwnershipClaim.EXECUTION_ID}),
            OwnershipResolverKind.APPROVAL_ID: frozenset(
                {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
            ),
            OwnershipResolverKind.ARTIFACT_ID: frozenset(
                {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
            ),
            OwnershipResolverKind.FINDING_ID: frozenset(
                {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
            ),
            OwnershipResolverKind.REPORT_ID: frozenset(
                {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
            ),
            OwnershipResolverKind.MEMORY_SCOPE: frozenset(
                {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
            ),
            OwnershipResolverKind.TERMINAL_SESSION_ID: frozenset(
                {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
            ),
            OwnershipResolverKind.BROWSER_SESSION_ID: frozenset(
                {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
            ),
            OwnershipResolverKind.TARGET_HTTP_ID: frozenset(
                {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
            ),
            OwnershipResolverKind.CONNECTOR_RUN_ID: frozenset(
                {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
            ),
            OwnershipResolverKind.CONTEXT_ID: frozenset(
                {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
            ),
            OwnershipResolverKind.TOOL_CALL_INTENT_ID: frozenset(
                {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
            ),
            OwnershipResolverKind.CHILD_RUN_BINDING: frozenset(
                {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
            ),
            OwnershipResolverKind.NODE_PRINCIPAL: frozenset(
                {OwnershipClaim.NODE_ID, OwnershipClaim.RUNNER_PRINCIPAL}
            ),
            OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE: frozenset(
                {
                    OwnershipClaim.NODE_ID,
                    OwnershipClaim.RUNNER_PRINCIPAL,
                    OwnershipClaim.RUNNER_COMMAND_ID,
                    OwnershipClaim.RESOURCE_KIND,
                    OwnershipClaim.RESOURCE_ID,
                }
            ),
            OwnershipResolverKind.LEGACY_RUNNER_STOP_LEASE: frozenset(
                {
                    OwnershipClaim.NODE_ID,
                    OwnershipClaim.RUNNER_PRINCIPAL,
                    OwnershipClaim.RUNNER_COMMAND_ID,
                    OwnershipClaim.LEASE_IDENTITY,
                    OwnershipClaim.QUARANTINE_STATE,
                }
            ),
            OwnershipResolverKind.EXECUTION_OWNERSHIP_ENVELOPE: frozenset(
                {
                    OwnershipClaim.EXECUTION_ID,
                    OwnershipClaim.NODE_ID,
                    OwnershipClaim.RUNNER_PRINCIPAL,
                }
            ),
            OwnershipResolverKind.AUDIT_OWNERSHIP_ENVELOPE: frozenset(
                {OwnershipClaim.AUDIT_ID, OwnershipClaim.PLAN_DIGEST}
            ),
            OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE: frozenset(
                {
                    OwnershipClaim.PREFLIGHT_JOB_ID,
                    OwnershipClaim.OPERATOR_PRINCIPAL_ID,
                    OwnershipClaim.AUTHORIZATION_SCOPE_DIGEST,
                    OwnershipClaim.REQUEST_DIGEST,
                    OwnershipClaim.NODE_ID,
                }
            ),
        }
    )
)

_SAME = AuditAlternative(AuditAlternativeDisposition.SAME_OPERATION)
_SAFE_PROJECTION = AuditAlternative(AuditAlternativeDisposition.SAFE_PROJECTION)
_UNSUPPORTED = AuditAlternative(AuditAlternativeDisposition.UNSUPPORTED)
_NOT_RUN_SCOPED = AuditAlternative(AuditAlternativeDisposition.NOT_RUN_SCOPED)


def _dedicated(operation: RunEffectOperation) -> AuditAlternative:
    return AuditAlternative(AuditAlternativeDisposition.DEDICATED_OPERATION, operation)


def _ownership_routed(operation: RunEffectOperation) -> AuditAlternative:
    return AuditAlternative(AuditAlternativeDisposition.OWNERSHIP_ROUTED, operation)


def _rule(
    operation: RunEffectOperation,
    origin: EffectOrigin,
    family: RunEffectFamily,
    allowed_run_kinds: frozenset[RunKind],
    effect: OperationEffect,
    resolver: OwnershipResolverKind,
    mode: EffectMode,
    alternative: AuditAlternative,
    *,
    owner_kind: EffectOwnerKind = EffectOwnerKind.RUN,
    required_claims: frozenset[OwnershipClaim] | None = None,
) -> RunKindEffectPolicy:
    claims = (
        required_claims
        if required_claims is not None
        else _OWNER_BASELINE_CLAIMS[owner_kind] | _RESOLVER_REQUIRED_CLAIMS[resolver]
    )
    return RunKindEffectPolicy(
        operation=operation,
        origin=origin,
        family=family,
        owner_kind=owner_kind,
        allowed_run_kinds=allowed_run_kinds,
        required_effect=effect,
        ownership_resolver=resolver,
        required_claims=frozenset(claims),
        effect_mode=mode,
        audit_alternative=alternative,
    )


def _rules(
    operations: tuple[RunEffectOperation, ...],
    origin: EffectOrigin,
    family: RunEffectFamily,
    allowed_run_kinds: frozenset[RunKind],
    effect: OperationEffect,
    resolver: OwnershipResolverKind,
    mode: EffectMode,
    alternative: AuditAlternative,
    *,
    owner_kind: EffectOwnerKind = EffectOwnerKind.RUN,
    required_claims: frozenset[OwnershipClaim] | None = None,
) -> tuple[RunKindEffectPolicy, ...]:
    return tuple(
        _rule(
            operation,
            origin,
            family,
            allowed_run_kinds,
            effect,
            resolver,
            mode,
            alternative,
            owner_kind=owner_kind,
            required_claims=required_claims,
        )
        for operation in operations
    )


# Every API route is represented here, including reads.  This makes the M1
# Code Audit read decision an explicit allowlist instead of an inference from
# successful Audit-root authorization.
_API_RULES: tuple[RunKindEffectPolicy, ...] = (
    *_rules(
        (
            RunEffectOperation.API_NOT_FOUND,
            RunEffectOperation.GET_NODE,
            RunEffectOperation.GET_SECURITY_PROFILE,
            RunEffectOperation.LIST_MODEL_PROFILES,
            RunEffectOperation.LIST_NODES,
            RunEffectOperation.LIST_TOOLS,
        ),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.ADMINISTRATION,
        _NO_RUN_KIND,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.NONE,
        EffectMode.GLOBAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.LIST_RUNS,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_QUERY,
        EffectMode.GLOBAL,
        _SAFE_PROJECTION,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.GET_RUN,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.RUN_LIFECYCLE,
        _ALL_RUN_KINDS,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _SAFE_PROJECTION,
    ),
    *_rules(
        (RunEffectOperation.LIST_EVENTS, RunEffectOperation.STREAM_EVENTS),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.RUN_LIFECYCLE,
        _ALL_RUN_KINDS,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _SAFE_PROJECTION,
    ),
    *_rules(
        (
            RunEffectOperation.GET_EXECUTION,
            RunEffectOperation.GET_EXECUTION_OUTPUT,
            RunEffectOperation.WAIT_EXECUTION,
        ),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.EXECUTION,
        _ALL_RUN_KINDS,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.EXECUTION_ID,
        EffectMode.READ_ONLY,
        _SAFE_PROJECTION,
    ),
    _rule(
        RunEffectOperation.LIST_RUN_EXECUTIONS,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.EXECUTION,
        _ALL_RUN_KINDS,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _SAFE_PROJECTION,
    ),
    _rule(
        RunEffectOperation.LIST_ARTIFACTS,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.ARTIFACT,
        _ALL_RUN_KINDS,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _SAFE_PROJECTION,
    ),
    *_rules(
        (RunEffectOperation.GET_ARTIFACT, RunEffectOperation.DOWNLOAD_ARTIFACT),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.ARTIFACT,
        _ALL_RUN_KINDS,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.ARTIFACT_ID,
        EffectMode.READ_ONLY,
        _SAFE_PROJECTION,
    ),
    *_rules(
        (RunEffectOperation.LIST_AUDITS,),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.AUDIT_QUERY,
        EffectMode.GLOBAL,
        _SAME,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.GET_AUDIT,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.RUN_LIFECYCLE,
        _AUDIT_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.AUDIT_ID,
        EffectMode.READ_ONLY,
        _SAME,
    ),
    *_rules(
        (
            RunEffectOperation.LIST_LOCAL_AUDIT_FINDINGS,
            RunEffectOperation.GET_LOCAL_AUDIT_FINDING,
            RunEffectOperation.GET_LOCAL_AUDIT_REPORT,
        ),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.FINDING,
        _NO_RUN_KIND,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.NONE,
        EffectMode.GLOBAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.GET_AUDIT_PREFLIGHT,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.READ_ONLY,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    _rule(
        RunEffectOperation.LIST_AUDIT_ARTIFACTS,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.ARTIFACT,
        _AUDIT_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.AUDIT_ID,
        EffectMode.READ_ONLY,
        _SAME,
    ),
    *_rules(
        (
            RunEffectOperation.GET_AUDIT_ARTIFACT,
            RunEffectOperation.DOWNLOAD_AUDIT_ARTIFACT,
        ),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.ARTIFACT,
        _AUDIT_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.ARTIFACT_ID,
        EffectMode.READ_ONLY,
        _SAME,
    ),
    *_rules(
        (RunEffectOperation.LIST_RUN_ACTIONS,),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.ACTION,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _dedicated(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    _rule(
        RunEffectOperation.GET_RUN_ACTION,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.ACTION,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.CHILD_RUN_BINDING,
        EffectMode.READ_ONLY,
        _dedicated(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    _rule(
        RunEffectOperation.GET_RUN_GRAPH,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.GRAPH,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.GET_RUN_METRICS,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.METRICS,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _UNSUPPORTED,
    ),
    *_rules(
        (RunEffectOperation.LIST_TARGET_HTTP_EXCHANGES,),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.TARGET_HTTP,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.GET_TARGET_HTTP_EXCHANGE,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.TARGET_HTTP,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.TARGET_HTTP_ID,
        EffectMode.READ_ONLY,
        _UNSUPPORTED,
    ),
    *_rules(
        (RunEffectOperation.LIST_FINDINGS,),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.FINDING,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _dedicated(RunEffectOperation.CODE_FINDING_TRIAGE),
    ),
    _rule(
        RunEffectOperation.GET_FINDING,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.FINDING,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.FINDING_ID,
        EffectMode.READ_ONLY,
        _dedicated(RunEffectOperation.CODE_FINDING_TRIAGE),
    ),
    *_rules(
        (RunEffectOperation.LIST_REPORTS,),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.REPORT,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _dedicated(RunEffectOperation.AUDIT_REPORT_REBUILD),
    ),
    _rule(
        RunEffectOperation.GET_REPORT,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.REPORT,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.REPORT_ID,
        EffectMode.READ_ONLY,
        _dedicated(RunEffectOperation.AUDIT_REPORT_REBUILD),
    ),
    _rule(
        RunEffectOperation.LIST_APPROVALS,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.APPROVAL,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _dedicated(RunEffectOperation.AUDIT_APPROVAL_DECISION),
    ),
    *_rules(
        (
            RunEffectOperation.LIST_MEMORIES,
            RunEffectOperation.SEARCH_MEMORIES,
        ),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.MEMORY,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.MEMORY_SCOPE,
        EffectMode.READ_ONLY,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.GET_MEMORY,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.MEMORY,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.MEMORY_SCOPE,
        EffectMode.READ_ONLY,
        _UNSUPPORTED,
    ),
    *_rules(
        (
            RunEffectOperation.GET_SESSION_CONTEXT,
            RunEffectOperation.GET_CONTEXT_COMPILATION,
        ),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.CONTEXT,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.CONTEXT_ID,
        EffectMode.READ_ONLY,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.GET_RUN_CONTEXT,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.CONTEXT,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.GET_TERMINAL,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.TERMINAL,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.TERMINAL_SESSION_ID,
        EffectMode.READ_ONLY,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.GET_BROWSER,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.BROWSER,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _UNSUPPORTED,
    ),
    *_rules(
        (
            RunEffectOperation.LIST_CONNECTOR_RUNS,
            RunEffectOperation.CONNECTOR_EVENTS,
            RunEffectOperation.CONNECTOR_WEBUI,
        ),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.CONNECTOR,
        _GENERAL_ONLY,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _UNSUPPORTED,
    ),
    # Local API mutations and controls.
    _rule(
        RunEffectOperation.CREATE_RUN,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.NONE,
        EffectMode.GLOBAL,
        _dedicated(RunEffectOperation.CREATE_AUDIT),
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.CREATE_AUDIT,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.AUDIT_CONTRACT,
        EffectMode.GLOBAL,
        _SAME,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.CREATE_AUDIT_PREFLIGHT,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.NONE,
        EffectMode.GLOBAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.START_AUDIT,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.NONE,
        EffectMode.GLOBAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.ISSUE_AUDIT_PREFLIGHT_PLAN,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.NORMAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    _rule(
        RunEffectOperation.PAUSE_RUN,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.WORKFLOW_CONTROL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.PAUSE_AUDIT),
    ),
    _rule(
        RunEffectOperation.RESUME_RUN,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.WORKFLOW_CONTROL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.RESUME_AUDIT),
    ),
    _rule(
        RunEffectOperation.CANCEL_RUN,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.WORKFLOW_CONTROL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.CANCEL_AUDIT),
    ),
    _rule(
        RunEffectOperation.PAUSE_AUDIT,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.WORKFLOW_CONTROL,
        _AUDIT_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.AUDIT_ID,
        EffectMode.NORMAL,
        _SAME,
    ),
    _rule(
        RunEffectOperation.RESUME_AUDIT,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.WORKFLOW_CONTROL,
        _AUDIT_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.AUDIT_ID,
        EffectMode.NORMAL,
        _SAME,
    ),
    _rule(
        RunEffectOperation.CANCEL_AUDIT,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.WORKFLOW_CONTROL,
        _AUDIT_ONLY,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.AUDIT_ID,
        EffectMode.NORMAL,
        _SAME,
    ),
    _rule(
        RunEffectOperation.CANCEL_AUDIT_PREFLIGHT,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.NORMAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    *_rules(
        (
            RunEffectOperation.APPEND_MESSAGE,
            RunEffectOperation.COMPACT_RUN,
            RunEffectOperation.SWITCH_RUN_MODEL,
        ),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.WORKFLOW_CONTROL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.CANCEL_CURRENT_EXECUTION,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.EXECUTION,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_EXECUTION_CONTROL),
    ),
    _rule(
        RunEffectOperation.CANCEL_EXECUTION,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.EXECUTION,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.EXECUTION_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_EXECUTION_CONTROL),
    ),
    *_rules(
        (RunEffectOperation.APPROVE, RunEffectOperation.REJECT),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.APPROVAL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.APPROVAL_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_APPROVAL_DECISION),
    ),
    _rule(
        RunEffectOperation.CANCEL_CONNECTOR_RUN,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.CONNECTOR,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.CREATE_FINDING,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.FINDING,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.CODE_FINDING_TRIAGE),
    ),
    _rule(
        RunEffectOperation.UPDATE_FINDING,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.FINDING,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.FINDING_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.CODE_FINDING_TRIAGE),
    ),
    *_rules(
        (
            RunEffectOperation.CREATE_MEMORY,
            RunEffectOperation.UPDATE_MEMORY,
            RunEffectOperation.DELETE_MEMORY,
            RunEffectOperation.PIN_MEMORY,
        ),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.MEMORY,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.MEMORY_SCOPE,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.GENERATE_REPORTS,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.REPORT,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_REPORT_REBUILD),
    ),
    _rule(
        RunEffectOperation.REGISTER_ARTIFACT,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.ARTIFACT,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_ARTIFACT_INGEST),
    ),
    _rule(
        RunEffectOperation.SUBMIT_HTTP_CAPTURE,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.TARGET_HTTP,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.CREATE_TERMINAL,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.TERMINAL,
        _GENERAL_ONLY,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.OPEN_BROWSER,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.BROWSER,
        _GENERAL_ONLY,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.CLOSE_TERMINAL,
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.TERMINAL,
        _GENERAL_ONLY,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.TERMINAL_SESSION_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    *_rules(
        (
            RunEffectOperation.CLOSE_BROWSER,
            RunEffectOperation.ACT_BROWSER,
            RunEffectOperation.TAKEOVER_BROWSER,
            RunEffectOperation.RELEASE_BROWSER,
            RunEffectOperation.OBSERVE_BROWSER,
        ),
        EffectOrigin.LOCAL_OPERATOR_API,
        RunEffectFamily.BROWSER,
        _GENERAL_ONLY,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.BROWSER_SESSION_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.TERMINAL_WEBSOCKET,
        EffectOrigin.WEBSOCKET,
        RunEffectFamily.TERMINAL,
        _GENERAL_ONLY,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.TERMINAL_SESSION_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.STREAM_BROWSER,
        EffectOrigin.WEBSOCKET,
        RunEffectFamily.BROWSER,
        _GENERAL_ONLY,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.BROWSER_SESSION_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    *_rules(
        (
            RunEffectOperation.GET_MODEL_PROFILE,
            RunEffectOperation.LIST_MODEL_PROFILES_FOR_ADMIN,
            RunEffectOperation.LIST_TOOLS_FOR_ADMIN,
        ),
        EffectOrigin.ADMIN_API,
        RunEffectFamily.ADMINISTRATION,
        _NO_RUN_KIND,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.NONE,
        EffectMode.GLOBAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    *_rules(
        (
            RunEffectOperation.DELETE_MODEL_PROFILE,
            RunEffectOperation.REFRESH_TOOLS,
            RunEffectOperation.SET_DEFAULT_MODEL_PROFILE,
            RunEffectOperation.UPDATE_TOOL,
            RunEffectOperation.UPSERT_MODEL_PROFILE,
        ),
        EffectOrigin.ADMIN_API,
        RunEffectFamily.ADMINISTRATION,
        _NO_RUN_KIND,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.NONE,
        EffectMode.GLOBAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.DISCONNECT_NODE,
        EffectOrigin.ADMIN_API,
        RunEffectFamily.ADMINISTRATION,
        _NO_RUN_KIND,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.NODE_PRINCIPAL,
        EffectMode.GLOBAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    *_rules(
        (RunEffectOperation.REGISTER_NODE, RunEffectOperation.HEARTBEAT_NODE),
        EffectOrigin.RUNNER_API,
        RunEffectFamily.ADMINISTRATION,
        _NO_RUN_KIND,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.NODE_PRINCIPAL,
        EffectMode.GLOBAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    *_rules(
        (
            RunEffectOperation.POLL_RUNNER_COMMAND,
            RunEffectOperation.FINISH_RUNNER_COMMAND,
            RunEffectOperation.RENEW_RUNNER_COMMAND_LEASE,
            RunEffectOperation.REPORT_RUNNER_COMMAND_OUTPUT,
        ),
        EffectOrigin.RUNNER_API,
        RunEffectFamily.RUNNER_COMMAND,
        _ALL_RUN_KINDS,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE,
        EffectMode.OWNERSHIP_CALLBACK,
        _ownership_routed(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    *_rules(
        (
            RunEffectOperation.POLL_AUDIT_PREFLIGHT_JOB,
            RunEffectOperation.RENEW_AUDIT_PREFLIGHT_LEASE,
            RunEffectOperation.START_AUDIT_PREFLIGHT_JOB,
            RunEffectOperation.FINISH_AUDIT_PREFLIGHT_JOB,
        ),
        EffectOrigin.RUNNER_API,
        RunEffectFamily.RUNNER_COMMAND,
        _NO_RUN_KIND,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.OWNERSHIP_CALLBACK,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    _rule(
        RunEffectOperation.STOP_AUDIT_PREFLIGHT_JOB,
        EffectOrigin.RUNNER_API,
        RunEffectFamily.SAFETY_STOP,
        _NO_RUN_KIND,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.STOP_PROOF,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    _rule(
        RunEffectOperation.FINISH_LEGACY_RUNNER_COMMAND,
        EffectOrigin.RUNNER_API,
        RunEffectFamily.SAFETY_STOP,
        _NO_RUN_KIND,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.LEGACY_RUNNER_STOP_LEASE,
        EffectMode.STOP_PROOF,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.LEGACY_RUNNER_COMMAND,
    ),
    *_rules(
        (
            RunEffectOperation.REPORT_EXECUTION_STATUS,
            RunEffectOperation.REPORT_EXECUTION_OUTPUT,
        ),
        EffectOrigin.RUNNER_API,
        RunEffectFamily.EXECUTION,
        _ALL_RUN_KINDS,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.EXECUTION_OWNERSHIP_ENVELOPE,
        EffectMode.OWNERSHIP_CALLBACK,
        _ownership_routed(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
)


_SERVICE_RULES: tuple[RunKindEffectPolicy, ...] = (
    _rule(
        RunEffectOperation.SERVICE_RUN_CREATE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.NONE,
        EffectMode.GLOBAL,
        _dedicated(RunEffectOperation.SERVICE_AUDIT_CREATE_DRAFT),
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.SERVICE_AUDIT_CREATE_DRAFT,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.AUDIT_CONTRACT,
        EffectMode.GLOBAL,
        _SAME,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_CREATE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.NONE,
        EffectMode.GLOBAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_GET,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.READ_ONLY,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    _rule(
        RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_CANCEL,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.NORMAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    _rule(
        RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_PLAN_ISSUE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.NORMAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_POLL,
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_RENEW,
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_START,
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_FINISH,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.RUNNER_COMMAND,
        _NO_RUN_KIND,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.OWNERSHIP_CALLBACK,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    _rule(
        RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_STOP,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.SAFETY_STOP,
        _NO_RUN_KIND,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.STOP_PROOF,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    _rule(
        RunEffectOperation.PERSIST_AUDIT_PREFLIGHT_MUTATION,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.NORMAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    _rule(
        RunEffectOperation.PERSIST_AUDIT_PREFLIGHT_PLAN_MUTATION,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.RUN_LIFECYCLE,
        _NO_RUN_KIND,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.NORMAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    _rule(
        RunEffectOperation.SERVICE_RUN_PAUSE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.WORKFLOW_CONTROL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.SERVICE_AUDIT_PAUSE),
    ),
    _rule(
        RunEffectOperation.SERVICE_RUN_RESUME,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.WORKFLOW_CONTROL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.SERVICE_AUDIT_RESUME),
    ),
    _rule(
        RunEffectOperation.SERVICE_RUN_CANCEL,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.WORKFLOW_CONTROL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.SERVICE_AUDIT_CANCEL),
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_RUN_COMPACT,
            RunEffectOperation.SERVICE_RUN_SWITCH_MODEL,
            RunEffectOperation.SERVICE_RUN_APPEND_MESSAGE,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.WORKFLOW_CONTROL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.SERVICE_RUN_CANCEL_CURRENT_EXECUTION,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.EXECUTION,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_EXECUTION_CONTROL),
    ),
    _rule(
        RunEffectOperation.SERVICE_RUN_CLEANUP,
        EffectOrigin.SAFETY_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _GENERAL_ONLY,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.SAFETY_REDUCE_ONLY,
        _dedicated(RunEffectOperation.SERVICE_AUDIT_RECONCILE),
    ),
    _rule(
        RunEffectOperation.SERVICE_AUDIT_PAUSE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.WORKFLOW_CONTROL,
        _AUDIT_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.AUDIT_ID,
        EffectMode.NORMAL,
        _SAME,
    ),
    _rule(
        RunEffectOperation.SERVICE_AUDIT_RESUME,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.WORKFLOW_CONTROL,
        _AUDIT_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.AUDIT_ID,
        EffectMode.NORMAL,
        _SAME,
    ),
    _rule(
        RunEffectOperation.SERVICE_AUDIT_CANCEL,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.WORKFLOW_CONTROL,
        _AUDIT_ONLY,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.AUDIT_ID,
        EffectMode.NORMAL,
        _SAME,
    ),
    _rule(
        RunEffectOperation.SERVICE_AUDIT_RECONCILE,
        EffectOrigin.SAFETY_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _AUDIT_ONLY,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.AUDIT_ID,
        EffectMode.RECONCILE,
        _SAME,
    ),
    _rule(
        RunEffectOperation.PERSIST_AUDIT_CONTROL_TRANSITION,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.WORKFLOW_CONTROL,
        _AUDIT_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.AUDIT_ID,
        EffectMode.NORMAL,
        _SAME,
    ),
    _rule(
        RunEffectOperation.PERSIST_AUDIT_CLEANUP_CONVERGENCE,
        EffectOrigin.SAFETY_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _AUDIT_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.AUDIT_ID,
        EffectMode.RECONCILE,
        _SAME,
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_APPROVAL_RECORD,
            RunEffectOperation.SERVICE_RUNTIME_APPROVAL_RECORD,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.APPROVAL,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_APPROVAL_DECISION),
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_APPROVAL_APPROVE,
            RunEffectOperation.SERVICE_APPROVAL_REJECT,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.APPROVAL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.APPROVAL_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_APPROVAL_DECISION),
    ),
    _rule(
        RunEffectOperation.SERVICE_EXECUTION_CANCEL,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.EXECUTION,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.EXECUTION_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_EXECUTION_CONTROL),
    ),
    _rule(
        RunEffectOperation.SERVICE_EXECUTION_SUBMIT,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.EXECUTION,
        _GENERAL_ONLY,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_EXECUTION_CONTROL),
    ),
    _rule(
        RunEffectOperation.SERVICE_EXECUTION_MUTATION,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.EXECUTION,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.EXECUTION_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_EXECUTION_CONTROL),
    ),
    _rule(
        RunEffectOperation.SERVICE_DEFERRED_EXECUTION_PREPARE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.EXECUTION,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.SERVICE_DEFERRED_EXECUTION_DISPATCH,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.EXECUTION,
        _GENERAL_ONLY,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.TOOL_CALL_INTENT_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.SERVICE_DEFERRED_EXECUTION_MUTATION,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.EXECUTION,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.TOOL_CALL_INTENT_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.SERVICE_DEFERRED_EXECUTION_APPROVE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.APPROVAL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.TOOL_CALL_INTENT_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_APPROVAL_DECISION),
    ),
    _rule(
        RunEffectOperation.SERVICE_DEFERRED_EXECUTION_REJECT,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.APPROVAL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.TOOL_CALL_INTENT_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_APPROVAL_DECISION),
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_ARTIFACT_REGISTER,
            RunEffectOperation.SERVICE_ARTIFACT_REGISTER_CONTENT,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.ARTIFACT,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_ARTIFACT_INGEST),
    ),
    _rule(
        RunEffectOperation.SERVICE_FINDING_CREATE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.FINDING,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.CODE_FINDING_TRIAGE),
    ),
    _rule(
        RunEffectOperation.SERVICE_FINDING_UPDATE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.FINDING,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.FINDING_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.CODE_FINDING_TRIAGE),
    ),
    _rule(
        RunEffectOperation.SERVICE_REPORT_GENERATE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.REPORT,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_REPORT_REBUILD),
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_MEMORY_CREATE,
            RunEffectOperation.SERVICE_MEMORY_UPDATE,
            RunEffectOperation.SERVICE_MEMORY_DELETE,
            RunEffectOperation.SERVICE_MEMORY_PIN,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.MEMORY,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.MEMORY_SCOPE,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.SERVICE_TERMINAL_CREATE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.TERMINAL,
        _GENERAL_ONLY,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_TERMINAL_WRITE,
            RunEffectOperation.SERVICE_TERMINAL_RESIZE,
            RunEffectOperation.SERVICE_TERMINAL_INTERRUPT,
            RunEffectOperation.SERVICE_TERMINAL_TAKEOVER,
            RunEffectOperation.SERVICE_TERMINAL_RELEASE,
            RunEffectOperation.SERVICE_TERMINAL_CLOSE,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.TERMINAL,
        _GENERAL_ONLY,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.TERMINAL_SESSION_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.SERVICE_BROWSER_OPEN,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.BROWSER,
        _GENERAL_ONLY,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_BROWSER_OBSERVE,
            RunEffectOperation.SERVICE_BROWSER_ACT,
            RunEffectOperation.SERVICE_BROWSER_TAKEOVER,
            RunEffectOperation.SERVICE_BROWSER_RELEASE,
            RunEffectOperation.SERVICE_BROWSER_CLOSE,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.BROWSER,
        _GENERAL_ONLY,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.BROWSER_SESSION_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.SERVICE_BROWSER_STOP_RUN,
        EffectOrigin.SAFETY_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _ALL_RUN_KINDS,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.SAFETY_REDUCE_ONLY,
        _SAME,
    ),
    _rule(
        RunEffectOperation.SERVICE_TARGET_HTTP_EXECUTE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.TARGET_HTTP,
        _GENERAL_ONLY,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.SERVICE_TARGET_HTTP_STOP_RUN,
        EffectOrigin.SAFETY_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _ALL_RUN_KINDS,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.SAFETY_REDUCE_ONLY,
        _SAME,
    ),
    _rule(
        RunEffectOperation.SERVICE_CONNECTOR_INGEST,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.CONNECTOR,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_CONTEXT_CREATE,
            RunEffectOperation.SERVICE_CONTEXT_RECORD_USAGE,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.CONTEXT,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.CONTEXT_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.RUNTIME_AGENT_CYCLE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.EXECUTION,
        _GENERAL_ONLY,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_NODE_REGISTER,
            RunEffectOperation.SERVICE_NODE_HEARTBEAT,
            RunEffectOperation.SERVICE_NODE_DISCONNECT,
            RunEffectOperation.SERVICE_NODE_REFRESH_LIVENESS,
            RunEffectOperation.SERVICE_MODEL_UPSERT,
            RunEffectOperation.SERVICE_MODEL_SET_DEFAULT,
            RunEffectOperation.SERVICE_MODEL_DELETE,
            RunEffectOperation.SERVICE_TOOL_REFRESH,
            RunEffectOperation.SERVICE_TOOL_UPDATE,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.ADMINISTRATION,
        _NO_RUN_KIND,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.NONE,
        EffectMode.GLOBAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_RUNNER_REGISTER,
            RunEffectOperation.SERVICE_RUNNER_HEARTBEAT,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.ADMINISTRATION,
        _NO_RUN_KIND,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.NODE_PRINCIPAL,
        EffectMode.GLOBAL,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.GLOBAL,
    ),
    _rule(
        RunEffectOperation.SERVICE_RUNNER_ENQUEUE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.RUNNER_COMMAND,
        _ALL_RUN_KINDS,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE,
        EffectMode.NORMAL,
        _ownership_routed(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_RUNNER_POLL,
            RunEffectOperation.SERVICE_RUNNER_FINISH,
            RunEffectOperation.SERVICE_RUNNER_RENEW_LEASE,
            RunEffectOperation.SERVICE_RUNNER_COMMAND_OUTPUT,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.RUNNER_COMMAND,
        _ALL_RUN_KINDS,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE,
        EffectMode.OWNERSHIP_CALLBACK,
        _ownership_routed(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_RUNNER_EXECUTION_STATUS,
            RunEffectOperation.SERVICE_RUNNER_EXECUTION_OUTPUT,
        ),
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.EXECUTION,
        _ALL_RUN_KINDS,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.EXECUTION_OWNERSHIP_ENVELOPE,
        EffectMode.OWNERSHIP_CALLBACK,
        _ownership_routed(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    _rule(
        RunEffectOperation.SERVICE_RUNNER_STOP_ACK,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.SAFETY_STOP,
        _ALL_RUN_KINDS,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE,
        EffectMode.STOP_PROOF,
        _ownership_routed(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    _rule(
        RunEffectOperation.SERVICE_RUNNER_LEGACY_STOP_ACK,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.SAFETY_STOP,
        _NO_RUN_KIND,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.LEGACY_RUNNER_STOP_LEASE,
        EffectMode.STOP_PROOF,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.LEGACY_RUNNER_COMMAND,
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_RUNNER_RECONCILE_STOP_RECEIPTS,
            RunEffectOperation.SERVICE_RUNNER_RECONCILE_QUARANTINE,
        ),
        EffectOrigin.SAFETY_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _ALL_RUN_KINDS,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE,
        EffectMode.SAFETY_REDUCE_ONLY,
        _SAME,
    ),
    _rule(
        RunEffectOperation.SERVICE_WORKFLOW_SIGNAL_CREATE,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.WORKFLOW_CONTROL,
        _ALL_RUN_KINDS,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _SAME,
    ),
)


_INTERNAL_RULES: tuple[RunKindEffectPolicy, ...] = (
    _rule(
        RunEffectOperation.WORKFLOW_START_RUN,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.RUN_LIFECYCLE,
        _GENERAL_ONLY,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.WORKFLOW_EXECUTION_COMPLETION,
        EffectOrigin.APPLICATION_SERVICE,
        RunEffectFamily.WORKFLOW_CONTROL,
        _ALL_RUN_KINDS,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.EXECUTION_ID,
        EffectMode.NORMAL,
        _ownership_routed(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    _rule(
        RunEffectOperation.TEMPORAL_EXECUTION_COMPLETION,
        EffectOrigin.TEMPORAL_WORKER,
        RunEffectFamily.EXECUTION,
        _ALL_RUN_KINDS,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.EXECUTION_OWNERSHIP_ENVELOPE,
        EffectMode.OWNERSHIP_CALLBACK,
        _ownership_routed(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    _rule(
        RunEffectOperation.TEMPORAL_APPROVAL_DECISION,
        EffectOrigin.TEMPORAL_ACTIVITY,
        RunEffectFamily.APPROVAL,
        _ALL_RUN_KINDS,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.AUDIT_OWNERSHIP_ENVELOPE,
        EffectMode.OWNERSHIP_CALLBACK,
        _ownership_routed(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    *_rules(
        (
            RunEffectOperation.ACTIVITY_PREPARE_CONVERSATION,
            RunEffectOperation.ACTIVITY_PREPARE_RUN,
        ),
        EffectOrigin.TEMPORAL_ACTIVITY,
        RunEffectFamily.RUN_LIFECYCLE,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.ACTIVITY_AGENT_CYCLE,
        EffectOrigin.TEMPORAL_ACTIVITY,
        RunEffectFamily.EXECUTION,
        _GENERAL_ONLY,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.ACTIVITY_COMPACT_CONTEXT,
        EffectOrigin.TEMPORAL_ACTIVITY,
        RunEffectFamily.CONTEXT,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.ACTIVITY_SWITCH_MODEL,
        EffectOrigin.TEMPORAL_ACTIVITY,
        RunEffectFamily.WORKFLOW_CONTROL,
        _GENERAL_ONLY,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.ACTIVITY_GENERATE_REPORT,
        EffectOrigin.TEMPORAL_ACTIVITY,
        RunEffectFamily.REPORT,
        _GENERAL_ONLY,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.NORMAL,
        _dedicated(RunEffectOperation.AUDIT_REPORT_REBUILD),
    ),
    *_rules(
        (
            RunEffectOperation.ACTIVITY_CLEANUP_REPORT_FAILURE,
            RunEffectOperation.ACTIVITY_CLEANUP_RUN,
        ),
        EffectOrigin.TEMPORAL_ACTIVITY,
        RunEffectFamily.SAFETY_STOP,
        _GENERAL_ONLY,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.RECONCILE,
        _UNSUPPORTED,
    ),
    _rule(
        RunEffectOperation.EXECUTION_RECONCILE,
        EffectOrigin.SAFETY_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _ALL_RUN_KINDS,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.EXECUTION_ID,
        EffectMode.RECONCILE,
        _SAME,
    ),
    _rule(
        RunEffectOperation.CONTROL_PLANE_CLEANUP_RECONCILE,
        EffectOrigin.CONTROL_PLANE_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _ALL_RUN_KINDS,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.RECONCILE,
        _SAME,
    ),
    _rule(
        RunEffectOperation.WORKER_CLEANUP_RECONCILE,
        EffectOrigin.WORKER_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _ALL_RUN_KINDS,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.RECONCILE,
        _SAME,
    ),
    _rule(
        RunEffectOperation.CONTROL_PLANE_RUNNER_RECONCILE,
        EffectOrigin.CONTROL_PLANE_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _ALL_RUN_KINDS,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE,
        EffectMode.SAFETY_REDUCE_ONLY,
        _SAME,
    ),
    *_rules(
        (
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RECONCILE,
            RunEffectOperation.CONTROL_PLANE_AUDIT_PREFLIGHT_RECONCILE,
        ),
        EffectOrigin.CONTROL_PLANE_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _NO_RUN_KIND,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.RECONCILE,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    _rule(
        RunEffectOperation.PERSIST_AUDIT_PREFLIGHT_MUTATION,
        EffectOrigin.CONTROL_PLANE_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _NO_RUN_KIND,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        EffectMode.RECONCILE,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
    ),
    _rule(
        RunEffectOperation.WORKER_RUNNER_RECONCILE,
        EffectOrigin.WORKER_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _ALL_RUN_KINDS,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE,
        EffectMode.SAFETY_REDUCE_ONLY,
        _SAME,
    ),
    _rule(
        RunEffectOperation.WORKFLOW_SIGNAL_DISPATCH,
        EffectOrigin.SAFETY_RECONCILER,
        RunEffectFamily.WORKFLOW_CONTROL,
        _ALL_RUN_KINDS,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.RECONCILE,
        _SAME,
    ),
    _rule(
        RunEffectOperation.WORKFLOW_SIGNAL_RECONCILE,
        EffectOrigin.SAFETY_RECONCILER,
        RunEffectFamily.WORKFLOW_CONTROL,
        _ALL_RUN_KINDS,
        OperationEffect.DURABLE_WRITE,
        OwnershipResolverKind.RUN_ID,
        EffectMode.RECONCILE,
        _SAME,
    ),
    _rule(
        RunEffectOperation.WORKFLOW_SIGNAL_TRANSPORT_SEND,
        EffectOrigin.SAFETY_RECONCILER,
        RunEffectFamily.WORKFLOW_CONTROL,
        _ALL_RUN_KINDS,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.RECONCILE,
        _SAME,
    ),
    _rule(
        RunEffectOperation.WORKFLOW_SIGNAL_OUTCOME_PROBE,
        EffectOrigin.SAFETY_RECONCILER,
        RunEffectFamily.WORKFLOW_CONTROL,
        _ALL_RUN_KINDS,
        OperationEffect.READ_ONLY,
        OwnershipResolverKind.RUN_ID,
        EffectMode.READ_ONLY,
        _SAME,
    ),
    _rule(
        RunEffectOperation.CONTROL_PLANE_WORKFLOW_SIGNAL_RECONCILE,
        EffectOrigin.CONTROL_PLANE_RECONCILER,
        RunEffectFamily.WORKFLOW_CONTROL,
        _ALL_RUN_KINDS,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.RECONCILE,
        _SAME,
    ),
    _rule(
        RunEffectOperation.WORKER_WORKFLOW_SIGNAL_RECONCILE,
        EffectOrigin.WORKER_RECONCILER,
        RunEffectFamily.WORKFLOW_CONTROL,
        _ALL_RUN_KINDS,
        OperationEffect.WORKFLOW_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.RECONCILE,
        _SAME,
    ),
    _rule(
        RunEffectOperation.SAFETY_STOP_RUN,
        EffectOrigin.SAFETY_RECONCILER,
        RunEffectFamily.SAFETY_STOP,
        _ALL_RUN_KINDS,
        OperationEffect.HOST_CONTROL,
        OwnershipResolverKind.RUN_ID,
        EffectMode.SAFETY_REDUCE_ONLY,
        _SAME,
    ),
    *_rules(
        (
            RunEffectOperation.RUNNER_COMMAND_ENQUEUE,
            RunEffectOperation.RUNNER_COMMAND_REPLAY,
        ),
        EffectOrigin.RUNNER_COMMAND,
        RunEffectFamily.RUNNER_COMMAND,
        _ALL_RUN_KINDS,
        OperationEffect.HOST_EXECUTION,
        OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE,
        EffectMode.NORMAL,
        _ownership_routed(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    *_rules(
        (
            RunEffectOperation.RUNNER_COMMAND_CLAIM,
            RunEffectOperation.RUNNER_COMMAND_RENEW,
            RunEffectOperation.RUNNER_COMMAND_OUTPUT,
            RunEffectOperation.RUNNER_COMMAND_FINISH,
        ),
        EffectOrigin.RUNNER_COMMAND,
        RunEffectFamily.RUNNER_COMMAND,
        _ALL_RUN_KINDS,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE,
        EffectMode.OWNERSHIP_CALLBACK,
        _ownership_routed(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    _rule(
        RunEffectOperation.RUNNER_COMMAND_STOP_ACK,
        EffectOrigin.RUNNER_COMMAND,
        RunEffectFamily.SAFETY_STOP,
        _ALL_RUN_KINDS,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE,
        EffectMode.STOP_PROOF,
        _ownership_routed(RunEffectOperation.AUDIT_WORKFLOW_CALLBACK),
    ),
    _rule(
        RunEffectOperation.RUNNER_COMMAND_LEGACY_STOP_ACK,
        EffectOrigin.RUNNER_COMMAND,
        RunEffectFamily.SAFETY_STOP,
        _NO_RUN_KIND,
        OperationEffect.RUNNER_CALLBACK,
        OwnershipResolverKind.LEGACY_RUNNER_STOP_LEASE,
        EffectMode.STOP_PROOF,
        _NOT_RUN_SCOPED,
        owner_kind=EffectOwnerKind.LEGACY_RUNNER_COMMAND,
    ),
)


def _build_catalog(
    rules: tuple[RunKindEffectPolicy, ...],
) -> MappingProxyType[tuple[RunEffectOperation, EffectOrigin], RunKindEffectPolicy]:
    catalog: dict[tuple[RunEffectOperation, EffectOrigin], RunKindEffectPolicy] = {}
    duplicates: list[str] = []
    for policy in rules:
        key = (policy.operation, policy.origin)
        if key in catalog:
            duplicates.append(f"{policy.operation.value}@{policy.origin.value}")
        catalog[key] = policy
    if duplicates:
        raise RuntimeError(f"duplicate RunKind effect policies: {sorted(duplicates)}")
    return MappingProxyType(catalog)


RUN_KIND_EFFECT_POLICIES = _build_catalog((*_API_RULES, *_SERVICE_RULES, *_INTERNAL_RULES))


def _build_route_bindings() -> MappingProxyType[str, RouteEffectBinding]:
    return MappingProxyType(
        {
            policy.operation.value: RouteEffectBinding(policy.operation, policy.origin)
            for policy in _API_RULES
        }
    )


API_ROUTE_EFFECT_BINDINGS = _build_route_bindings()


def _entry(
    qualified_name: str,
    operation: RunEffectOperation,
    origin: EffectOrigin,
    *,
    surface: EffectEntrypointSurface | None = None,
) -> ManagedEffectEntrypoint:
    if surface is None:
        if origin is EffectOrigin.RUNNER_COMMAND:
            surface = EffectEntrypointSurface.RUNNER_COMMAND
        elif origin is EffectOrigin.TEMPORAL_ACTIVITY:
            surface = EffectEntrypointSurface.ACTIVITY
        elif origin in {
            EffectOrigin.CONTROL_PLANE_RECONCILER,
            EffectOrigin.WORKER_RECONCILER,
            EffectOrigin.SAFETY_RECONCILER,
        }:
            surface = EffectEntrypointSurface.RECONCILER
        elif origin is EffectOrigin.TEMPORAL_WORKER:
            surface = EffectEntrypointSurface.CALLBACK
        else:
            surface = EffectEntrypointSurface.SERVICE
    return ManagedEffectEntrypoint(qualified_name, operation, origin, surface)


# These are the explicitly managed non-route entrypoints.  CI resolves each
# qualified symbol and verifies that its operation/origin pair exists.  A new
# effectful service, callback or reconciler must be added here and to the
# catalog in the same change.
MANAGED_EFFECT_ENTRYPOINTS: tuple[ManagedEffectEntrypoint, ...] = (
    _entry(
        "riftx.application.services.runs:RunApplicationService.create_run",
        RunEffectOperation.SERVICE_RUN_CREATE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.runs:RunApplicationService.pause",
        RunEffectOperation.SERVICE_RUN_PAUSE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.runs:RunApplicationService.resume",
        RunEffectOperation.SERVICE_RUN_RESUME,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.runs:RunApplicationService.cancel",
        RunEffectOperation.SERVICE_RUN_CANCEL,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.runs:RunApplicationService.cancel_current_execution",
        RunEffectOperation.SERVICE_RUN_CANCEL_CURRENT_EXECUTION,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.runs:RunApplicationService.compact",
        RunEffectOperation.SERVICE_RUN_COMPACT,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.runs:RunApplicationService.switch_model",
        RunEffectOperation.SERVICE_RUN_SWITCH_MODEL,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.runs:RunApplicationService.append_user_message",
        RunEffectOperation.SERVICE_RUN_APPEND_MESSAGE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.runs:RunApplicationService.stop_resources_for_cleanup",
        RunEffectOperation.SERVICE_RUN_CLEANUP,
        EffectOrigin.SAFETY_RECONCILER,
    ),
    _entry(
        "riftx.application.services.workflow_signals:WorkflowSignalOutboxApplicationService.create",
        RunEffectOperation.SERVICE_WORKFLOW_SIGNAL_CREATE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.workflow_signals:WorkflowSignalDispatcher.dispatch_batch",
        RunEffectOperation.WORKFLOW_SIGNAL_DISPATCH,
        EffectOrigin.SAFETY_RECONCILER,
    ),
    _entry(
        "riftx.application.services.workflow_signals:WorkflowSignalReconciler.reconcile_batch",
        RunEffectOperation.WORKFLOW_SIGNAL_RECONCILE,
        EffectOrigin.SAFETY_RECONCILER,
    ),
    _entry(
        "riftx.persistence.audit_control_uow:SQLAlchemyAuditControlUnitOfWork.transition",
        RunEffectOperation.PERSIST_AUDIT_CONTROL_TRANSITION,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.persistence.audit_control_uow:SQLAlchemyAuditControlUnitOfWork.converge_cleanup",
        RunEffectOperation.PERSIST_AUDIT_CLEANUP_CONVERGENCE,
        EffectOrigin.SAFETY_RECONCILER,
    ),
    _entry(
        "riftx.application.services.audits:AuditApplicationService.create_draft",
        RunEffectOperation.SERVICE_AUDIT_CREATE_DRAFT,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.audits:AuditApplicationService.create_draft_authorized",
        RunEffectOperation.SERVICE_AUDIT_CREATE_DRAFT,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.audits:"
        "AuditApplicationService.create_draft_v2_authorized",
        RunEffectOperation.SERVICE_AUDIT_CREATE_DRAFT,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.audit_preflight:"
        "AuditPreflightApplicationService.create_authorized",
        RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_CREATE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.audit_preflight:"
        "AuditPreflightApplicationService.get_authorized",
        RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_GET,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.audit_preflight:"
        "AuditPreflightApplicationService.cancel_authorized",
        RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_CANCEL,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.audit_preflight_plan:"
        "AuditPreflightPlanApplicationService.issue_authorized",
        RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_PLAN_ISSUE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    *(
        _entry(
            f"riftx.application.services.audit_preflight_runner:"
            f"AuditPreflightRunnerService.{method}",
            operation,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method, operation in (
            ("poll", RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_POLL),
            ("renew_lease", RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_RENEW),
            ("start", RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_START),
            ("finish", RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_FINISH),
            ("stop", RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_STOP),
        )
    ),
    *(
        _entry(
            f"riftx.application.services.audit_preflight_runner:"
            f"AuditPreflightRunnerService.{method}",
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RECONCILE,
            EffectOrigin.CONTROL_PLANE_RECONCILER,
        )
        for method in (
            "reconcile_batch",
            "mark_expired_outcome_unknown",
            "expire_pending_never_created",
            "converge_finish_receipt",
            "converge_stop_receipt",
        )
    ),
    *(
        _entry(
            f"riftx.persistence.audit_preflight:SQLAlchemyAuditPreflightRepository.{method}",
            RunEffectOperation.PERSIST_AUDIT_PREFLIGHT_MUTATION,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method in ("create", "claim_next", "compare_and_set")
    ),
    *(
        _entry(
            f"riftx.persistence.audit_preflight_plan:"
            f"SQLAlchemyAuditPreflightPlanRepository.{method}",
            RunEffectOperation.PERSIST_AUDIT_PREFLIGHT_PLAN_MUTATION,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method in ("create", "compare_and_set")
    ),
    _entry(
        "riftx.persistence.audit_preflight:"
        "SQLAlchemyAuditPreflightRepository.compare_and_set_reconciliation",
        RunEffectOperation.PERSIST_AUDIT_PREFLIGHT_MUTATION,
        EffectOrigin.CONTROL_PLANE_RECONCILER,
    ),
    _entry(
        "riftx.application.services.audit_controls:AuditControlApplicationService.pause",
        RunEffectOperation.SERVICE_AUDIT_PAUSE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.audit_controls:AuditControlApplicationService.resume",
        RunEffectOperation.SERVICE_AUDIT_RESUME,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.audit_controls:AuditControlApplicationService.cancel",
        RunEffectOperation.SERVICE_AUDIT_CANCEL,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.audit_controls:AuditControlApplicationService.reconcile_run",
        RunEffectOperation.SERVICE_AUDIT_RECONCILE,
        EffectOrigin.SAFETY_RECONCILER,
    ),
    _entry(
        "riftx.application.services.approvals:ApprovalApplicationService.approve",
        RunEffectOperation.SERVICE_APPROVAL_APPROVE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.approvals:ApprovalApplicationService.reject",
        RunEffectOperation.SERVICE_APPROVAL_REJECT,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.approvals:ApprovalRequestRecorder.record",
        RunEffectOperation.SERVICE_APPROVAL_RECORD,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.approvals:RuntimeApprovalRequestRecorder.record",
        RunEffectOperation.SERVICE_RUNTIME_APPROVAL_RECORD,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.executions:ExecutionApplicationService.cancel",
        RunEffectOperation.SERVICE_EXECUTION_CANCEL,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.execution.service:ExecutionService.submit",
        RunEffectOperation.SERVICE_EXECUTION_SUBMIT,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.execution.service:ExecutionService.cancel",
        RunEffectOperation.SERVICE_EXECUTION_CANCEL,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    *(
        _entry(
            f"riftx.execution.service:ExecutionService.{method}",
            RunEffectOperation.SERVICE_EXECUTION_MUTATION,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method in ("sync_intent_execution", "wait")
    ),
    _entry(
        "riftx.execution.deferred:DeferredExecutionDispatcher.prepare",
        RunEffectOperation.SERVICE_DEFERRED_EXECUTION_PREPARE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    *(
        _entry(
            f"riftx.execution.deferred:DeferredExecutionDispatcher.{method}",
            RunEffectOperation.SERVICE_DEFERRED_EXECUTION_DISPATCH,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method in ("dispatch", "execute_intent", "execute_approved_intent")
    ),
    *(
        _entry(
            f"riftx.execution.deferred:DeferredExecutionDispatcher.{method}",
            RunEffectOperation.SERVICE_DEFERRED_EXECUTION_MUTATION,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method in (
            "claim_intent_execution",
            "rollback_intent_execution_claim",
            "sync_intent_execution",
            "settle_failed_intent_execution_start",
            "mark_intent_executing",
        )
    ),
    _entry(
        "riftx.execution.deferred:DeferredExecutionDispatcher.approve_intent",
        RunEffectOperation.SERVICE_DEFERRED_EXECUTION_APPROVE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.execution.deferred:DeferredExecutionDispatcher.reject_intent",
        RunEffectOperation.SERVICE_DEFERRED_EXECUTION_REJECT,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.artifacts:ArtifactApplicationService.register",
        RunEffectOperation.SERVICE_ARTIFACT_REGISTER,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.artifacts:ArtifactApplicationService.register_content",
        RunEffectOperation.SERVICE_ARTIFACT_REGISTER_CONTENT,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.findings:FindingApplicationService.create_finding",
        RunEffectOperation.SERVICE_FINDING_CREATE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.findings:FindingApplicationService.update_finding",
        RunEffectOperation.SERVICE_FINDING_UPDATE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.application.services.reports:ReportApplicationService.generate",
        RunEffectOperation.SERVICE_REPORT_GENERATE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.memory.service:MemoryService.create",
        RunEffectOperation.SERVICE_MEMORY_CREATE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.memory.service:MemoryService.update",
        RunEffectOperation.SERVICE_MEMORY_UPDATE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.memory.service:MemoryService.delete",
        RunEffectOperation.SERVICE_MEMORY_DELETE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.memory.service:MemoryService.pin",
        RunEffectOperation.SERVICE_MEMORY_PIN,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    *(
        _entry(
            f"riftx.application.services.terminals:TerminalApplicationService.{method}",
            operation,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method, operation in (
            ("create", RunEffectOperation.SERVICE_TERMINAL_CREATE),
            ("write", RunEffectOperation.SERVICE_TERMINAL_WRITE),
            ("resize", RunEffectOperation.SERVICE_TERMINAL_RESIZE),
            ("interrupt", RunEffectOperation.SERVICE_TERMINAL_INTERRUPT),
            ("take_over", RunEffectOperation.SERVICE_TERMINAL_TAKEOVER),
            ("release", RunEffectOperation.SERVICE_TERMINAL_RELEASE),
            ("close", RunEffectOperation.SERVICE_TERMINAL_CLOSE),
        )
    ),
    *(
        _entry(
            f"riftx.browser.service:BrowserApplicationService.{method}",
            operation,
            origin,
        )
        for method, operation, origin in (
            ("open", RunEffectOperation.SERVICE_BROWSER_OPEN, EffectOrigin.APPLICATION_SERVICE),
            (
                "observe",
                RunEffectOperation.SERVICE_BROWSER_OBSERVE,
                EffectOrigin.APPLICATION_SERVICE,
            ),
            ("act", RunEffectOperation.SERVICE_BROWSER_ACT, EffectOrigin.APPLICATION_SERVICE),
            (
                "takeover",
                RunEffectOperation.SERVICE_BROWSER_TAKEOVER,
                EffectOrigin.APPLICATION_SERVICE,
            ),
            (
                "release",
                RunEffectOperation.SERVICE_BROWSER_RELEASE,
                EffectOrigin.APPLICATION_SERVICE,
            ),
            ("close", RunEffectOperation.SERVICE_BROWSER_CLOSE, EffectOrigin.APPLICATION_SERVICE),
            (
                "stop_run",
                RunEffectOperation.SERVICE_BROWSER_STOP_RUN,
                EffectOrigin.SAFETY_RECONCILER,
            ),
        )
    ),
    _entry(
        "riftx.target_http.service:TargetHttpApplicationService.execute",
        RunEffectOperation.SERVICE_TARGET_HTTP_EXECUTE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.target_http.service:TargetHttpApplicationService.stop_run",
        RunEffectOperation.SERVICE_TARGET_HTTP_STOP_RUN,
        EffectOrigin.SAFETY_RECONCILER,
    ),
    _entry(
        "riftx.connectors.service:ConnectorApplicationService.ingest",
        RunEffectOperation.SERVICE_CONNECTOR_INGEST,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.context.inspector:ContextApplicationService.create",
        RunEffectOperation.SERVICE_CONTEXT_CREATE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    _entry(
        "riftx.context.inspector:ContextApplicationService.record_usage",
        RunEffectOperation.SERVICE_CONTEXT_RECORD_USAGE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    *(
        _entry(
            f"riftx.application.services.nodes:NodeApplicationService.{method}",
            operation,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method, operation in (
            ("register", RunEffectOperation.SERVICE_NODE_REGISTER),
            ("heartbeat", RunEffectOperation.SERVICE_NODE_HEARTBEAT),
            ("disconnect", RunEffectOperation.SERVICE_NODE_DISCONNECT),
            ("refresh_liveness", RunEffectOperation.SERVICE_NODE_REFRESH_LIVENESS),
        )
    ),
    *(
        _entry(
            f"riftx.application.services.models:ModelProfileApplicationService.{method}",
            operation,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method, operation in (
            ("upsert_profile", RunEffectOperation.SERVICE_MODEL_UPSERT),
            ("set_default", RunEffectOperation.SERVICE_MODEL_SET_DEFAULT),
            ("delete_profile", RunEffectOperation.SERVICE_MODEL_DELETE),
        )
    ),
    *(
        _entry(
            f"riftx.application.services.tools:ToolApplicationService.{method}",
            operation,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method, operation in (
            ("refresh_tools", RunEffectOperation.SERVICE_TOOL_REFRESH),
            ("update_tool", RunEffectOperation.SERVICE_TOOL_UPDATE),
        )
    ),
    *(
        _entry(
            f"riftx.application.services.runner_control:RunnerControlService.{method}",
            operation,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method, operation in (
            ("register", RunEffectOperation.SERVICE_RUNNER_REGISTER),
            ("heartbeat", RunEffectOperation.SERVICE_RUNNER_HEARTBEAT),
            ("enqueue", RunEffectOperation.SERVICE_RUNNER_ENQUEUE),
            ("poll", RunEffectOperation.SERVICE_RUNNER_POLL),
            ("finish_command", RunEffectOperation.SERVICE_RUNNER_FINISH),
            (
                "record_legacy_stop_ack",
                RunEffectOperation.SERVICE_RUNNER_LEGACY_STOP_ACK,
            ),
            ("renew_command_lease", RunEffectOperation.SERVICE_RUNNER_RENEW_LEASE),
            ("append_command_output", RunEffectOperation.SERVICE_RUNNER_COMMAND_OUTPUT),
            ("report_execution", RunEffectOperation.SERVICE_RUNNER_EXECUTION_STATUS),
            ("append_output", RunEffectOperation.SERVICE_RUNNER_EXECUTION_OUTPUT),
        )
    ),
    _entry(
        "riftx.application.services.runner_control:RunnerControlService.reconcile_stop_receipts",
        RunEffectOperation.SERVICE_RUNNER_RECONCILE_STOP_RECEIPTS,
        EffectOrigin.SAFETY_RECONCILER,
    ),
    _entry(
        "riftx.application.services.runner_control:"
        "RunnerControlService.reconcile_quarantined_commands",
        RunEffectOperation.SERVICE_RUNNER_RECONCILE_QUARANTINE,
        EffectOrigin.SAFETY_RECONCILER,
    ),
    *(
        _entry(
            f"riftx.application.workflow_router:RunWorkflowControlRouter.{method}",
            operation,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method, operation in (
            ("start_run", RunEffectOperation.WORKFLOW_START_RUN),
            ("pause", RunEffectOperation.SERVICE_RUN_PAUSE),
            ("resume", RunEffectOperation.SERVICE_RUN_RESUME),
            ("approve", RunEffectOperation.SERVICE_APPROVAL_APPROVE),
            ("reject", RunEffectOperation.SERVICE_APPROVAL_REJECT),
            (
                "execution_completed",
                RunEffectOperation.WORKFLOW_EXECUTION_COMPLETION,
            ),
            (
                "cancel_current_execution",
                RunEffectOperation.SERVICE_RUN_CANCEL_CURRENT_EXECUTION,
            ),
            ("cancel", RunEffectOperation.SERVICE_RUN_CANCEL),
            ("compact", RunEffectOperation.SERVICE_RUN_COMPACT),
            ("switch_model", RunEffectOperation.SERVICE_RUN_SWITCH_MODEL),
            ("append_user_message", RunEffectOperation.SERVICE_RUN_APPEND_MESSAGE),
            ("pause_audit", RunEffectOperation.SERVICE_AUDIT_PAUSE),
            ("resume_audit", RunEffectOperation.SERVICE_AUDIT_RESUME),
            ("cancel_audit", RunEffectOperation.SERVICE_AUDIT_CANCEL),
            (
                "execution_completed_owned",
                RunEffectOperation.WORKFLOW_EXECUTION_COMPLETION,
            ),
        )
    ),
    _entry(
        "riftx.temporal.workflow_signal_transport:RoutedWorkflowSignalTransport.send",
        RunEffectOperation.WORKFLOW_SIGNAL_TRANSPORT_SEND,
        EffectOrigin.SAFETY_RECONCILER,
    ),
    _entry(
        "riftx.temporal.workflow_signal_transport:TemporalWorkflowSignalOutcomeProbe.observe",
        RunEffectOperation.WORKFLOW_SIGNAL_OUTCOME_PROBE,
        EffectOrigin.SAFETY_RECONCILER,
    ),
    _entry(
        "riftx.runtime.coordinator:RuntimeCoordinator.run_cycle",
        RunEffectOperation.RUNTIME_AGENT_CYCLE,
        EffectOrigin.APPLICATION_SERVICE,
    ),
    *(
        _entry(
            f"riftx.temporal.activities:RiftXActivities.{method}",
            operation,
            EffectOrigin.TEMPORAL_ACTIVITY,
        )
        for method, operation in (
            (
                "prepare_conversation_activity",
                RunEffectOperation.ACTIVITY_PREPARE_CONVERSATION,
            ),
            ("prepare_run_activity", RunEffectOperation.ACTIVITY_PREPARE_RUN),
            ("agent_cycle_activity", RunEffectOperation.ACTIVITY_AGENT_CYCLE),
            ("run_agent_cycle_activity", RunEffectOperation.ACTIVITY_AGENT_CYCLE),
            ("compact_context_activity", RunEffectOperation.ACTIVITY_COMPACT_CONTEXT),
            ("switch_model_activity", RunEffectOperation.ACTIVITY_SWITCH_MODEL),
            ("generate_report_activity", RunEffectOperation.ACTIVITY_GENERATE_REPORT),
            (
                "cleanup_report_failure_activity",
                RunEffectOperation.ACTIVITY_CLEANUP_REPORT_FAILURE,
            ),
            ("cleanup_run_activity", RunEffectOperation.ACTIVITY_CLEANUP_RUN),
        )
    ),
    _entry(
        "riftx.temporal.runtime_activity:RuntimeCycleActivities.run_agent_cycle_activity",
        RunEffectOperation.ACTIVITY_AGENT_CYCLE,
        EffectOrigin.TEMPORAL_ACTIVITY,
    ),
    *(
        _entry(
            f"riftx.execution.reconciliation:ExecutionReconciler.{method}",
            RunEffectOperation.EXECUTION_RECONCILE,
            EffectOrigin.SAFETY_RECONCILER,
        )
        for method in ("reconcile_run", "reconcile_execution")
    ),
    _entry(
        "riftx.api.runtime:ControlPlane._reconcile_completing_runs",
        RunEffectOperation.CONTROL_PLANE_CLEANUP_RECONCILE,
        EffectOrigin.CONTROL_PLANE_RECONCILER,
    ),
    _entry(
        "riftx.temporal.worker_runtime:TemporalWorkerRuntime._safety_reconciler_loop",
        RunEffectOperation.WORKER_CLEANUP_RECONCILE,
        EffectOrigin.WORKER_RECONCILER,
    ),
    _entry(
        "riftx.temporal.worker_runtime:TemporalWorkerRuntime._reconcile_finalization",
        RunEffectOperation.WORKER_CLEANUP_RECONCILE,
        EffectOrigin.WORKER_RECONCILER,
    ),
    _entry(
        "riftx.api.runtime:ControlPlane._reconcile_runner_state",
        RunEffectOperation.CONTROL_PLANE_RUNNER_RECONCILE,
        EffectOrigin.CONTROL_PLANE_RECONCILER,
    ),
    _entry(
        "riftx.api.runtime:ControlPlane._reconcile_audit_preflight_jobs",
        RunEffectOperation.CONTROL_PLANE_AUDIT_PREFLIGHT_RECONCILE,
        EffectOrigin.CONTROL_PLANE_RECONCILER,
    ),
    _entry(
        "riftx.api.runtime:ControlPlane._reconcile_workflow_signals",
        RunEffectOperation.CONTROL_PLANE_WORKFLOW_SIGNAL_RECONCILE,
        EffectOrigin.CONTROL_PLANE_RECONCILER,
    ),
    _entry(
        "riftx.temporal.worker_runtime:TemporalWorkerRuntime._runner_reconciliation_loop",
        RunEffectOperation.WORKER_RUNNER_RECONCILE,
        EffectOrigin.WORKER_RECONCILER,
    ),
    _entry(
        "riftx.temporal.worker_runtime:TemporalWorkerRuntime._workflow_signal_loop",
        RunEffectOperation.WORKER_WORKFLOW_SIGNAL_RECONCILE,
        EffectOrigin.WORKER_RECONCILER,
    ),
    _entry(
        "riftx.application.services.run_safety:RunSafetyStopService.stop_run",
        RunEffectOperation.SAFETY_STOP_RUN,
        EffectOrigin.SAFETY_RECONCILER,
    ),
    *(
        _entry(
            f"riftx.persistence.repositories:SQLAlchemyRunnerCommandRepository.{method}",
            operation,
            EffectOrigin.RUNNER_COMMAND,
        )
        for method, operation in (
            ("enqueue", RunEffectOperation.RUNNER_COMMAND_ENQUEUE),
            ("enqueue", RunEffectOperation.RUNNER_COMMAND_REPLAY),
            ("lease_next", RunEffectOperation.RUNNER_COMMAND_CLAIM),
            ("renew_lease", RunEffectOperation.RUNNER_COMMAND_RENEW),
            ("finish", RunEffectOperation.RUNNER_COMMAND_FINISH),
            ("finish", RunEffectOperation.RUNNER_COMMAND_STOP_ACK),
            (
                "record_legacy_stop_ack",
                RunEffectOperation.RUNNER_COMMAND_LEGACY_STOP_ACK,
            ),
        )
    ),
)


def _managed_type(
    qualified_name: str,
    *,
    read_only: tuple[str, ...] = (),
    out_of_scope: tuple[tuple[str, str], ...] = (),
) -> ManagedEffectType:
    return ManagedEffectType(
        qualified_name=qualified_name,
        read_only_methods=frozenset(read_only),
        out_of_scope_methods=tuple(
            ManagedOutOfScopeMethod(method_name, reason) for method_name, reason in out_of_scope
        ),
    )


# Every public async method on these classes must be exactly one of:
# effect entrypoint, explicit read-only method, or explicit out-of-scope method.
# The validator imports and inspects the live classes, so adding a method without
# updating this inventory fails CI rather than silently inheriting a default.
MANAGED_EFFECT_TYPES: tuple[ManagedEffectType, ...] = (
    _managed_type(
        "riftx.application.services.runs:RunApplicationService",
        read_only=("get_run", "list_runs", "list_runs_for_reconciliation", "resolve_kind"),
    ),
    _managed_type(
        "riftx.application.services.workflow_signals:WorkflowSignalOutboxApplicationService",
    ),
    _managed_type(
        "riftx.application.services.workflow_signals:WorkflowSignalDispatcher",
    ),
    _managed_type(
        "riftx.application.services.workflow_signals:WorkflowSignalReconciler",
    ),
    _managed_type("riftx.persistence.audit_control_uow:SQLAlchemyAuditControlUnitOfWork"),
    _managed_type(
        "riftx.application.services.audits:AuditApplicationService",
        read_only=(
            "cancel",
            "get",
            "get_authorized",
            "get_by_run_authorized",
            "list",
            "list_authorized",
            "pause",
            "resume",
        ),
    ),
    _managed_type(
        "riftx.application.services.audit_preflight:AuditPreflightApplicationService",
    ),
    _managed_type(
        "riftx.application.services.audit_preflight_runner:AuditPreflightRunnerService",
        read_only=("authenticate",),
    ),
    _managed_type(
        "riftx.persistence.audit_preflight:SQLAlchemyAuditPreflightRepository",
        read_only=(
            "get_owner_binding",
            "get_idempotency_binding",
            "get",
            "get_reconciliation_candidate",
            "get_replayable_claim",
            "list_reconciliation_candidates",
        ),
    ),
    _managed_type(
        "riftx.application.services.audit_controls:AuditControlApplicationService",
    ),
    _managed_type(
        "riftx.application.services.approvals:ApprovalApplicationService",
        read_only=("get", "list"),
    ),
    _managed_type("riftx.application.services.approvals:ApprovalRequestRecorder"),
    _managed_type("riftx.application.services.approvals:RuntimeApprovalRequestRecorder"),
    _managed_type(
        "riftx.application.services.artifacts:ArtifactApplicationService",
        read_only=(
            "get",
            "get_for_audit",
            "list",
            "list_for_audit",
            "open_audit_content",
            "open_public_content",
            "read_content_slice",
            "resolve_owner",
            "resolve_run_id",
        ),
    ),
    _managed_type(
        "riftx.application.services.executions:ExecutionApplicationService",
        read_only=("get", "list", "list_active", "output", "resolve_run_id", "wait"),
    ),
    _managed_type(
        "riftx.execution.service:ExecutionService",
        read_only=("find_admission", "get"),
    ),
    _managed_type(
        "riftx.execution.deferred:DeferredExecutionDispatcher",
        read_only=(
            "find_execution_admission",
            "pending_intents",
            "require_current_intent_execution_claim",
        ),
    ),
    _managed_type(
        "riftx.application.services.findings:FindingApplicationService",
        read_only=("get_finding", "list_findings", "resolve_run_id"),
    ),
    _managed_type(
        "riftx.application.services.reports:ReportApplicationService",
        read_only=("build_source", "get", "list", "resolve_run_id"),
    ),
    _managed_type(
        "riftx.memory.service:MemoryService",
        read_only=("get", "list", "list_scope", "resolve_scope", "retrieve"),
    ),
    _managed_type(
        "riftx.application.services.terminals:TerminalApplicationService",
        read_only=("get", "materialize_launch_request", "read", "resolve_run_id"),
    ),
    _managed_type(
        "riftx.browser.service:BrowserApplicationService",
        read_only=("get", "list_for_run", "observations_after", "resolve_run_id"),
    ),
    _managed_type("riftx.target_http.service:TargetHttpApplicationService"),
    _managed_type("riftx.connectors.service:ConnectorApplicationService"),
    _managed_type(
        "riftx.context.inspector:ContextApplicationService",
        read_only=(
            "get",
            "latest_for_run",
            "latest_for_session",
            "resolve_latest_for_session",
            "resolve_run_id",
        ),
    ),
    _managed_type(
        "riftx.application.services.runner_control:RunnerControlService",
        read_only=(
            "authenticate",
            "current_principal",
            "read_command_output",
            "require_execution_callback_kind",
            "wait_command",
        ),
    ),
    _managed_type("riftx.application.workflow_router:RunWorkflowControlRouter"),
    _managed_type(
        "riftx.temporal.workflow_signal_transport:RoutedWorkflowSignalTransport",
    ),
    _managed_type(
        "riftx.temporal.workflow_signal_transport:TemporalWorkflowSignalOutcomeProbe",
    ),
    _managed_type("riftx.runtime.coordinator:RuntimeCoordinator"),
    _managed_type("riftx.temporal.activities:RiftXActivities"),
    _managed_type("riftx.temporal.runtime_activity:RuntimeCycleActivities"),
    _managed_type("riftx.application.services.run_safety:RunSafetyStopService"),
    _managed_type("riftx.execution.reconciliation:ExecutionReconciler"),
    _managed_type(
        "riftx.application.services.nodes:NodeApplicationService",
        read_only=("get", "list"),
    ),
    _managed_type(
        "riftx.application.services.models:ModelProfileApplicationService",
        read_only=("get_profile", "list_profiles", "resolve_profile"),
    ),
    _managed_type(
        "riftx.application.services.tools:ToolApplicationService",
        read_only=("list_tools",),
    ),
    _managed_type(
        "riftx.api.runtime:ControlPlane",
        out_of_scope=(
            (
                "close",
                "process shutdown root; Run cleanup and reconciliation "
                "entrypoints are inventoried separately",
            ),
        ),
    ),
    _managed_type(
        "riftx.temporal.worker_runtime:TemporalWorkerRuntime",
        out_of_scope=(
            (
                "run",
                "worker process lifecycle root; effect callbacks and reconcilers "
                "are inventoried separately",
            ),
            (
                "close",
                "worker process lifecycle root; effect callbacks and reconcilers "
                "are inventoried separately",
            ),
        ),
    ),
)


class RunKindEffectInventoryError(RuntimeError):
    """Raised when a route or managed effect entrypoint drifts from the catalog."""


def resolve_run_kind_effect_policy(
    operation: RunEffectOperation | str,
    origin: EffectOrigin | str,
) -> RunKindEffectPolicy:
    """Resolve one exact policy or deny without reflecting caller-controlled values."""

    try:
        resolved_operation = RunEffectOperation(operation)
    except (TypeError, ValueError) as exc:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.UNKNOWN_OPERATION) from exc
    try:
        resolved_origin = EffectOrigin(origin)
    except (TypeError, ValueError) as exc:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.UNKNOWN_ORIGIN) from exc
    policy = RUN_KIND_EFFECT_POLICIES.get((resolved_operation, resolved_origin))
    if policy is None:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.UNREGISTERED_OPERATION_ORIGIN)
    return policy


def require_run_kind_effect_policy(
    operation: RunEffectOperation | str,
    origin: EffectOrigin | str,
    *,
    ownership: ResolvedEffectOwnership | object,
    effect: OperationEffect | str,
    mode: EffectMode | str,
) -> RunKindEffectPolicy:
    """Require an exact effect/mode/owner match; every mismatch fails closed."""

    policy = resolve_run_kind_effect_policy(operation, origin)
    try:
        resolved_effect = OperationEffect(effect)
    except (TypeError, ValueError) as exc:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.UNKNOWN_EFFECT) from exc
    if resolved_effect is not policy.required_effect:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.EFFECT_MISMATCH)
    try:
        resolved_mode = EffectMode(mode)
    except (TypeError, ValueError) as exc:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.UNKNOWN_MODE) from exc
    if resolved_mode is not policy.effect_mode:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.MODE_MISMATCH)

    resolved_owner_kind, resolved_ownership = _resolve_effect_ownership(ownership)
    if resolved_owner_kind is not policy.owner_kind:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.OWNER_KIND_MISMATCH)
    if any(not _ownership_has_claim(resolved_ownership, claim) for claim in policy.required_claims):
        raise RunKindEffectPolicyDenied(PolicyDenialReason.OWNERSHIP_CLAIM_MISSING)

    if policy.owner_kind is not EffectOwnerKind.RUN:
        return policy
    assert isinstance(resolved_ownership, RunEffectOwnership)
    try:
        resolved_kind = RunKind(resolved_ownership.run_kind)
    except (TypeError, ValueError) as exc:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.UNKNOWN_RUN_KIND) from exc
    if resolved_kind not in policy.allowed_run_kinds:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.RUN_KIND_UNSUPPORTED)
    return policy


def _resolve_effect_ownership(
    ownership: ResolvedEffectOwnership | object,
) -> tuple[EffectOwnerKind, ResolvedEffectOwnership]:
    try:
        owner_kind = EffectOwnerKind(object.__getattribute__(ownership, "owner_kind"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.UNKNOWN_OWNER_KIND) from exc
    expected_type: type[ResolvedEffectOwnership] = {
        EffectOwnerKind.GLOBAL: GlobalEffectOwnership,
        EffectOwnerKind.RUN: RunEffectOwnership,
        EffectOwnerKind.PREFLIGHT_JOB: PreflightJobEffectOwnership,
        EffectOwnerKind.LEGACY_RUNNER_COMMAND: LegacyRunnerCommandEffectOwnership,
    }[owner_kind]
    if type(ownership) is not expected_type:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.OWNERSHIP_VARIANT_INVALID)
    return owner_kind, ownership


def _ownership_has_claim(
    ownership: ResolvedEffectOwnership,
    claim: OwnershipClaim,
) -> bool:
    attribute = {
        OwnershipClaim.ADMINISTRATIVE_SCOPE_DIGEST: "administrative_scope_digest",
        OwnershipClaim.RUN_ID: "run_id",
        OwnershipClaim.RUN_KIND: "run_kind",
        OwnershipClaim.AUDIT_ID: "audit_id",
        OwnershipClaim.PLAN_DIGEST: "plan_digest",
        OwnershipClaim.EXECUTION_ID: "execution_id",
        OwnershipClaim.EFFECT_EXECUTION_ID: "effect_execution_id",
        OwnershipClaim.RESOURCE_KIND: "resource_kind",
        OwnershipClaim.RESOURCE_ID: "resource_id",
        OwnershipClaim.NODE_ID: "node_id",
        OwnershipClaim.RUNNER_PRINCIPAL: "runner_principal",
        OwnershipClaim.RUNNER_COMMAND_ID: "runner_command_id",
        OwnershipClaim.PREFLIGHT_JOB_ID: "preflight_job_id",
        OwnershipClaim.OPERATOR_PRINCIPAL_ID: "operator_principal_id",
        OwnershipClaim.AUTHORIZATION_SCOPE_DIGEST: "authorization_scope_digest",
        OwnershipClaim.REQUEST_DIGEST: "request_digest",
        OwnershipClaim.CAPSULE_ID: "capsule_id",
        OwnershipClaim.LEASE_IDENTITY: "lease_identity",
        OwnershipClaim.QUARANTINE_STATE: "quarantine_state",
    }[claim]
    value = getattr(ownership, attribute, None)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _enum_value(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else None


def validate_api_route_effect_inventory(route_policies: Mapping[str, object]) -> None:
    """Verify every API RoutePolicy has one exact RunKind effect rule."""

    route_names = set(route_policies)
    binding_names = set(API_ROUTE_EFFECT_BINDINGS)
    missing = sorted(route_names - binding_names)
    stale = sorted(binding_names - route_names)
    mismatched: list[str] = []
    expected_authorization = {
        EffectOrigin.LOCAL_OPERATOR_API: "local_operator",
        EffectOrigin.WEBSOCKET: "local_operator",
        EffectOrigin.ADMIN_API: "admin_token",
        EffectOrigin.RUNNER_API: None,
    }
    for route_name in sorted(route_names & binding_names):
        route_policy = route_policies[route_name]
        binding = API_ROUTE_EFFECT_BINDINGS[route_name]
        catalog_policy = RUN_KIND_EFFECT_POLICIES.get((binding.operation, binding.origin))
        if catalog_policy is None:
            mismatched.append(f"{route_name}:missing_catalog_policy")
            continue
        actual_effect = _enum_value(getattr(route_policy, "effect", None))
        if actual_effect != catalog_policy.required_effect.value:
            mismatched.append(
                f"{route_name}:effect={actual_effect!r}:"
                f"catalog={catalog_policy.required_effect.value!r}"
            )
        actual_authorization = _enum_value(getattr(route_policy, "authorization", None))
        expected = expected_authorization[binding.origin]
        if binding.origin is EffectOrigin.RUNNER_API:
            if actual_authorization not in {"runner_token", "runner_bootstrap_token"}:
                mismatched.append(
                    f"{route_name}:authorization={actual_authorization!r}:catalog='runner'"
                )
        elif actual_authorization != expected:
            mismatched.append(
                f"{route_name}:authorization={actual_authorization!r}:catalog={expected!r}"
            )
    if missing or stale or mismatched:
        raise RunKindEffectInventoryError(
            "RunKind API effect inventory validation failed: "
            f"missing={missing}, stale={stale}, mismatched={sorted(mismatched)}"
        )


def validate_managed_effect_inventory(
    entrypoints: tuple[ManagedEffectEntrypoint, ...] = MANAGED_EFFECT_ENTRYPOINTS,
    managed_types: tuple[ManagedEffectType, ...] = MANAGED_EFFECT_TYPES,
) -> None:
    """Resolve live symbols and classify every managed public async method once."""

    missing: list[str] = []
    duplicates: list[str] = []
    unresolved: list[str] = []
    seen: set[tuple[str, RunEffectOperation, EffectOrigin]] = set()
    for entrypoint in entrypoints:
        key = (entrypoint.qualified_name, entrypoint.operation, entrypoint.origin)
        if key in seen:
            duplicates.append(
                f"{entrypoint.qualified_name}:{entrypoint.operation.value}@"
                f"{entrypoint.origin.value}"
            )
        seen.add(key)
        if (entrypoint.operation, entrypoint.origin) not in RUN_KIND_EFFECT_POLICIES:
            missing.append(
                f"{entrypoint.qualified_name}:{entrypoint.operation.value}@"
                f"{entrypoint.origin.value}"
            )
        try:
            target = _resolve_qualified_symbol(entrypoint.qualified_name)
        except (AttributeError, ImportError, ValueError):
            unresolved.append(entrypoint.qualified_name)
        else:
            if not callable(target) or not inspect.iscoroutinefunction(target):
                unresolved.append(entrypoint.qualified_name)

    duplicate_types: list[str] = []
    seen_types: set[str] = set()
    for managed_type in managed_types:
        if managed_type.qualified_name in seen_types:
            duplicate_types.append(managed_type.qualified_name)
        seen_types.add(managed_type.qualified_name)

    unclassified_methods: list[str] = []
    stale_classifications: list[str] = []
    conflicting_classifications: list[str] = []
    for managed_type in managed_types:
        try:
            target_type = _resolve_qualified_symbol(managed_type.qualified_name)
        except (AttributeError, ImportError, ValueError):
            unresolved.append(managed_type.qualified_name)
            continue
        if not inspect.isclass(target_type):
            unresolved.append(managed_type.qualified_name)
            continue

        public_async_methods = {
            name
            for name, value in inspect.getmembers(target_type, inspect.iscoroutinefunction)
            if not name.startswith("_")
        }
        prefix = f"{managed_type.qualified_name}."
        effect_method_counts: dict[str, int] = {}
        for entrypoint in entrypoints:
            if not entrypoint.qualified_name.startswith(prefix):
                continue
            method_name = entrypoint.qualified_name.removeprefix(prefix)
            if "." in method_name or method_name.startswith("_"):
                continue
            effect_method_counts[method_name] = effect_method_counts.get(method_name, 0) + 1
        effect_methods = set(effect_method_counts)
        out_of_scope_names = [item.method_name for item in managed_type.out_of_scope_methods]
        out_of_scope_methods = set(out_of_scope_names)
        read_only_methods = set(managed_type.read_only_methods)

        for method_name, count in effect_method_counts.items():
            if count != 1:
                conflicting_classifications.append(
                    f"{managed_type.qualified_name}.{method_name}:effect_count={count}"
                )
        duplicate_out_of_scope = {
            name for name in out_of_scope_names if out_of_scope_names.count(name) > 1
        }
        for method_name in sorted(duplicate_out_of_scope):
            conflicting_classifications.append(
                f"{managed_type.qualified_name}.{method_name}:duplicate_out_of_scope"
            )
        for method_name in sorted(
            (effect_methods & read_only_methods)
            | (effect_methods & out_of_scope_methods)
            | (read_only_methods & out_of_scope_methods)
        ):
            conflicting_classifications.append(
                f"{managed_type.qualified_name}.{method_name}:multiple_categories"
            )

        declared_methods = effect_methods | read_only_methods | out_of_scope_methods
        unclassified_methods.extend(
            f"{managed_type.qualified_name}.{method_name}"
            for method_name in sorted(public_async_methods - declared_methods)
        )
        stale_classifications.extend(
            f"{managed_type.qualified_name}.{method_name}"
            for method_name in sorted(declared_methods - public_async_methods)
        )

    if (
        missing
        or duplicates
        or unresolved
        or duplicate_types
        or unclassified_methods
        or stale_classifications
        or conflicting_classifications
    ):
        raise RunKindEffectInventoryError(
            "managed RunKind effect inventory validation failed: "
            f"missing={sorted(missing)}, duplicates={sorted(duplicates)}, "
            f"unresolved={sorted(set(unresolved))}, "
            f"duplicate_types={sorted(duplicate_types)}, "
            f"unclassified={sorted(unclassified_methods)}, "
            f"stale={sorted(stale_classifications)}, "
            f"conflicting={sorted(conflicting_classifications)}"
        )


def _resolve_qualified_symbol(qualified_name: str) -> object:
    module_name, separator, symbol_path = qualified_name.partition(":")
    if not separator or not module_name or not symbol_path:
        raise ValueError("qualified symbol must use module:path syntax")
    target: object = importlib.import_module(module_name)
    for segment in symbol_path.split("."):
        target = getattr(target, segment)
    return target


# The CI policy test invokes ``validate_managed_effect_inventory`` explicitly.
# Eager validation here would import every managed production
# module while this module itself is still initializing, creating circular
# imports for services that consume the policy at runtime.
