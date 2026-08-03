"""Mappings between domain objects and SQLAlchemy records."""

from __future__ import annotations

from datetime import datetime

from riftx.application.errors import RepositoryIntegrityError
from riftx.domain import (
    RUNNER_COMMAND_OWNERSHIP_SCHEMA_VERSION,
    RUNNER_EFFECT_BINDING_SCHEMA_VERSION,
    RUNNER_STOP_RECEIPT_SCHEMA_VERSION,
    Approval,
    ApprovalDecision,
    ApprovalGrant,
    ApprovalMode,
    ApprovalStatus,
    Artifact,
    ArtifactAccessClass,
    ArtifactContentTrust,
    ArtifactIngestProvenance,
    Engagement,
    EntryPoint,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Finding,
    FindingEvidence,
    FindingSeverity,
    FindingStatus,
    Node,
    NodeStatus,
    Objective,
    Report,
    ReportFormat,
    Run,
    RunEvent,
    RunKind,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandOwnership,
    RunnerCommandOwnershipState,
    RunnerCommandStatus,
    RunnerCredential,
    RunnerEffectBinding,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
    RunnerStopReceipt,
    RunStatus,
    Scope,
    SuccessCriterion,
    TerminalOwner,
    TerminalSession,
    TerminalStatus,
    ToolCall,
)

from .orm import (
    ApprovalGrantRecord,
    ApprovalRecord,
    ArtifactRecord,
    EngagementRecord,
    ExecutionRecord,
    FindingRecord,
    NodeRecord,
    ReportRecord,
    RunEventRecord,
    RunnerCommandOwnershipRecord,
    RunnerCommandRecord,
    RunnerCredentialRecord,
    RunnerEffectBindingRecord,
    RunnerStopReceiptRecord,
    RunRecord,
    TerminalSessionRecord,
    ToolCallRecord,
)


def node_to_record(node: Node) -> NodeRecord:
    return NodeRecord(
        id=node.id,
        name=node.name,
        platform=node.platform,
        architecture=node.architecture,
        runner_version=node.runner_version,
        status=node.status.value,
        capabilities_json=node.capabilities,
        labels_json=node.labels,
        current_runner_instance_id=(
            node.current_owner.instance_id if node.current_owner is not None else None
        ),
        current_runner_epoch=(node.current_owner.epoch if node.current_owner is not None else 0),
        last_seen_at=node.last_seen_at,
        created_at=node.created_at,
        updated_at=node.updated_at,
    )


def apply_node_to_record(node: Node, record: NodeRecord) -> None:
    record.name = node.name
    record.platform = node.platform
    record.architecture = node.architecture
    record.runner_version = node.runner_version
    record.status = node.status.value
    record.capabilities_json = node.capabilities
    record.labels_json = node.labels
    record.last_seen_at = node.last_seen_at
    record.updated_at = node.updated_at


def node_from_record(record: NodeRecord) -> Node:
    return Node(
        id=record.id,
        name=record.name,
        platform=record.platform,
        architecture=record.architecture,
        runner_version=record.runner_version,
        status=NodeStatus(record.status),
        capabilities=record.capabilities_json or [],
        labels=record.labels_json or {},
        current_owner=(
            RunnerPrincipal(
                instance_id=record.current_runner_instance_id,
                epoch=record.current_runner_epoch,
            )
            if record.current_runner_instance_id is not None and record.current_runner_epoch > 0
            else None
        ),
        last_seen_at=record.last_seen_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def artifact_to_record(artifact: Artifact) -> ArtifactRecord:
    return ArtifactRecord(
        id=artifact.id,
        run_id=artifact.run_id,
        execution_id=artifact.execution_id,
        audit_id=artifact.audit_id,
        access_class=artifact.access_class.value,
        content_trust=artifact.content_trust.value,
        name=artifact.name,
        path=artifact.path,
        storage_key=artifact.storage_key,
        ingest_provenance_json=artifact.ingest_provenance.model_dump(mode="json"),
        mime_type=artifact.mime_type,
        sha256=artifact.sha256,
        size=artifact.size,
        description=artifact.description,
        created_at=artifact.created_at,
    )


def artifact_from_record(record: ArtifactRecord) -> Artifact:
    try:
        return Artifact(
            id=record.id,
            run_id=record.run_id,
            execution_id=record.execution_id,
            audit_id=record.audit_id,
            access_class=ArtifactAccessClass(record.access_class),
            content_trust=ArtifactContentTrust(record.content_trust),
            name=record.name,
            path=record.path,
            storage_key=record.storage_key,
            ingest_provenance=ArtifactIngestProvenance.model_validate(
                record.ingest_provenance_json
            ),
            mime_type=record.mime_type,
            sha256=record.sha256,
            size=record.size,
            description=record.description,
            created_at=record.created_at,
        )
    except RepositoryIntegrityError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        raise RepositoryIntegrityError(
            "Artifact",
            getattr(record, "id", "invalid-id"),
        ) from None


def engagement_to_record(engagement: Engagement) -> EngagementRecord:
    return EngagementRecord(
        id=engagement.id,
        name=engagement.name,
        description=engagement.description,
        authorization_reference=engagement.authorization_reference,
        created_at=engagement.created_at,
        updated_at=engagement.updated_at,
    )


def engagement_from_record(record: EngagementRecord) -> Engagement:
    return Engagement(
        id=record.id,
        name=record.name,
        description=record.description,
        authorization_reference=record.authorization_reference,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def runner_credential_to_record(credential: RunnerCredential) -> RunnerCredentialRecord:
    return RunnerCredentialRecord(
        runner_instance_id=credential.principal.instance_id,
        node_id=credential.node_id,
        runner_epoch=credential.principal.epoch,
        token_hash=credential.token_hash,
        token_prefix=credential.token_prefix,
        protocol_capabilities_json=list(credential.protocol_capabilities),
        created_at=credential.created_at,
        rotated_at=credential.rotated_at,
        revoked_at=credential.revoked_at,
    )


def apply_runner_credential_to_record(
    credential: RunnerCredential,
    record: RunnerCredentialRecord,
) -> None:
    if (
        record.runner_instance_id != credential.principal.instance_id
        or record.node_id != credential.node_id
        or record.runner_epoch != credential.principal.epoch
    ):
        raise ValueError("Runner credential principal is immutable")
    record.token_hash = credential.token_hash
    record.token_prefix = credential.token_prefix
    if tuple(record.protocol_capabilities_json or []) != credential.protocol_capabilities:
        raise ValueError("Runner credential protocol capabilities are immutable")
    record.rotated_at = credential.rotated_at
    record.revoked_at = credential.revoked_at


def runner_credential_from_record(record: RunnerCredentialRecord) -> RunnerCredential:
    return RunnerCredential(
        node_id=record.node_id,
        principal=RunnerPrincipal(
            instance_id=record.runner_instance_id,
            epoch=record.runner_epoch,
        ),
        token_hash=record.token_hash,
        token_prefix=record.token_prefix,
        protocol_capabilities=tuple(record.protocol_capabilities_json or []),
        created_at=record.created_at,
        rotated_at=record.rotated_at,
        revoked_at=record.revoked_at,
    )


def runner_command_to_record(command: RunnerCommand) -> RunnerCommandRecord:
    return RunnerCommandRecord(
        id=command.id,
        node_id=command.node_id,
        kind=command.kind.value,
        idempotency_key=command.idempotency_key,
        target_runner_instance_id=(
            command.target.instance_id if command.target is not None else None
        ),
        target_runner_epoch=(command.target.epoch if command.target is not None else None),
        payload_json=command.payload,
        status=command.status.value,
        attempts=command.attempts,
        lease_id=command.lease_id,
        lease_expires_at=command.lease_expires_at,
        result_json=command.result,
        error=command.error,
        state_version=command.state_version,
        created_at=command.created_at,
        updated_at=command.updated_at,
        completed_at=command.completed_at,
    )


def runner_effect_binding_to_record(binding: RunnerEffectBinding) -> RunnerEffectBindingRecord:
    return RunnerEffectBindingRecord(
        id=binding.id,
        schema_version=binding.schema_version,
        run_id=binding.run_id,
        run_kind=binding.run_kind.value,
        node_id=binding.node_id,
        target_runner_instance_id=binding.target.instance_id,
        target_runner_epoch=binding.target.epoch,
        origin=binding.origin.value,
        operation_family=binding.operation_family.value,
        execution_id=binding.execution_id,
        resource_kind=binding.resource_kind.value,
        resource_id=binding.resource_id,
        audit_id=binding.audit_id,
        plan_digest=binding.plan_digest,
        binding_digest=binding.binding_digest,
        created_at=binding.created_at,
    )


def runner_command_ownership_to_record(
    command: RunnerCommand,
) -> RunnerCommandOwnershipRecord:
    ownership = command.ownership
    return RunnerCommandOwnershipRecord(
        command_id=command.id,
        verification_state=command.ownership_state.value,
        schema_version=(ownership.schema_version if ownership is not None else None),
        effect_binding_id=(ownership.effect_binding.id if ownership is not None else None),
        operation=(ownership.operation.value if ownership is not None else None),
        operation_family=(
            ownership.operation_family.value if ownership is not None else None
        ),
        payload_digest=(ownership.payload_digest if ownership is not None else None),
        output_contract_json=(
            ownership.output_contract.model_dump(mode="json") if ownership is not None else None
        ),
        output_contract_digest=(
            ownership.output_contract_digest if ownership is not None else None
        ),
        envelope_digest=(ownership.envelope_digest if ownership is not None else None),
        quarantine_reason=(
            command.quarantine_reason
            if command.ownership_state is RunnerCommandOwnershipState.QUARANTINED
            else None
        ),
        quarantined_at=(
            command.updated_at
            if command.ownership_state is RunnerCommandOwnershipState.QUARANTINED
            else None
        ),
        reconciliation_state=(
            "untouched"
            if command.ownership_state is RunnerCommandOwnershipState.QUARANTINED
            and ownership is None
            else (
                "pending"
                if command.ownership_state is RunnerCommandOwnershipState.QUARANTINED
                else None
            )
        ),
        replacement_command_id=None,
        created_at=(ownership.created_at if ownership is not None else command.created_at),
    )


def runner_command_from_record(
    record: RunnerCommandRecord,
    ownership_record: RunnerCommandOwnershipRecord | None = None,
    binding_record: RunnerEffectBindingRecord | None = None,
) -> RunnerCommand:
    try:
        target = (
            RunnerPrincipal(
                instance_id=record.target_runner_instance_id,
                epoch=record.target_runner_epoch,
            )
            if record.target_runner_instance_id is not None
            and record.target_runner_epoch is not None
            else None
        )
        state = (
            RunnerCommandOwnershipState(ownership_record.verification_state)
            if ownership_record is not None
            else RunnerCommandOwnershipState.QUARANTINED
        )
        ownership = (
            _runner_command_ownership_from_record(ownership_record, binding_record)
            if state is RunnerCommandOwnershipState.VERIFIED
            else None
        )
        reason = (
            ownership_record.quarantine_reason or "ownership_quarantine_reason_missing"
            if ownership_record is not None
            and state is RunnerCommandOwnershipState.QUARANTINED
            else ("ownership_record_missing" if ownership_record is None else "")
        )
        return RunnerCommand(
            id=record.id,
            node_id=record.node_id,
            kind=RunnerCommandKind(record.kind),
            idempotency_key=record.idempotency_key,
            target=target,
            ownership=ownership,
            ownership_state=state,
            quarantine_reason=reason,
            payload=record.payload_json or {},
            status=RunnerCommandStatus(record.status),
            attempts=record.attempts,
            lease_id=record.lease_id,
            lease_expires_at=record.lease_expires_at,
            result=record.result_json or {},
            error=record.error,
            state_version=record.state_version,
            created_at=record.created_at,
            updated_at=record.updated_at,
            completed_at=record.completed_at,
        )
    except (TypeError, ValueError) as exc:
        raise RepositoryIntegrityError(
            "RunnerCommand",
            record.id,
            reason_code="runner_command_ownership_invalid",
        ) from exc


def _runner_command_ownership_from_record(
    record: RunnerCommandOwnershipRecord | None,
    binding_record: RunnerEffectBindingRecord | None,
) -> RunnerCommandOwnership | None:
    if record is None or record.schema_version is None or binding_record is None:
        return None
    if record.schema_version != RUNNER_COMMAND_OWNERSHIP_SCHEMA_VERSION:
        raise ValueError("Runner command ownership schema version is unsupported")
    required = (
        record.effect_binding_id,
        record.operation,
        record.operation_family,
        record.payload_digest,
        record.output_contract_json,
        record.output_contract_digest,
        record.envelope_digest,
    )
    if any(value is None for value in required):
        raise ValueError("Runner command ownership record is incomplete")
    assert record.effect_binding_id is not None
    assert record.operation is not None
    assert record.operation_family is not None
    assert record.payload_digest is not None
    assert record.output_contract_json is not None
    assert record.output_contract_digest is not None
    assert record.envelope_digest is not None
    if binding_record.id != record.effect_binding_id:
        raise ValueError("Runner command ownership effect binding does not match")
    return RunnerCommandOwnership(
        schema_version=RUNNER_COMMAND_OWNERSHIP_SCHEMA_VERSION,
        command_id=record.command_id,
        effect_binding=_runner_effect_binding_from_record(binding_record),
        operation=RunnerCommandKind(record.operation),
        operation_family=RunnerOperationFamily(record.operation_family),
        payload_digest=record.payload_digest,
        output_contract=RunnerOutputContract.model_validate(record.output_contract_json),
        output_contract_digest=record.output_contract_digest,
        envelope_digest=record.envelope_digest,
        created_at=record.created_at,
    )


def _runner_effect_binding_from_record(record: RunnerEffectBindingRecord) -> RunnerEffectBinding:
    if record.schema_version != RUNNER_EFFECT_BINDING_SCHEMA_VERSION:
        raise ValueError("Runner effect binding schema version is unsupported")
    return RunnerEffectBinding(
        id=record.id,
        schema_version=RUNNER_EFFECT_BINDING_SCHEMA_VERSION,
        run_id=record.run_id,
        run_kind=RunKind(record.run_kind),
        node_id=record.node_id,
        target=RunnerPrincipal(
            instance_id=record.target_runner_instance_id,
            epoch=record.target_runner_epoch,
        ),
        origin=RunnerCommandOrigin(record.origin),
        operation_family=RunnerOperationFamily(record.operation_family),
        execution_id=record.execution_id,
        resource_kind=RunnerResourceKind(record.resource_kind),
        resource_id=record.resource_id,
        audit_id=record.audit_id,
        plan_digest=record.plan_digest,
        binding_digest=record.binding_digest,
        created_at=record.created_at,
    )


def runner_stop_receipt_to_record(
    receipt: RunnerStopReceipt,
    *,
    ack: dict[str, object],
) -> RunnerStopReceiptRecord:
    return RunnerStopReceiptRecord(
        id=receipt.id,
        schema_version=receipt.schema_version,
        command_id=receipt.command_id,
        effect_binding_id=receipt.effect_binding_id,
        envelope_digest=receipt.envelope_digest,
        binding_digest=receipt.binding_digest,
        operation=receipt.operation.value,
        operation_family=receipt.operation_family.value,
        resource_kind=receipt.resource_kind.value,
        resource_id=receipt.resource_id,
        execution_id=receipt.execution_id,
        node_id=receipt.node_id,
        principal_instance_id=receipt.principal.instance_id,
        principal_epoch=receipt.principal.epoch,
        ack_digest=receipt.ack_digest,
        ack_json=ack,
        received_at=receipt.received_at,
    )


def runner_stop_receipt_from_record(record: RunnerStopReceiptRecord) -> RunnerStopReceipt:
    if record.schema_version != RUNNER_STOP_RECEIPT_SCHEMA_VERSION:
        raise ValueError("Runner stop receipt schema version is unsupported")
    return RunnerStopReceipt(
        id=record.id,
        schema_version=RUNNER_STOP_RECEIPT_SCHEMA_VERSION,
        command_id=record.command_id,
        effect_binding_id=record.effect_binding_id,
        envelope_digest=record.envelope_digest,
        binding_digest=record.binding_digest,
        operation=RunnerCommandKind(record.operation),
        operation_family=RunnerOperationFamily(record.operation_family),
        resource_kind=RunnerResourceKind(record.resource_kind),
        resource_id=record.resource_id,
        execution_id=record.execution_id,
        node_id=record.node_id,
        principal=RunnerPrincipal(
            instance_id=record.principal_instance_id,
            epoch=record.principal_epoch,
        ),
        ack_digest=record.ack_digest,
        received_at=record.received_at,
    )


def run_to_record(run: Run) -> RunRecord:
    return RunRecord(
        id=run.id,
        engagement_id=run.engagement_id,
        kind=run.kind.value,
        node_id=run.node_id,
        objective=run.objective.description,
        success_criteria_json=[item.model_dump(mode="json") for item in run.success_criteria],
        entry_points_json=[item.model_dump(mode="json") for item in run.entry_points],
        scope_json=run.scope.model_dump(mode="json"),
        status=run.status.value,
        approval_mode=run.approval_mode.value,
        model_profile=run.model_profile,
        workspace_path=run.workspace_path,
        temporal_workflow_id=run.temporal_workflow_id,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def apply_run_to_record(run: Run, record: RunRecord) -> None:
    """Copy mutable run state onto an already-persisted record."""

    if record.kind != run.kind.value:
        raise ValueError("Run kind is immutable")
    record.node_id = run.node_id
    record.objective = run.objective.description
    record.success_criteria_json = [item.model_dump(mode="json") for item in run.success_criteria]
    record.entry_points_json = [item.model_dump(mode="json") for item in run.entry_points]
    record.scope_json = run.scope.model_dump(mode="json")
    record.status = run.status.value
    record.approval_mode = run.approval_mode.value
    record.model_profile = run.model_profile
    record.workspace_path = run.workspace_path
    record.temporal_workflow_id = run.temporal_workflow_id
    record.started_at = run.started_at
    record.finished_at = run.finished_at


def run_from_record(record: RunRecord) -> Run:
    return Run(
        id=record.id,
        engagement_id=record.engagement_id,
        kind=RunKind(record.kind),
        node_id=record.node_id,
        objective=Objective(description=record.objective),
        success_criteria=[
            SuccessCriterion.model_validate(item) for item in record.success_criteria_json
        ],
        entry_points=[EntryPoint.model_validate(item) for item in record.entry_points_json],
        scope=Scope.model_validate(record.scope_json),
        status=RunStatus(record.status),
        approval_mode=ApprovalMode(record.approval_mode),
        model_profile=record.model_profile,
        workspace_path=record.workspace_path,
        temporal_workflow_id=record.temporal_workflow_id,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def event_to_record(event: RunEvent) -> RunEventRecord:
    return RunEventRecord(
        id=event.id,
        run_id=event.run_id,
        sequence=event.sequence,
        event_type=event.event_type,
        payload_json=event.payload,
        created_at=event.created_at,
    )


def event_from_record(record: RunEventRecord) -> RunEvent:
    return RunEvent(
        id=record.id,
        run_id=record.run_id,
        sequence=record.sequence,
        event_type=record.event_type,
        payload=record.payload_json,
        created_at=record.created_at,
    )


def execution_to_record(
    execution: Execution,
    *,
    updated_at: datetime,
) -> ExecutionRecord:
    return ExecutionRecord(
        id=execution.id,
        execution_key=execution.execution_key,
        launch_fingerprint=execution.launch_fingerprint,
        run_id=execution.run_id,
        audit_id=execution.audit_id,
        plan_digest=execution.plan_digest,
        session_id=execution.session_id,
        tool_call_id=execution.tool_call_id,
        attempt_group=execution.attempt_group,
        node_id=execution.node_id,
        owner_runner_instance_id=(
            execution.owner.instance_id if execution.owner is not None else None
        ),
        owner_runner_epoch=(execution.owner.epoch if execution.owner is not None else None),
        runner_command_id=execution.runner_command_id,
        runner_effect_binding_id=execution.runner_effect_binding_id,
        runner_binding_digest=execution.runner_binding_digest,
        runner_envelope_digest=execution.runner_envelope_digest,
        executor_type=execution.executor_type.value,
        argv_json=execution.argv,
        command_text=execution.command_text,
        tool_id=execution.tool_id,
        tool_version=execution.tool_version,
        executable_path=execution.executable_path,
        cwd=execution.cwd,
        env_diff_json=execution.env_diff,
        platform_system=execution.platform_system,
        platform_release=execution.platform_release,
        platform_architecture=execution.platform_architecture,
        status=execution.status.value,
        pid=execution.pid,
        process_group_id=execution.process_group_id,
        containment_id=execution.containment_id,
        exit_code=execution.exit_code,
        stdout_path=execution.stdout_path,
        stderr_path=execution.stderr_path,
        created_at=execution.created_at,
        process_created_at=execution.process_created_at,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        physical_stop_confirmed_at=execution.physical_stop_confirmed_at,
        updated_at=updated_at,
    )


def apply_execution_to_record(execution: Execution, record: ExecutionRecord) -> None:
    if record.execution_key != execution.execution_key:
        raise ValueError("Execution key is immutable after creation")
    if record.launch_fingerprint != execution.launch_fingerprint:
        raise ValueError("Execution launch fingerprint is immutable after creation")
    if record.created_at != execution.created_at:
        raise ValueError("Execution created_at is immutable after creation")
    if record.audit_id != execution.audit_id or record.plan_digest != execution.plan_digest:
        raise ValueError("Execution Audit ownership is immutable after creation")
    incoming_owner = (
        (execution.owner.instance_id, execution.owner.epoch)
        if execution.owner is not None
        else (None, None)
    )
    stored_owner = (record.owner_runner_instance_id, record.owner_runner_epoch)
    if stored_owner != (None, None) and incoming_owner != stored_owner:
        raise ValueError("Execution owner is immutable after creation")
    if stored_owner == (None, None) and incoming_owner != (None, None):
        assert execution.owner is not None
        record.owner_runner_instance_id = execution.owner.instance_id
        record.owner_runner_epoch = execution.owner.epoch
    stored_runner_binding = (
        record.runner_command_id,
        record.runner_effect_binding_id,
        record.runner_binding_digest,
        record.runner_envelope_digest,
    )
    incoming_runner_binding = (
        execution.runner_command_id,
        execution.runner_effect_binding_id,
        execution.runner_binding_digest,
        execution.runner_envelope_digest,
    )
    if stored_runner_binding != (None, None, None, None):
        if incoming_runner_binding != stored_runner_binding:
            raise ValueError("Execution Runner callback binding is immutable")
    elif incoming_runner_binding != (None, None, None, None):
        (
            record.runner_command_id,
            record.runner_effect_binding_id,
            record.runner_binding_digest,
            record.runner_envelope_digest,
        ) = incoming_runner_binding
    for field_name in (
        "pid",
        "process_group_id",
        "containment_id",
        "process_created_at",
        "executable_path",
        "tool_id",
        "tool_version",
        "platform_system",
        "platform_release",
        "platform_architecture",
        "started_at",
        "physical_stop_confirmed_at",
    ):
        persisted = getattr(record, field_name)
        proposed = getattr(execution, field_name)
        if persisted not in {None, ""} and proposed != persisted:
            raise ValueError(f"Execution bound field {field_name!r} is immutable after first write")
    record.session_id = execution.session_id
    record.tool_call_id = execution.tool_call_id
    record.attempt_group = execution.attempt_group
    record.node_id = execution.node_id
    record.executor_type = execution.executor_type.value
    record.argv_json = execution.argv
    record.command_text = execution.command_text
    record.tool_id = execution.tool_id
    record.tool_version = execution.tool_version
    record.executable_path = execution.executable_path
    record.cwd = execution.cwd
    record.env_diff_json = execution.env_diff
    record.platform_system = execution.platform_system
    record.platform_release = execution.platform_release
    record.platform_architecture = execution.platform_architecture
    record.status = execution.status.value
    record.pid = execution.pid
    record.process_group_id = execution.process_group_id
    record.containment_id = execution.containment_id
    record.exit_code = execution.exit_code
    record.stdout_path = execution.stdout_path
    record.stderr_path = execution.stderr_path
    record.process_created_at = execution.process_created_at
    record.started_at = execution.started_at
    record.finished_at = execution.finished_at
    record.physical_stop_confirmed_at = execution.physical_stop_confirmed_at


def execution_from_record(record: ExecutionRecord) -> Execution:
    return Execution(
        id=record.id,
        execution_key=record.execution_key,
        launch_fingerprint=record.launch_fingerprint,
        run_id=record.run_id,
        audit_id=record.audit_id,
        plan_digest=record.plan_digest,
        session_id=record.session_id,
        tool_call_id=record.tool_call_id,
        attempt_group=record.attempt_group,
        node_id=record.node_id,
        owner=(
            RunnerPrincipal(
                instance_id=record.owner_runner_instance_id,
                epoch=record.owner_runner_epoch,
            )
            if record.owner_runner_instance_id is not None and record.owner_runner_epoch is not None
            else None
        ),
        runner_command_id=record.runner_command_id,
        runner_effect_binding_id=record.runner_effect_binding_id,
        runner_binding_digest=record.runner_binding_digest,
        runner_envelope_digest=record.runner_envelope_digest,
        executor_type=ExecutorType(record.executor_type),
        argv=record.argv_json,
        command_text=record.command_text,
        tool_id=record.tool_id,
        tool_version=record.tool_version,
        executable_path=record.executable_path,
        cwd=record.cwd,
        env_diff=record.env_diff_json or {},
        platform_system=record.platform_system,
        platform_release=record.platform_release,
        platform_architecture=record.platform_architecture,
        status=ExecutionStatus(record.status),
        pid=record.pid,
        process_group_id=record.process_group_id,
        containment_id=record.containment_id,
        exit_code=record.exit_code,
        stdout_path=record.stdout_path,
        stderr_path=record.stderr_path,
        created_at=record.created_at,
        process_created_at=record.process_created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        physical_stop_confirmed_at=record.physical_stop_confirmed_at,
    )


def finding_to_record(
    finding: Finding,
    *,
    created_at: datetime,
    updated_at: datetime,
) -> FindingRecord:
    return FindingRecord(
        id=finding.id,
        run_id=finding.run_id,
        title=finding.title,
        severity=finding.severity.value,
        status=finding.status.value,
        affected_assets_json=finding.affected_assets,
        description=finding.description,
        evidence_json=[item.model_dump(mode="json") for item in finding.evidence],
        reproduction_steps_json=finding.reproduction_steps,
        impact=finding.impact,
        recommendation=finding.recommendation,
        created_at=created_at,
        updated_at=updated_at,
    )


def apply_finding_to_record(finding: Finding, record: FindingRecord) -> None:
    if record.id != finding.id:
        raise ValueError("Finding id is immutable after creation")
    if record.run_id != finding.run_id:
        raise ValueError("Finding run_id is immutable after creation")
    if record.created_at != finding.created_at:
        raise ValueError("Finding created_at is immutable after creation")
    record.title = finding.title
    record.severity = finding.severity.value
    record.status = finding.status.value
    record.affected_assets_json = finding.affected_assets
    record.description = finding.description
    record.evidence_json = [item.model_dump(mode="json") for item in finding.evidence]
    record.reproduction_steps_json = finding.reproduction_steps
    record.impact = finding.impact
    record.recommendation = finding.recommendation


def finding_from_record(record: FindingRecord) -> Finding:
    return Finding(
        id=record.id,
        run_id=record.run_id,
        title=record.title,
        severity=FindingSeverity(record.severity),
        status=FindingStatus(record.status),
        affected_assets=record.affected_assets_json,
        description=record.description,
        evidence=[FindingEvidence.model_validate(item) for item in record.evidence_json],
        reproduction_steps=record.reproduction_steps_json,
        impact=record.impact,
        recommendation=record.recommendation,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def tool_call_to_record(tool_call: ToolCall) -> ToolCallRecord:
    return ToolCallRecord(
        id=tool_call.id,
        sdk_call_id=tool_call.sdk_call_id,
        run_id=tool_call.run_id,
        agent_step_id=tool_call.agent_step_id,
        tool_id=tool_call.tool_id,
        skill_id=tool_call.skill_id,
        arguments_json=tool_call.arguments,
        approval_status=tool_call.approval_status.value,
        execution_id=tool_call.execution_id,
        created_at=tool_call.created_at,
    )


def tool_call_from_record(record: ToolCallRecord) -> ToolCall:
    return ToolCall(
        id=record.id,
        sdk_call_id=record.sdk_call_id or record.id,
        run_id=record.run_id,
        agent_step_id=record.agent_step_id,
        tool_id=record.tool_id,
        skill_id=record.skill_id,
        arguments=record.arguments_json,
        approval_status=ApprovalStatus(record.approval_status),
        execution_id=record.execution_id,
        created_at=record.created_at,
    )


def approval_to_record(approval: Approval) -> ApprovalRecord:
    return ApprovalRecord(
        id=approval.id,
        run_id=approval.run_id,
        tool_call_id=approval.tool_call_id,
        status=approval.status.value,
        tool_name=approval.tool_name,
        command_json=approval.command,
        cwd=approval.cwd,
        target_summary=approval.target_summary,
        env_diff_json=approval.env_diff,
        reason=approval.reason,
        decision=approval.decision.value if approval.decision is not None else None,
        decision_feedback=approval.decision_feedback,
        decided_by=approval.decided_by,
        created_at=approval.created_at,
        decided_at=approval.decided_at,
    )


def apply_approval_to_record(approval: Approval, record: ApprovalRecord) -> None:
    record.status = approval.status.value
    record.reason = approval.reason
    record.decision = approval.decision.value if approval.decision is not None else None
    record.decision_feedback = approval.decision_feedback
    record.decided_by = approval.decided_by
    record.decided_at = approval.decided_at


def approval_from_record(record: ApprovalRecord) -> Approval:
    return Approval(
        id=record.id,
        run_id=record.run_id,
        tool_call_id=record.tool_call_id,
        status=ApprovalStatus(record.status),
        tool_name=record.tool_name,
        command=record.command_json or [],
        cwd=record.cwd,
        target_summary=record.target_summary,
        env_diff=record.env_diff_json or {},
        reason=record.reason,
        decision=ApprovalDecision(record.decision) if record.decision is not None else None,
        decision_feedback=record.decision_feedback,
        decided_by=record.decided_by,
        created_at=record.created_at,
        decided_at=record.decided_at,
    )


def approval_grant_to_record(grant: ApprovalGrant) -> ApprovalGrantRecord:
    return ApprovalGrantRecord(
        id=grant.id,
        run_id=grant.run_id,
        tool_id=grant.tool_id,
        created_by=grant.created_by,
        created_at=grant.created_at,
    )


def approval_grant_from_record(record: ApprovalGrantRecord) -> ApprovalGrant:
    return ApprovalGrant(
        id=record.id,
        run_id=record.run_id,
        tool_id=record.tool_id,
        created_by=record.created_by,
        created_at=record.created_at,
    )


def terminal_to_record(terminal: TerminalSession) -> TerminalSessionRecord:
    return TerminalSessionRecord(
        id=terminal.id,
        run_id=terminal.run_id,
        execution_id=terminal.execution_id,
        runner_id=terminal.runner_id,
        shell=terminal.shell,
        cwd=terminal.cwd,
        status=terminal.status.value,
        owner=terminal.owner.value,
        cols=terminal.cols,
        rows=terminal.rows,
        output_cursor=terminal.output_cursor,
        takeover_cursor=terminal.takeover_cursor,
        takeover_started_at=terminal.takeover_started_at,
        transcript_artifact_id=terminal.transcript_artifact_id,
        created_at=terminal.created_at,
        closed_at=terminal.closed_at,
    )


def apply_terminal_to_record(
    terminal: TerminalSession,
    record: TerminalSessionRecord,
) -> None:
    record.status = terminal.status.value
    record.owner = terminal.owner.value
    record.cols = terminal.cols
    record.rows = terminal.rows
    record.output_cursor = terminal.output_cursor
    record.takeover_cursor = terminal.takeover_cursor
    record.takeover_started_at = terminal.takeover_started_at
    record.transcript_artifact_id = terminal.transcript_artifact_id
    record.closed_at = terminal.closed_at


def terminal_from_record(record: TerminalSessionRecord) -> TerminalSession:
    return TerminalSession(
        id=record.id,
        run_id=record.run_id,
        execution_id=record.execution_id,
        runner_id=record.runner_id,
        shell=record.shell,
        cwd=record.cwd,
        status=TerminalStatus(record.status),
        owner=TerminalOwner(record.owner),
        cols=record.cols,
        rows=record.rows,
        output_cursor=record.output_cursor,
        takeover_cursor=record.takeover_cursor,
        takeover_started_at=record.takeover_started_at,
        transcript_artifact_id=record.transcript_artifact_id,
        created_at=record.created_at,
        closed_at=record.closed_at,
    )


def report_to_record(report: Report) -> ReportRecord:
    return ReportRecord(
        id=report.id,
        run_id=report.run_id,
        format=report.format.value,
        artifact_id=report.artifact_id,
        finding_ids_json=report.finding_ids,
        created_at=report.created_at,
    )


def report_from_record(record: ReportRecord) -> Report:
    return Report(
        id=record.id,
        run_id=record.run_id,
        format=ReportFormat(record.format),
        artifact_id=record.artifact_id,
        finding_ids=record.finding_ids_json or [],
        created_at=record.created_at,
    )
