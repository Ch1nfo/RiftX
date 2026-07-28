use crate::gateway_state::GatewayState;
use crate::gateway_state::PendingApprovalRequest;
use codex_riftx_core::ApprovalActor;
use codex_riftx_core::ApprovalDecisionReason;
use codex_riftx_core::ApprovalOutcome;
use codex_riftx_core::ApprovalRecord;
use codex_riftx_core::ApprovalRequestKind;
use codex_riftx_core::RecordedApprovalDecision;
use codex_riftx_core::StateError;
use codex_riftx_ipc::ApprovalDecision;
use codex_riftx_ipc::ApprovalKind;
use codex_riftx_ipc::PendingApproval;

const MAX_DISPLAY_ARGUMENTS: usize = 64;
const MAX_DISPLAY_ARGUMENT_CHARS: usize = 256;
const MAX_EXECUTABLE_NAMES: usize = 32;
const MAX_PATH_CHARS: usize = 1_024;

pub(crate) async fn queue(
    state: &GatewayState,
    pending: PendingApprovalRequest,
) -> Result<(), StateError> {
    state
        .store
        .put_approval(&record_from_view(&pending.view))
        .await?;
    state
        .pending_approvals
        .write()
        .await
        .insert(pending.view.id.clone(), pending);
    Ok(())
}

pub(crate) async fn record_operator_decision(
    state: &GatewayState,
    view: &PendingApproval,
    decision: ApprovalDecision,
    outcome: ApprovalOutcome,
    reason: ApprovalDecisionReason,
    decided_at: i64,
) -> Result<(), StateError> {
    let mut record = state
        .store
        .approval(&view.engagement_id, &view.id)
        .await?
        .unwrap_or_else(|| record_from_view(view));
    record.requested_decision = Some(match decision {
        ApprovalDecision::Approve => RecordedApprovalDecision::Approve,
        ApprovalDecision::Deny => RecordedApprovalDecision::Deny,
    });
    record.outcome = outcome;
    record.actor = Some(ApprovalActor::LocalOperator);
    record.decision_reason = Some(reason);
    record.decided_at = Some(decided_at);
    state.store.put_approval(&record).await
}

pub(crate) async fn cancel_stale_after_restart(
    state: &GatewayState,
    engagement_id: &str,
    decided_at: i64,
) -> Result<(), StateError> {
    for mut record in state.store.approvals(engagement_id).await? {
        if record.outcome != ApprovalOutcome::Pending {
            continue;
        }
        record.outcome = ApprovalOutcome::Cancelled;
        record.actor = Some(ApprovalActor::System);
        record.decision_reason = Some(ApprovalDecisionReason::DaemonRestart);
        record.decided_at = Some(decided_at);
        state.store.put_approval(&record).await?;
    }
    Ok(())
}

pub(crate) async fn record_system_cancellation(
    state: &GatewayState,
    view: &PendingApproval,
    reason: ApprovalDecisionReason,
    decided_at: i64,
) -> Result<(), StateError> {
    let mut record = state
        .store
        .approval(&view.engagement_id, &view.id)
        .await?
        .unwrap_or_else(|| record_from_view(view));
    record.outcome = ApprovalOutcome::Cancelled;
    record.actor = Some(ApprovalActor::System);
    record.decision_reason = Some(reason);
    record.decided_at = Some(decided_at);
    state.store.put_approval(&record).await
}

fn record_from_view(view: &PendingApproval) -> ApprovalRecord {
    let intent = view.execution_intent.as_ref();
    ApprovalRecord {
        id: view.id.clone(),
        engagement_id: view.engagement_id.clone(),
        kind: match view.kind {
            ApprovalKind::Command => ApprovalRequestKind::Command,
            ApprovalKind::Tool => ApprovalRequestKind::Tool,
        },
        requested_at: view.requested_at,
        decided_at: None,
        requested_decision: None,
        outcome: ApprovalOutcome::Pending,
        actor: None,
        decision_reason: None,
        policy_revision: view.policy_revision.clone(),
        execution_binding_sha256: intent
            .map(|intent| intent.binding_sha256.clone())
            .unwrap_or_default(),
        command_sha256: intent
            .map(|intent| intent.command_sha256.clone())
            .unwrap_or_default(),
        argument_sha256: intent
            .map(|intent| intent.argument_sha256.clone())
            .unwrap_or_default(),
        display_argv: intent
            .map(|intent| {
                intent
                    .display_argv
                    .iter()
                    .take(MAX_DISPLAY_ARGUMENTS)
                    .map(|argument| truncate(argument, MAX_DISPLAY_ARGUMENT_CHARS))
                    .collect()
            })
            .unwrap_or_default(),
        cwd: intent
            .map(|intent| truncate(&intent.cwd.display().to_string(), MAX_PATH_CHARS))
            .or_else(|| view.cwd.as_deref().map(|cwd| truncate(cwd, MAX_PATH_CHARS))),
        executable_names: intent
            .map(|intent| {
                intent
                    .executables
                    .iter()
                    .take(MAX_EXECUTABLE_NAMES)
                    .map(|executable| {
                        truncate(&executable.requested_name, MAX_DISPLAY_ARGUMENT_CHARS)
                    })
                    .collect()
            })
            .unwrap_or_default(),
    }
}

fn truncate(value: &str, max_chars: usize) -> String {
    value.chars().take(max_chars).collect()
}
