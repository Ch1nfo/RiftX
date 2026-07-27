use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::StateError;
use codex_riftx_core::TaskStatus;
use serde_json::json;
use std::time::Duration;
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

const DEADLINE_POLL_INTERVAL: Duration = Duration::from_secs(1);

impl GatewayState {
    pub(crate) async fn register_authorization_deadline(&self, engagement: &Engagement) {
        self.cancel_authorization_deadline(&engagement.id).await;
        let Some(expires_at) = engagement.authorization.window.expires_at else {
            return;
        };
        let cancellation = CancellationToken::new();
        self.deadline_tasks
            .write()
            .await
            .insert(engagement.id.clone(), cancellation.clone());
        let state = self.clone();
        let engagement_id = engagement.id.clone();
        tokio::spawn(async move {
            wait_for_deadline(expires_at, cancellation.clone()).await;
            if cancellation.is_cancelled() {
                return;
            }
            state.expire_engagement(&engagement_id, expires_at).await;
        });
    }

    pub(crate) async fn cancel_authorization_deadline(&self, engagement_id: &str) {
        if let Some(cancellation) = self.deadline_tasks.write().await.remove(engagement_id) {
            cancellation.cancel();
        }
    }

    async fn expire_engagement(&self, engagement_id: &str, expires_at: i64) {
        let Ok(_permit) = self.turn_slot.clone().acquire_owned().await else {
            return;
        };
        let Ok(engagement) = self.store.engagement(engagement_id).await else {
            self.deadline_tasks.write().await.remove(engagement_id);
            return;
        };
        if engagement.authorization.window.expires_at != Some(expires_at) {
            self.deadline_tasks.write().await.remove(engagement_id);
            return;
        }
        if self
            .expire_engagement_locked(
                engagement,
                ExpirationTrigger::RegisteredDeadline { expires_at },
            )
            .await
            .is_err()
        {
            self.emit_event(
                engagement_id,
                "engagementExpirationFailed",
                json!({"error": "expiration state could not be persisted"}),
            )
            .await;
        }
    }

    pub(crate) async fn expire_engagement_locked(
        &self,
        engagement: Engagement,
        trigger: ExpirationTrigger,
    ) -> Result<bool, StateError> {
        let engagement_id = &engagement.id;
        let Some(expires_at) = engagement.authorization.window.expires_at else {
            return Ok(false);
        };
        let deadline_reached = match trigger {
            ExpirationTrigger::CurrentTime => unix_timestamp() >= expires_at,
            ExpirationTrigger::RegisteredDeadline {
                expires_at: expected,
            } => expected == expires_at,
        };
        if !deadline_reached
            || !matches!(
                engagement.status,
                EngagementStatus::Draft | EngagementStatus::Active | EngagementStatus::Interrupted
            )
        {
            return Ok(false);
        }

        if engagement.status == EngagementStatus::Active {
            let task_update = self
                .update_deadline_tasks(engagement_id, DeadlineTaskUpdate::Expiring)
                .await;
            self.stop_engagement_work(engagement_id).await;
            task_update?;
        }
        let audit_available = self
            .append_engagement_critical(
                &engagement,
                "engagement/authorizationExpired",
                &json!({"expiresAt": expires_at}),
            )
            .await
            .is_ok();
        let expired_at = unix_timestamp();
        self.store
            .transition_engagement(engagement_id, EngagementStatus::Expired, expired_at)
            .await?;
        self.update_deadline_tasks(engagement_id, DeadlineTaskUpdate::Expired)
            .await?;
        self.deadline_tasks.write().await.remove(engagement_id);
        self.emit_event(
            engagement_id,
            "engagementExpired",
            json!({
                "expiresAt": expires_at,
                "expiredAt": expired_at,
                "auditAvailable": audit_available,
            }),
        )
        .await;
        Ok(true)
    }

    async fn update_deadline_tasks(
        &self,
        engagement_id: &str,
        update: DeadlineTaskUpdate,
    ) -> Result<(), StateError> {
        let mut tasks = self.store.tasks(engagement_id).await?;
        for task in &mut tasks {
            let next = match (update, task.status) {
                (DeadlineTaskUpdate::Expiring, TaskStatus::Pending | TaskStatus::Running) => {
                    Some(TaskStatus::Expiring)
                }
                (
                    DeadlineTaskUpdate::Expired,
                    TaskStatus::Pending
                    | TaskStatus::Running
                    | TaskStatus::Interrupted
                    | TaskStatus::Expiring,
                ) => Some(TaskStatus::Expired),
                (DeadlineTaskUpdate::Expiring | DeadlineTaskUpdate::Expired, _) => None,
            };
            if let Some(next) = next {
                task.status = next;
                self.store.put_task(task).await?;
            }
        }
        Ok(())
    }
}

#[derive(Clone, Copy)]
pub(crate) enum ExpirationTrigger {
    CurrentTime,
    RegisteredDeadline { expires_at: i64 },
}

#[derive(Clone, Copy)]
enum DeadlineTaskUpdate {
    Expiring,
    Expired,
}

async fn wait_for_deadline(expires_at: i64, cancellation: CancellationToken) {
    let now = unix_timestamp();
    let remaining_seconds = expires_at.saturating_sub(now);
    let monotonic_deadline = u64::try_from(remaining_seconds)
        .ok()
        .and_then(|seconds| Instant::now().checked_add(Duration::from_secs(seconds)));
    loop {
        if unix_timestamp() >= expires_at
            || monotonic_deadline.is_some_and(|deadline| Instant::now() >= deadline)
        {
            return;
        }
        let wait = monotonic_deadline
            .map(|deadline| deadline.saturating_duration_since(Instant::now()))
            .map_or(DEADLINE_POLL_INTERVAL, |remaining| {
                remaining.min(DEADLINE_POLL_INTERVAL)
            });
        tokio::select! {
            () = cancellation.cancelled() => return,
            () = tokio::time::sleep(wait) => {}
        }
    }
}

#[cfg(test)]
#[path = "deadline_tests.rs"]
mod tests;
