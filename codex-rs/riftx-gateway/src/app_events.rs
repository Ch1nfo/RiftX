use crate::gateway_state::GatewayState;
use crate::gateway_state::PendingApprovalKind;
use crate::gateway_state::PendingApprovalRequest;
use codex_riftx_app_server_adapter::PendingCommandApproval;
use codex_riftx_app_server_adapter::RiftxAppServerEvent;
use codex_riftx_core::ExecutionStatus;
use codex_riftx_ipc::ApprovalKind;
use codex_riftx_ipc::PendingApproval;
use serde_json::Value;
use serde_json::json;

pub(crate) async fn process(state: &GatewayState, profile_name: &str, event: RiftxAppServerEvent) {
    match event {
        RiftxAppServerEvent::Notification(notification) => {
            crate::execution_events::process_notification(state, &notification).await;
            crate::conversation::process_notification(state, &notification).await;
            forward_event(
                state,
                profile_name,
                RiftxAppServerEvent::Notification(notification),
            )
            .await;
        }
        RiftxAppServerEvent::CommandApproval(pending) => {
            command_approval(state, profile_name, pending).await
        }
        RiftxAppServerEvent::FileChangeApproval(pending) => {
            if let Some(app_server) = state.app_server(profile_name) {
                let _ = app_server.deny_file_change(pending.clone()).await;
            }
            publish_pending(
                state,
                "approval/fileChangeDenied",
                pending.params.thread_id.clone(),
                json!({
                    "approvalId": pending.approval_id(),
                    "reason": "RiftX denies agent file-change escalation"
                }),
            )
            .await;
        }
        RiftxAppServerEvent::PermissionsApproval(pending) => {
            if let Some(app_server) = state.app_server(profile_name) {
                let _ = app_server
                    .reject_permissions(
                        pending.clone(),
                        "RiftX denies permission profile escalation".to_string(),
                    )
                    .await;
            }
            publish_pending(
                state,
                "approval/permissionsDenied",
                pending.params.thread_id.clone(),
                json!({
                    "approvalId": pending.approval_id(),
                    "reason": "RiftX denies permission profile escalation"
                }),
            )
            .await;
        }
        RiftxAppServerEvent::DynamicToolCall(pending) => {
            if let Some(app_server) = state.app_server(profile_name) {
                let _ = app_server
                    .reject_dynamic_tool(
                        pending,
                        "RiftX does not expose fixed dynamic tools; use local shell tools"
                            .to_string(),
                    )
                    .await;
            }
        }
        other => forward_event(state, profile_name, other).await,
    }
}

async fn command_approval(
    state: &GatewayState,
    profile_name: &str,
    pending: PendingCommandApproval,
) {
    let Some(engagement_id) = engagement_for_thread(state, &pending.params.thread_id).await else {
        if let Some(app_server) = state.app_server(profile_name) {
            let _ = app_server
                .decide_command_approval(
                    pending,
                    codex_riftx_app_server_adapter::OperatorApprovalDecision::Deny,
                )
                .await;
        }
        return;
    };
    let Ok(engagement) = state.store.engagement(&engagement_id).await else {
        return;
    };
    let approval_id = pending.approval_id();
    state.pending_approvals.write().await.insert(
        approval_id.clone(),
        PendingApprovalRequest {
            profile_name: profile_name.to_string(),
            engagement_id: engagement_id.clone(),
            view: PendingApproval {
                id: approval_id.clone(),
                engagement_id: engagement_id.clone(),
                policy_revision: engagement.policy_revision,
                kind: ApprovalKind::Command,
                requested_at: pending.params.started_at_ms / 1_000,
                command: pending.params.command.clone(),
                cwd: pending.params.cwd.as_ref().map(ToString::to_string),
                reason: pending.params.reason.clone(),
            },
            kind: PendingApprovalKind::Command(pending.clone()),
        },
    );
    state
        .publish(
            &engagement_id,
            "approval/command",
            json!({"approvalId": approval_id, "payload": pending.params}),
        )
        .await;
}

async fn forward_event(state: &GatewayState, profile_name: &str, event: RiftxAppServerEvent) {
    let Ok(event) = event.envelope() else {
        return;
    };
    let Some(thread_id) = event.thread_id.as_deref() else {
        state
            .publish_to_profile_active(profile_name, &event.kind, event.data)
            .await;
        return;
    };
    let Some(engagement_id) = engagement_for_thread(state, thread_id).await else {
        return;
    };
    if event.kind == "turn/completed"
        && let Some(turn_id) = event.turn_id.as_deref()
    {
        let status = match event.data.pointer("/turn/status").and_then(Value::as_str) {
            Some("interrupted") => ExecutionStatus::Interrupted,
            _ => ExecutionStatus::Failed,
        };
        crate::execution_events::finish_turn(state, &engagement_id, turn_id, status).await;
        state
            .complete_task(&engagement_id, turn_id, &event.data)
            .await;
        state.active_turns.write().await.remove(&engagement_id);
        state.take_pending_approvals(&engagement_id).await;
        tokio::spawn(crate::artifacts::capture_pending(
            state.clone(),
            engagement_id.clone(),
        ));
    }
    state
        .publish(
            &engagement_id,
            &event.kind,
            json!({
                "requestId": event.request_id,
                "turnId": event.turn_id,
                "payload": event.data,
            }),
        )
        .await;
}

async fn publish_pending(state: &GatewayState, kind: &str, thread_id: String, data: Value) {
    if let Some(engagement_id) = engagement_for_thread(state, &thread_id).await {
        state.publish(&engagement_id, kind, data).await;
    }
}

async fn engagement_for_thread(state: &GatewayState, thread_id: &str) -> Option<String> {
    state
        .thread_engagements
        .read()
        .await
        .get(thread_id)
        .cloned()
}
