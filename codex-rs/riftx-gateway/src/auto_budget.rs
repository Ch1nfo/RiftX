use crate::engagement_stop::AgentThreadDisposition;
use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use codex_riftx_core::AutoRun;
use codex_riftx_core::AutoRunState;
use codex_riftx_core::AutoStopReason;
use serde_json::json;
use std::time::Duration;
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

impl GatewayState {
    pub(crate) async fn register_auto_wall_clock_budget(&self, run: &AutoRun) {
        self.cancel_auto_wall_clock_budget(&run.engagement_id).await;
        let Some(started_at) = run.started_at else {
            return;
        };
        if terminal_state(run.state) {
            return;
        }
        let deadline = started_at.saturating_add(
            i64::try_from(run.config.limits.max_wall_clock_seconds).unwrap_or(i64::MAX),
        );
        let cancellation = CancellationToken::new();
        self.auto_budget_tasks
            .write()
            .await
            .insert(run.engagement_id.clone(), cancellation.clone());
        let state = self.clone();
        let engagement_id = run.engagement_id.clone();
        tokio::spawn(async move {
            wait_for_budget(deadline, cancellation.clone()).await;
            if cancellation.is_cancelled() {
                return;
            }
            state
                .exhaust_auto_wall_clock_budget(&engagement_id, deadline)
                .await;
        });
    }

    pub(crate) async fn cancel_auto_wall_clock_budget(&self, engagement_id: &str) {
        if let Some(cancellation) = self.auto_budget_tasks.write().await.remove(engagement_id) {
            cancellation.cancel();
        }
    }

    async fn exhaust_auto_wall_clock_budget(&self, engagement_id: &str, deadline: i64) {
        let Ok(_permit) = self.turn_slot.clone().acquire_owned().await else {
            return;
        };
        let Ok(Some(mut run)) = self.store.auto_run(engagement_id).await else {
            self.auto_budget_tasks.write().await.remove(engagement_id);
            return;
        };
        let expected_deadline = run.started_at.map(|started_at| {
            started_at.saturating_add(
                i64::try_from(run.config.limits.max_wall_clock_seconds).unwrap_or(i64::MAX),
            )
        });
        if expected_deadline != Some(deadline) || terminal_state(run.state) {
            self.auto_budget_tasks.write().await.remove(engagement_id);
            return;
        }
        let Ok(engagement) = self.store.engagement(engagement_id).await else {
            self.auto_budget_tasks.write().await.remove(engagement_id);
            return;
        };
        if self
            .append_engagement_critical(
                &engagement,
                "auto/wallClockBudgetExhausted",
                &json!({
                    "startedAt": run.started_at,
                    "deadline": deadline,
                    "maxWallClockSeconds": run.config.limits.max_wall_clock_seconds,
                }),
            )
            .await
            .is_err()
        {
            self.stop_engagement_work(engagement_id, AgentThreadDisposition::Preserve)
                .await;
            let _ = crate::auto_run::lifecycle_stop(
                self,
                engagement_id,
                crate::auto_run::AutoLifecycleStop::AuditUnavailable,
            )
            .await;
            self.auto_budget_tasks.write().await.remove(engagement_id);
            return;
        }

        self.stop_engagement_work(engagement_id, AgentThreadDisposition::Preserve)
            .await;
        run.state = AutoRunState::BudgetExhausted;
        run.stop_reason = Some(AutoStopReason::WallClockBudgetExhausted);
        run.updated_at = unix_timestamp();
        if self.store.put_auto_run(&run).await.is_err() {
            self.emit_event(
                engagement_id,
                "auto/controllerError",
                json!({"message": "wall-clock budget state could not be persisted"}),
            )
            .await;
            self.auto_budget_tasks.write().await.remove(engagement_id);
            return;
        }
        self.emit_event(
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
        self.auto_budget_tasks.write().await.remove(engagement_id);
    }
}

fn terminal_state(state: AutoRunState) -> bool {
    matches!(
        state,
        AutoRunState::Succeeded
            | AutoRunState::Expired
            | AutoRunState::BudgetExhausted
            | AutoRunState::Failed
            | AutoRunState::Killed
    )
}

async fn wait_for_budget(deadline: i64, cancellation: CancellationToken) {
    let delay = u64::try_from(deadline.saturating_sub(unix_timestamp())).unwrap_or(0);
    tokio::select! {
        () = tokio::time::sleep_until(Instant::now() + Duration::from_secs(delay)) => {}
        () = cancellation.cancelled() => {}
    }
}

#[cfg(test)]
#[path = "auto_budget_tests.rs"]
mod tests;
