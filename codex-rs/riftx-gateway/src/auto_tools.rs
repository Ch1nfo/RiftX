use crate::engagement_stop::AgentThreadDisposition;
use crate::execution_events::ExecutionKey;
use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use codex_riftx_core::AutoRunState;
use codex_riftx_core::Evidence;
use codex_riftx_core::EvidencePurpose;
use codex_riftx_core::Execution;
use codex_riftx_core::ExecutionStatus;
use serde_json::json;
use std::time::Duration;
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

const MAX_UNAVAILABLE_TOOLS: usize = 16;
const MAX_TOOL_NAME_BYTES: usize = 128;

pub(crate) async fn record_unavailable_tool(state: &GatewayState, execution: &Execution) {
    if execution.status != ExecutionStatus::Failed {
        return;
    }
    let Some(tool) = execution
        .tool
        .as_ref()
        .filter(|tool| tool.resolved_path.is_none())
    else {
        return;
    };
    record_unavailable_name(
        state,
        &execution.engagement_id,
        &tool.requested_name,
        Some(&execution.id),
    )
    .await;
}

pub(crate) async fn record_unavailable_name(
    state: &GatewayState,
    engagement_id: &str,
    requested_name: &str,
    execution_id: Option<&str>,
) {
    let tool_name = truncate_utf8(requested_name, MAX_TOOL_NAME_BYTES).to_string();
    let Ok(Some(mut run)) = state.store.auto_run(engagement_id).await else {
        return;
    };
    if !matches!(run.state, AutoRunState::Running | AutoRunState::Evaluating)
        || run.unavailable_tools.contains(&tool_name)
    {
        return;
    }
    while run.unavailable_tools.len() >= MAX_UNAVAILABLE_TOOLS {
        run.unavailable_tools.remove(0);
    }
    run.unavailable_tools.push(tool_name.clone());
    run.updated_at = unix_timestamp();
    if let Err(error) = state.store.put_auto_run(&run).await {
        state
            .emit_event(
                engagement_id,
                "auto/controllerError",
                json!({"message": error.to_string()}),
            )
            .await;
        return;
    }
    state
        .publish(
            engagement_id,
            "auto/toolUnavailable",
            json!({
                "executionId": execution_id,
                "toolName": tool_name,
                "action": "replan",
            }),
        )
        .await;
}

pub(crate) async fn watch_execution(
    state: GatewayState,
    key: ExecutionKey,
    execution: Execution,
    cancellation: CancellationToken,
) {
    let Ok(Some(run)) = state.store.auto_run(&execution.engagement_id).await else {
        return;
    };
    if run.state != AutoRunState::Running {
        return;
    }
    let timeout = Duration::from_secs(run.config.limits.max_single_command_seconds);
    tokio::spawn(async move {
        tokio::select! {
            () = cancellation.cancelled() => {}
            () = tokio::time::sleep(timeout) => {
                handle_timeout(&state, &key, &execution).await;
            }
        }
    });
}

async fn handle_timeout(state: &GatewayState, key: &ExecutionKey, execution: &Execution) {
    let Ok(_turn_permit) = state.turn_slot.clone().acquire_owned().await else {
        return;
    };
    if !state.active_executions.read().await.contains_key(key) {
        return;
    }
    let Some(active_turn) = state
        .active_turns
        .read()
        .await
        .get(&execution.engagement_id)
        .cloned()
    else {
        return;
    };
    if active_turn.turn_id != execution.turn_id {
        return;
    }
    let Ok(Some(run)) = state.store.auto_run(&execution.engagement_id).await else {
        return;
    };
    if run.state != AutoRunState::Running {
        return;
    }
    let Ok(engagement) = state.store.engagement(&execution.engagement_id).await else {
        return;
    };
    let tool_name = execution
        .tool
        .as_ref()
        .map(|tool| truncate_utf8(&tool.requested_name, MAX_TOOL_NAME_BYTES))
        .unwrap_or("<unparsed>");
    let timeout_seconds = run.config.limits.max_single_command_seconds;
    if state
        .publish_critical(
            &engagement,
            "auto/toolTimedOut",
            json!({
                "executionId": execution.id,
                "turnId": execution.turn_id,
                "toolName": tool_name,
                "timeoutSeconds": timeout_seconds,
                "outcome": "failure",
            }),
        )
        .await
        .is_err()
    {
        state
            .stop_engagement_work(&execution.engagement_id, AgentThreadDisposition::Preserve)
            .await;
        let _ = crate::auto_run::lifecycle_stop(
            state,
            &execution.engagement_id,
            crate::auto_run::AutoLifecycleStop::AuditUnavailable,
        )
        .await;
        return;
    }

    let evidence = Evidence {
        id: Uuid::new_v4().to_string(),
        engagement_id: execution.engagement_id.clone(),
        finding_id: None,
        execution_id: Some(execution.id.clone()),
        artifact_id: None,
        summary: format!(
            "Tool {tool_name} exceeded the Auto command timeout of {timeout_seconds} seconds and was interrupted"
        ),
        purpose: EvidencePurpose::Operational,
        reproducible: false,
        captured_at: unix_timestamp(),
    };
    if let Err(error) = state.store.put_evidence(&evidence).await {
        state
            .emit_event(
                &execution.engagement_id,
                "evidence/captureFailed",
                json!({"executionId": execution.id, "message": error.to_string()}),
            )
            .await;
    } else {
        state
            .publish(
                &execution.engagement_id,
                "evidence/captured",
                serde_json::to_value(&evidence).unwrap_or_default(),
            )
            .await;
    }

    state
        .timed_out_auto_turns
        .write()
        .await
        .insert((execution.engagement_id.clone(), execution.turn_id.clone()));
    state
        .stop_engagement_work(&execution.engagement_id, AgentThreadDisposition::Preserve)
        .await;
}

fn truncate_utf8(value: &str, max_bytes: usize) -> &str {
    if value.len() <= max_bytes {
        return value;
    }
    let end = value
        .char_indices()
        .map(|(index, _)| index)
        .take_while(|index| *index <= max_bytes)
        .last()
        .unwrap_or(0);
    &value[..end]
}

#[cfg(test)]
#[path = "auto_tools_tests.rs"]
mod tests;
