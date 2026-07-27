use crate::api::ApiError;
use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use axum::Json;
use axum::extract::Path;
use axum::extract::State;
use codex_riftx_core::AutoLlmProfileSnapshot;
use codex_riftx_core::AutoRun;
use codex_riftx_core::AutoRunConfig;
use codex_riftx_core::AutoRunLimits;
use codex_riftx_core::AutoRunState;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::ExecutionMode;
use sha2::Digest;
use sha2::Sha256;

pub(crate) async fn get(
    State(state): State<GatewayState>,
    Path(engagement_id): Path<String>,
) -> Result<Json<AutoRun>, ApiError> {
    let engagement = state.store.engagement(&engagement_id).await?;
    if engagement.mode != ExecutionMode::Auto {
        return Err(ApiError::bad_request("engagement is not in Auto mode"));
    }
    state
        .store
        .auto_run(&engagement_id)
        .await?
        .map(Json)
        .ok_or_else(|| ApiError::not_found("Auto run has not been prepared"))
}

pub(crate) async fn prepare(
    state: &GatewayState,
    engagement: &Engagement,
) -> Result<AutoRun, ApiError> {
    if engagement.mode != ExecutionMode::Auto {
        return Err(ApiError::bad_request("engagement is not in Auto mode"));
    }
    let config = snapshot_config(state, engagement)?;
    if engagement.status == EngagementStatus::Interrupted
        && let Some(existing) = state.store.auto_run(&engagement.id).await?
    {
        if existing.config != config {
            return Err(ApiError::conflict(
                "auto_snapshot_changed",
                "Auto run inputs changed after interruption; create a new engagement",
            ));
        }
        return Ok(existing);
    }

    let now = unix_timestamp();
    let run = AutoRun {
        engagement_id: engagement.id.clone(),
        config,
        state: AutoRunState::Ready,
        stop_reason: None,
        current_subgoal: None,
        turns_started: 0,
        turns_completed: 0,
        tool_calls: 0,
        consecutive_failures: 0,
        no_progress_turns: 0,
        started_at: None,
        updated_at: now,
    };
    state
        .append_engagement_critical(
            engagement,
            "auto/runPrepared",
            &serde_json::to_value(&run).map_err(|error| ApiError::internal(error.to_string()))?,
        )
        .await
        .map_err(|_| ApiError::audit_unavailable())?;
    state.store.put_auto_run(&run).await?;
    state
        .emit_event(
            &engagement.id,
            "auto/runPrepared",
            serde_json::json!({
                "state": run.state,
                "limits": run.config.limits,
                "toolsSnapshotSha256": run.config.tools_snapshot_sha256,
                "policyRevision": run.config.policy_revision,
            }),
        )
        .await;
    Ok(run)
}

fn snapshot_config(
    state: &GatewayState,
    engagement: &Engagement,
) -> Result<AutoRunConfig, ApiError> {
    let expires_at = engagement
        .authorization
        .window
        .expires_at
        .ok_or_else(|| ApiError::bad_request("Auto mode requires an authorization expiry"))?;
    let profile = state
        .config
        .llm
        .profiles
        .get(&engagement.llm_profile)
        .ok_or_else(|| ApiError::bad_request("Auto run LLM profile is not configured"))?;
    let profile_bytes =
        serde_json::to_vec(profile).map_err(|error| ApiError::internal(error.to_string()))?;
    let profile_sha256 = hex_digest(Sha256::digest(profile_bytes));
    Ok(AutoRunConfig {
        objective: engagement.objective.clone(),
        authorization: engagement.authorization.clone(),
        llm_profile: AutoLlmProfileSnapshot {
            name: engagement.llm_profile.clone(),
            model: profile.model.clone(),
            base_url: profile.base_url.clone(),
            protocol: profile.protocol.as_str().to_string(),
            timeout_seconds: profile.timeout_seconds,
            reasoning_level: profile.reasoning_level.as_str().to_string(),
            context_budget: profile.context_budget,
            config_sha256: profile_sha256,
        },
        tools_snapshot_sha256: state.tools.snapshot_sha256.clone(),
        policy_revision: engagement.policy_revision.clone(),
        expires_at,
        limits: AutoRunLimits::default(),
    })
}

fn hex_digest(bytes: impl AsRef<[u8]>) -> String {
    bytes
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
#[path = "auto_run_tests.rs"]
mod tests;
