use crate::gateway_state::GatewayState;
use crate::gateway_state::PendingApproval;
use crate::gateway_state::PendingApprovalKind;
use codex_riftx_app_server_adapter::PendingCommandApproval;
use codex_riftx_app_server_adapter::PendingDynamicToolCall;
use codex_riftx_app_server_adapter::RiftxAppServerEvent;
use codex_riftx_app_server_adapter::StructuredToolRequest;
use codex_riftx_core::ApprovalMode;
use codex_riftx_core::EffectivePolicy;
use serde_json::Value;
use serde_json::json;

pub(crate) async fn process(state: &GatewayState, event: RiftxAppServerEvent) {
    match event {
        RiftxAppServerEvent::CommandApproval(pending) => command_approval(state, pending).await,
        RiftxAppServerEvent::FileChangeApproval(pending) => {
            if let Some(app_server) = &state.app_server {
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
            if let Some(app_server) = &state.app_server {
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
        RiftxAppServerEvent::DynamicToolCall(pending) => dynamic_tool(state, pending).await,
        other => forward_event(state, other).await,
    }
}

async fn command_approval(state: &GatewayState, pending: PendingCommandApproval) {
    let Some(engagement_id) = engagement_for_thread(state, &pending.params.thread_id).await else {
        if let Some(app_server) = &state.app_server {
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
        PendingApproval {
            engagement_id: engagement_id.clone(),
            policy_revision: engagement.policy_revision,
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

async fn dynamic_tool(state: &GatewayState, pending: PendingDynamicToolCall) {
    let Some(engagement_id) = engagement_for_thread(state, &pending.params.thread_id).await else {
        fail_dynamic(state, pending, "thread is not bound to an engagement").await;
        return;
    };
    let result = prepare_dynamic(state, &engagement_id, &pending).await;
    let (request, environment_id, policy, approval_mode) = match result {
        Ok(prepared) => prepared,
        Err(message) => {
            fail_dynamic(state, pending, &message).await;
            return;
        }
    };
    let needs_approval = match approval_mode {
        ApprovalMode::Always => true,
        ApprovalMode::HighRisk => matches!(
            request,
            StructuredToolRequest::Nuclei(_) | StructuredToolRequest::Ffuf(_)
        ),
        ApprovalMode::Never => false,
    };
    if needs_approval {
        let approval_id = pending.approval_id();
        state.pending_approvals.write().await.insert(
            approval_id.clone(),
            PendingApproval {
                engagement_id: engagement_id.clone(),
                policy_revision: policy.revision.clone(),
                kind: PendingApprovalKind::Dynamic {
                    pending: pending.clone(),
                    request: request.clone(),
                    environment_id,
                },
            },
        );
        state
            .publish(
                &engagement_id,
                "approval/dynamicTool",
                json!({
                    "approvalId": approval_id,
                    "toolCallId": pending.params.call_id,
                    "tool": request.name(),
                    "targets": request.targets(),
                    "policyRevision": policy.revision,
                }),
            )
            .await;
        return;
    }
    spawn_dynamic_execution(
        state.clone(),
        engagement_id,
        pending,
        request,
        environment_id,
    );
}

pub(crate) fn spawn_dynamic_execution(
    state: GatewayState,
    engagement_id: String,
    pending: PendingDynamicToolCall,
    request: StructuredToolRequest,
    environment_id: String,
) {
    tokio::spawn(async move {
        let Some(app_server) = &state.app_server else {
            return;
        };
        state
            .publish(
                &engagement_id,
                "tool/started",
                json!({"toolCallId": pending.params.call_id, "tool": request.name()}),
            )
            .await;
        match app_server
            .execute_structured_tool(&environment_id, request.clone())
            .await
        {
            Ok(output) => {
                let _ = app_server
                    .complete_dynamic_tool_call(pending.clone(), Ok(&output))
                    .await;
                let _ =
                    crate::tool_results::persist(&state.store, &engagement_id, &request, &output)
                        .await;
                state
                    .publish(
                        &engagement_id,
                        "tool/completed",
                        json!({
                            "toolCallId": pending.params.call_id,
                            "tool": output.tool,
                            "exitCode": output.exit_code,
                        }),
                    )
                    .await;
            }
            Err(error) => {
                let message = error.to_string();
                let _ = app_server
                    .complete_dynamic_tool_call(pending.clone(), Err(&message))
                    .await;
                state
                    .publish(
                        &engagement_id,
                        "tool/failed",
                        json!({"toolCallId": pending.params.call_id, "message": message}),
                    )
                    .await;
            }
        }
    });
}

async fn prepare_dynamic(
    state: &GatewayState,
    engagement_id: &str,
    pending: &PendingDynamicToolCall,
) -> Result<(StructuredToolRequest, String, EffectivePolicy, ApprovalMode), String> {
    let engagement = state
        .store
        .engagement(engagement_id)
        .await
        .map_err(|error| error.to_string())?;
    let profile = state
        .config
        .tool_profiles
        .get(&engagement.tool_profile)
        .ok_or_else(|| "engagement tool profile no longer exists".to_string())?;
    let policy = EffectivePolicy::resolve(&state.config.policy, &engagement.scope, profile, None)
        .map_err(|error| error.to_string())?;
    if policy.revision != engagement.policy_revision {
        return Err("engagement policy revision is stale".to_string());
    }
    let request =
        StructuredToolRequest::parse(&pending.params.tool, pending.params.arguments.clone())
            .map_err(|error| error.to_string())?;
    if !policy.allows_tool(request.name()) {
        return Err(format!(
            "tool {} is not allowed by the effective profile",
            request.name()
        ));
    }
    for target in request.targets() {
        policy
            .check_target(target)
            .map_err(|error| error.to_string())?;
    }
    let sandbox_id = engagement
        .sandbox_id
        .ok_or_else(|| "engagement has no active sandbox".to_string())?;
    let sandbox = state
        .manager
        .sandbox(&sandbox_id)
        .await
        .map_err(|error| error.to_string())?;
    Ok((request, sandbox.environment_id, policy, profile.approval))
}

async fn fail_dynamic(state: &GatewayState, pending: PendingDynamicToolCall, message: &str) {
    if let Some(app_server) = &state.app_server {
        let _ = app_server
            .complete_dynamic_tool_call(pending.clone(), Err(message))
            .await;
    }
    publish_pending(
        state,
        "tool/rejected",
        pending.params.thread_id,
        json!({"toolCallId": pending.params.call_id, "message": message}),
    )
    .await;
}

async fn forward_event(state: &GatewayState, event: RiftxAppServerEvent) {
    let Ok(event) = event.envelope() else {
        return;
    };
    let Some(thread_id) = event.thread_id.as_deref() else {
        state.publish_to_active(&event.kind, event.data).await;
        return;
    };
    let Some(engagement_id) = engagement_for_thread(state, thread_id).await else {
        return;
    };
    if event.kind == "turn/completed"
        && let Some(turn_id) = event.turn_id.as_deref()
    {
        state
            .complete_task(&engagement_id, turn_id, &event.data)
            .await;
        state.active_turns.write().await.remove(&engagement_id);
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
