use crate::api::ApiError;
use crate::api::TurnRequestSource;
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
use codex_riftx_core::AutoStopReason;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::ExecutionMode;
use codex_riftx_core::StateError;
use serde_json::Value;
use serde_json::json;
use sha2::Digest;
use sha2::Sha256;

const AUTO_PROMPT_MAX_BYTES: usize = 4_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum TurnOutcome {
    Completed,
    Interrupted,
    Failed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct StopDecision {
    state: AutoRunState,
    reason: AutoStopReason,
}

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
            json!({
                "state": run.state,
                "limits": run.config.limits,
                "toolsSnapshotSha256": run.config.tools_snapshot_sha256,
                "policyRevision": run.config.policy_revision,
            }),
        )
        .await;
    Ok(run)
}

pub(crate) async fn start_locked(
    state: &GatewayState,
    engagement: &Engagement,
) -> Result<(), ApiError> {
    let mut run = state
        .store
        .auto_run(&engagement.id)
        .await?
        .ok_or_else(|| ApiError::not_found("Auto run has not been prepared"))?;
    if !matches!(
        run.state,
        AutoRunState::Ready | AutoRunState::Paused | AutoRunState::NeedsInput
    ) {
        return Err(ApiError::conflict(
            "auto_not_startable",
            format!("Auto run cannot start from state {:?}", run.state),
        ));
    }

    let now = unix_timestamp();
    if let Some(decision) = stop_decision(&run, now) {
        stop_run(state, &mut run, decision).await?;
        return Ok(());
    }
    let audit_kind = if run.started_at.is_some() {
        "auto/runResumed"
    } else {
        "auto/runStarted"
    };
    state
        .append_engagement_critical(
            engagement,
            audit_kind,
            &json!({
                "turnsStarted": run.turns_started,
                "toolCalls": run.tool_calls,
                "limits": run.config.limits,
            }),
        )
        .await
        .map_err(|_| ApiError::audit_unavailable())?;
    run.started_at.get_or_insert(now);
    start_next_turn_locked(state, &mut run).await
}

pub(crate) async fn expire(state: &GatewayState, engagement_id: &str) -> Result<(), StateError> {
    let Some(mut run) = state.store.auto_run(engagement_id).await? else {
        return Ok(());
    };
    if matches!(
        run.state,
        AutoRunState::Succeeded
            | AutoRunState::Expired
            | AutoRunState::BudgetExhausted
            | AutoRunState::Failed
            | AutoRunState::Killed
    ) {
        return Ok(());
    }
    run.state = AutoRunState::Expired;
    run.stop_reason = Some(AutoStopReason::AuthorizationExpired);
    run.updated_at = unix_timestamp();
    state.store.put_auto_run(&run).await?;
    state
        .emit_event(
            engagement_id,
            "auto/stopped",
            json!({
                "state": run.state,
                "reason": run.stop_reason,
                "turnsStarted": run.turns_started,
                "turnsCompleted": run.turns_completed,
                "toolCalls": run.tool_calls,
            }),
        )
        .await;
    Ok(())
}

pub(crate) async fn on_turn_completed(state: &GatewayState, engagement_id: &str, data: &Value) {
    let permit = state.turn_slot.clone().acquire_owned().await;
    let Ok(_permit) = permit else {
        return;
    };
    if let Err(error) = on_turn_completed_locked(state, engagement_id, data).await {
        state
            .emit_event(
                engagement_id,
                "auto/controllerError",
                json!({"message": error.to_string()}),
            )
            .await;
    }
}

async fn on_turn_completed_locked(
    state: &GatewayState,
    engagement_id: &str,
    data: &Value,
) -> Result<(), ApiError> {
    let engagement = state.store.engagement(engagement_id).await?;
    if engagement.mode != ExecutionMode::Auto || engagement.status != EngagementStatus::Active {
        return Ok(());
    }
    let Some(mut run) = state.store.auto_run(engagement_id).await? else {
        return Ok(());
    };
    if run.state != AutoRunState::Running {
        return Ok(());
    }

    let outcome = match data.pointer("/turn/status").and_then(Value::as_str) {
        Some("completed") => TurnOutcome::Completed,
        Some("interrupted") => TurnOutcome::Interrupted,
        _ => TurnOutcome::Failed,
    };
    apply_turn_completion(&mut run, outcome, unix_timestamp());
    state.store.put_auto_run(&run).await?;
    state
        .emit_event(
            engagement_id,
            "auto/turnEvaluated",
            json!({
                "state": run.state,
                "turnsCompleted": run.turns_completed,
                "consecutiveFailures": run.consecutive_failures,
                "outcome": match outcome {
                    TurnOutcome::Completed => "completed",
                    TurnOutcome::Interrupted => "interrupted",
                    TurnOutcome::Failed => "failed",
                },
            }),
        )
        .await;

    let now = unix_timestamp();
    let decision = if outcome == TurnOutcome::Interrupted {
        Some(StopDecision {
            state: AutoRunState::Paused,
            reason: AutoStopReason::OperatorPause,
        })
    } else {
        stop_decision(&run, now)
    };
    if let Some(decision) = decision {
        stop_run(state, &mut run, decision).await?;
        return Ok(());
    }
    start_next_turn_locked(state, &mut run).await
}

async fn start_next_turn_locked(state: &GatewayState, run: &mut AutoRun) -> Result<(), ApiError> {
    let now = unix_timestamp();
    if let Some(decision) = stop_decision(run, now) {
        stop_run(state, run, decision).await?;
        return Ok(());
    }

    prepare_next_turn(run, now);
    state.store.put_auto_run(run).await?;
    state
        .emit_event(
            &run.engagement_id,
            "auto/turnScheduled",
            json!({
                "turn": run.turns_started,
                "maxTurns": run.config.limits.max_turns,
                "currentSubgoal": run.current_subgoal,
            }),
        )
        .await;
    let prompt = auto_turn_prompt(run);
    if let Err(error) = crate::api::start_turn_locked(
        state,
        &run.engagement_id,
        Some(prompt),
        TurnRequestSource::Auto,
    )
    .await
    {
        stop_run(
            state,
            run,
            StopDecision {
                state: AutoRunState::Failed,
                reason: AutoStopReason::UnrecoverableError,
            },
        )
        .await?;
        return Err(error);
    }
    Ok(())
}

fn apply_turn_completion(run: &mut AutoRun, outcome: TurnOutcome, now: i64) {
    run.state = AutoRunState::Evaluating;
    run.turns_completed = run.turns_completed.saturating_add(1);
    run.consecutive_failures = match outcome {
        TurnOutcome::Completed => 0,
        TurnOutcome::Interrupted | TurnOutcome::Failed => {
            run.consecutive_failures.saturating_add(1)
        }
    };
    run.updated_at = now;
}

fn prepare_next_turn(run: &mut AutoRun, now: i64) {
    run.state = AutoRunState::Running;
    run.stop_reason = None;
    run.turns_started = run.turns_started.saturating_add(1);
    run.current_subgoal = Some(if run.turns_started == 1 {
        "Establish the current authorized state and take the first verifiable step".to_string()
    } else {
        format!(
            "Advance the objective with one bounded, verifiable step (turn {})",
            run.turns_started
        )
    });
    run.updated_at = now;
}

fn stop_decision(run: &AutoRun, now: i64) -> Option<StopDecision> {
    if now >= run.config.expires_at {
        return Some(StopDecision {
            state: AutoRunState::Expired,
            reason: AutoStopReason::AuthorizationExpired,
        });
    }
    if run.started_at.is_some_and(|started_at| {
        now.saturating_sub(started_at) >= run.config.limits.max_wall_clock_seconds as i64
    }) {
        return Some(StopDecision {
            state: AutoRunState::BudgetExhausted,
            reason: AutoStopReason::WallClockBudgetExhausted,
        });
    }
    if run.turns_started >= run.config.limits.max_turns {
        return Some(StopDecision {
            state: AutoRunState::BudgetExhausted,
            reason: AutoStopReason::TurnBudgetExhausted,
        });
    }
    if run.tool_calls >= run.config.limits.max_tool_calls {
        return Some(StopDecision {
            state: AutoRunState::BudgetExhausted,
            reason: AutoStopReason::ToolBudgetExhausted,
        });
    }
    if run.consecutive_failures >= run.config.limits.max_consecutive_failures {
        return Some(StopDecision {
            state: AutoRunState::Failed,
            reason: AutoStopReason::ConsecutiveFailures,
        });
    }
    None
}

async fn stop_run(
    state: &GatewayState,
    run: &mut AutoRun,
    decision: StopDecision,
) -> Result<(), ApiError> {
    run.state = decision.state;
    run.stop_reason = Some(decision.reason);
    run.updated_at = unix_timestamp();
    state.store.put_auto_run(run).await?;
    state
        .emit_event(
            &run.engagement_id,
            "auto/stopped",
            json!({
                "state": run.state,
                "reason": run.stop_reason,
                "turnsStarted": run.turns_started,
                "turnsCompleted": run.turns_completed,
                "toolCalls": run.tool_calls,
            }),
        )
        .await;
    Ok(())
}

fn auto_turn_prompt(run: &AutoRun) -> String {
    let criteria = if run.config.objective.success_criteria.is_empty() {
        "No textual success criteria were supplied; do not claim success without verifiable evidence."
            .to_string()
    } else {
        run.config
            .objective
            .success_criteria
            .iter()
            .map(|criterion| format!("- {criterion}"))
            .collect::<Vec<_>>()
            .join("\n")
    };
    let prompt = format!(
        "RiftX Auto controller turn {}/{}.\nObjective: {}\nCurrent subgoal: {}\nSuccess criteria:\n{}\n\nLoad the current structured engagement state, then take exactly one bounded and verifiable step within the declared authorization scope. Preserve evidence for material claims. Do not expand scope based only on newly discovered assets. End the turn with a concise progress summary, evidence references, unresolved ambiguity, and the safest next step. The RiftX controller—not this response—decides whether another turn runs.",
        run.turns_started,
        run.config.limits.max_turns,
        run.config.objective.summary,
        run.current_subgoal.as_deref().unwrap_or("not assigned"),
        criteria,
    );
    truncate_utf8(&prompt, AUTO_PROMPT_MAX_BYTES).to_string()
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
