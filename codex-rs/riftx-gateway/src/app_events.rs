use crate::gateway_state::GatewayState;
use crate::gateway_state::PendingApprovalKind;
use crate::gateway_state::PendingApprovalRequest;
use crate::gateway_state::unix_timestamp;
use codex_riftx_app_server_adapter::PendingCommandApproval;
use codex_riftx_app_server_adapter::PendingDynamicToolCall;
use codex_riftx_app_server_adapter::RIFTX_CREDENTIAL_TOOL_NAME;
use codex_riftx_app_server_adapter::RiftxAppServerEvent;
use codex_riftx_core::Engagement;
use codex_riftx_core::ExecutionStatus;
use codex_riftx_execution_policy::CommandIntentInput;
use codex_riftx_execution_policy::CommandSpec;
use codex_riftx_execution_policy::DecisionContext;
use codex_riftx_execution_policy::ExecutionDisposition;
use codex_riftx_execution_policy::ExecutionIntent;
use codex_riftx_execution_policy::decide;
use codex_riftx_ipc::ApprovalKind;
use codex_riftx_ipc::PendingApproval;
use serde_json::Value;
use serde_json::json;
use std::path::PathBuf;
use uuid::Uuid;

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
            dynamic_tool(state, profile_name, pending).await
        }
        other => forward_event(state, profile_name, other).await,
    }
}

async fn dynamic_tool(state: &GatewayState, profile_name: &str, pending: PendingDynamicToolCall) {
    let Some(app_server) = state.app_server(profile_name) else {
        return;
    };
    if pending.params.namespace.is_some() || pending.params.tool != RIFTX_CREDENTIAL_TOOL_NAME {
        let _ = app_server
            .reject_dynamic_tool(
                pending,
                "RiftX rejected an unknown dynamic tool".to_string(),
            )
            .await;
        return;
    }
    let Some(engagement_id) = engagement_for_thread(state, &pending.params.thread_id).await else {
        let _ = app_server
            .reject_dynamic_tool(
                pending,
                "RiftX could not bind the tool call to an engagement".to_string(),
            )
            .await;
        return;
    };
    let Ok(engagement) = state.store.engagement(&engagement_id).await else {
        let _ = app_server
            .reject_dynamic_tool(
                pending,
                "RiftX could not load the engagement for this tool call".to_string(),
            )
            .await;
        return;
    };
    let params = match serde_json::from_value::<codex_riftx_ipc::CredentialExecutionParams>(
        pending.params.arguments.clone(),
    ) {
        Ok(params) => params,
        Err(error) => {
            let _ = app_server
                .resolve_dynamic_tool_text(
                    pending,
                    format!("RiftX rejected invalid credential tool arguments: {error}"),
                    false,
                )
                .await;
            return;
        }
    };
    let mut origin = crate::credential_execution::CredentialExecutionOrigin::DynamicTool {
        thread_id: pending.params.thread_id.clone(),
        tool_call_id: pending.params.call_id.clone(),
        turn_id: pending.params.turn_id.clone(),
        approved_binding: None,
    };
    let intent = match crate::credential_execution::credential_execution_intent(
        state,
        &engagement,
        &params,
        &origin,
        "preview",
    ) {
        Ok(intent) => intent,
        Err(error) => {
            let _ = app_server
                .resolve_dynamic_tool_text(pending, error.to_string(), false)
                .await;
            return;
        }
    };
    let decision = decide(
        &intent,
        DecisionContext {
            now: unix_timestamp(),
            authorized_capabilities: &engagement.authorization.capabilities,
        },
    );
    if decision.disposition == ExecutionDisposition::Deny {
        let _ = app_server
            .resolve_dynamic_tool_text(
                pending,
                format!("RiftX denied credential execution: {:?}", decision.reasons),
                false,
            )
            .await;
        return;
    }
    if decision.disposition == ExecutionDisposition::RequireApproval {
        if engagement.mode == codex_riftx_core::ExecutionMode::Auto {
            let _ = crate::auto_run::needs_input(
                state,
                &engagement,
                "Credential execution needs authorization clarification before Auto can continue",
            )
            .await;
            let _ = app_server
                .resolve_dynamic_tool_text(
                    pending,
                    "RiftX paused Auto because this credential execution needs operator input"
                        .to_string(),
                    false,
                )
                .await;
            return;
        }
        if !await_execution_approval(
            state,
            profile_name,
            &engagement_id,
            &engagement.policy_revision,
            &intent,
        )
        .await
        {
            let _ = app_server
                .resolve_dynamic_tool_text(
                    pending,
                    "RiftX denied credential execution".to_string(),
                    false,
                )
                .await;
            return;
        }
        if let crate::credential_execution::CredentialExecutionOrigin::DynamicTool {
            approved_binding,
            ..
        } = &mut origin
        {
            *approved_binding = Some(intent.binding_sha256.clone());
        }
    }
    if !crate::auto_run::record_tool_call(state, &engagement)
        .await
        .unwrap_or(false)
    {
        let _ = app_server
            .resolve_dynamic_tool_text(
                pending,
                "RiftX denied the tool call because the Auto tool budget is exhausted".to_string(),
                false,
            )
            .await;
        return;
    }
    state
        .publish(
            &engagement_id,
            "tool/credentialRequested",
            json!({
                "callId": &pending.params.call_id,
                "turnId": &pending.params.turn_id,
                "tool": &params.tool,
                "target": &params.target,
            }),
        )
        .await;
    let result =
        crate::credential_execution::execute_inner(state, engagement_id.clone(), params, origin)
            .await;
    let (text, success) = match result {
        Ok(response) => (crate::credential_execution::model_output(&response), true),
        Err(error) => {
            state
                .publish(
                    &engagement_id,
                    "tool/credentialRejected",
                    json!({
                        "callId": &pending.params.call_id,
                        "turnId": &pending.params.turn_id,
                        "error": error.to_string(),
                    }),
                )
                .await;
            (format!("RiftX credential tool failed: {error}"), false)
        }
    };
    let _ = app_server
        .resolve_dynamic_tool_text(pending, text, success)
        .await;
}

async fn await_execution_approval(
    state: &GatewayState,
    profile_name: &str,
    engagement_id: &str,
    policy_revision: &str,
    intent: &ExecutionIntent,
) -> bool {
    let approval_id = Uuid::new_v4().to_string();
    let (decision_tx, decision_rx) = tokio::sync::oneshot::channel();
    let command = (!intent.display_argv.is_empty()).then(|| intent.display_argv.join(" "));
    state.pending_approvals.write().await.insert(
        approval_id.clone(),
        PendingApprovalRequest {
            profile_name: profile_name.to_string(),
            engagement_id: engagement_id.to_string(),
            view: PendingApproval {
                id: approval_id.clone(),
                engagement_id: engagement_id.to_string(),
                policy_revision: policy_revision.to_string(),
                kind: ApprovalKind::Tool,
                requested_at: unix_timestamp(),
                command,
                cwd: Some(intent.cwd.display().to_string()),
                reason: Some(format!(
                    "{} mode requires approval for {:?}-risk credential execution",
                    format!("{:?}", intent.mode).to_ascii_lowercase(),
                    intent.risk
                )),
                execution_intent: Some(intent.clone()),
            },
            kind: PendingApprovalKind::Tool { decision_tx },
        },
    );
    state
        .publish(
            engagement_id,
            "approval/tool",
            json!({"approvalId": approval_id, "executionIntent": intent}),
        )
        .await;
    decision_rx.await.unwrap_or(false)
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
    let intent = command_execution_intent(state, &engagement, &pending);
    let decision = decide(
        &intent,
        DecisionContext {
            now: unix_timestamp(),
            authorized_capabilities: &engagement.authorization.capabilities,
        },
    );
    match decision.disposition {
        ExecutionDisposition::Deny => {
            if let Some(app_server) = state.app_server(profile_name) {
                let _ = app_server
                    .decide_command_approval(
                        pending.clone(),
                        codex_riftx_app_server_adapter::OperatorApprovalDecision::Deny,
                    )
                    .await;
            }
            state
                .publish(
                    &engagement_id,
                    "approval/commandDenied",
                    json!({"decision": decision, "intent": intent}),
                )
                .await;
            return;
        }
        ExecutionDisposition::Allow => {
            if !crate::auto_run::record_tool_call(state, &engagement)
                .await
                .unwrap_or(false)
            {
                if let Some(app_server) = state.app_server(profile_name) {
                    let _ = app_server
                        .decide_command_approval(
                            pending,
                            codex_riftx_app_server_adapter::OperatorApprovalDecision::Deny,
                        )
                        .await;
                }
                state
                    .publish(
                        &engagement_id,
                        "approval/commandDenied",
                        json!({"reason": "autoToolBudgetExhausted", "intent": intent}),
                    )
                    .await;
                return;
            }
            if state
                .publish_critical(
                    &engagement,
                    "approval/commandAllowed",
                    json!({"decision": decision, "intent": intent}),
                )
                .await
                .is_err()
            {
                let _ = crate::auto_run::lifecycle_stop(
                    state,
                    &engagement_id,
                    crate::auto_run::AutoLifecycleStop::AuditUnavailable,
                )
                .await;
                if let Some(app_server) = state.app_server(profile_name) {
                    let _ = app_server
                        .decide_command_approval(
                            pending,
                            codex_riftx_app_server_adapter::OperatorApprovalDecision::Deny,
                        )
                        .await;
                }
                return;
            }
            if let Some(app_server) = state.app_server(profile_name) {
                let _ = app_server
                    .decide_command_approval(
                        pending,
                        codex_riftx_app_server_adapter::OperatorApprovalDecision::Approve,
                    )
                    .await;
            }
            return;
        }
        ExecutionDisposition::RequireApproval => {}
    }
    if engagement.mode == codex_riftx_core::ExecutionMode::Auto {
        let _ = crate::auto_run::needs_input(
            state,
            &engagement,
            "Command risk or scope is ambiguous; clarify authorization before Auto continues",
        )
        .await;
        if let Some(app_server) = state.app_server(profile_name) {
            let _ = app_server
                .decide_command_approval(
                    pending,
                    codex_riftx_app_server_adapter::OperatorApprovalDecision::Deny,
                )
                .await;
        }
        state
            .publish(
                &engagement_id,
                "approval/commandDenied",
                json!({"reason": "autoNeedsInput", "intent": intent}),
            )
            .await;
        return;
    }
    let approval_id = pending.approval_id();
    let display_command = (!intent.display_argv.is_empty()).then(|| intent.display_argv.join(" "));
    let event_intent = intent.clone();
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
                command: display_command,
                cwd: pending.params.cwd.as_ref().map(ToString::to_string),
                reason: pending.params.reason.clone(),
                execution_intent: Some(intent),
            },
            kind: PendingApprovalKind::Command(Box::new(pending.clone())),
        },
    );
    state
        .publish(
            &engagement_id,
            "approval/command",
            json!({"approvalId": approval_id, "executionIntent": event_intent}),
        )
        .await;
}

pub(crate) fn command_execution_intent(
    state: &GatewayState,
    engagement: &Engagement,
    pending: &PendingCommandApproval,
) -> ExecutionIntent {
    let cwd = pending
        .params
        .cwd
        .as_ref()
        .map(ToString::to_string)
        .map(PathBuf::from)
        .unwrap_or_else(|| state.config.daemon.workspace_root.join(&engagement.id));
    ExecutionIntent::from_command(CommandIntentInput {
        engagement_id: &engagement.id,
        thread_id: &pending.params.thread_id,
        turn_id: &pending.params.turn_id,
        tool_call_id: &pending.params.item_id,
        mode: engagement.mode,
        command: CommandSpec::CommandLine(pending.params.command.as_deref().unwrap_or_default()),
        cwd: &cwd,
        search_path: &state.tool_search_path,
        inventory: &state.tools,
        requested_capabilities: &[],
        authorization_deadline: engagement.authorization.window.expires_at,
        policy_revision: &engagement.policy_revision,
    })
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
    let auto_completion_data = (event.kind == "turn/completed").then(|| event.data.clone());
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
        crate::artifacts::capture_pending(state.clone(), engagement_id.clone()).await;
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
    if let Some(data) = auto_completion_data {
        crate::auto_run::on_turn_completed(state, &engagement_id, &data).await;
    }
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
