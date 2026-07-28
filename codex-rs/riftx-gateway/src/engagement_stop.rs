use crate::gateway_state::GatewayState;
use crate::gateway_state::PendingApprovalKind;
use crate::gateway_state::unix_timestamp;
use codex_riftx_core::ApprovalDecisionReason;
use codex_riftx_core::ExecutionStatus;
use serde_json::json;

#[derive(Clone, Copy)]
pub(crate) enum AgentThreadDisposition {
    Preserve,
    Remove,
}

impl GatewayState {
    pub(crate) async fn stop_engagement_work(
        &self,
        engagement_id: &str,
        thread_disposition: AgentThreadDisposition,
    ) {
        let cancellations = self
            .credential_processes
            .read()
            .await
            .values()
            .filter(|process| process.engagement_id == engagement_id)
            .map(|process| process.cancellation.clone())
            .collect::<Vec<_>>();
        for cancellation in cancellations {
            cancellation.cancel();
        }

        let active_turn = self.active_turns.read().await.get(engagement_id).cloned();
        if let Some(active_turn) = active_turn {
            let is_only_profile_turn = self
                .active_turns
                .read()
                .await
                .values()
                .filter(|turn| turn.profile_name == active_turn.profile_name)
                .count()
                == 1;
            if let Some(app_server) = self.app_server(&active_turn.profile_name) {
                let _ = app_server
                    .interrupt_turn(active_turn.thread_id.clone(), active_turn.turn_id.clone())
                    .await;
                let _ = app_server
                    .clean_background_terminals(active_turn.thread_id.clone())
                    .await;
            }
            if is_only_profile_turn {
                self.cancel_profile_model_requests(&active_turn.profile_name);
            }
            crate::execution_events::finish_turn(
                self,
                engagement_id,
                &active_turn.turn_id,
                ExecutionStatus::Interrupted,
            )
            .await;
        }
        self.active_turns.write().await.remove(engagement_id);
        if matches!(thread_disposition, AgentThreadDisposition::Remove) {
            self.agent_threads.write().await.remove(engagement_id);
        }

        for pending in self.take_pending_approvals(engagement_id).await {
            if crate::approval_history::record_system_cancellation(
                self,
                &pending.view,
                ApprovalDecisionReason::EngagementStopped,
                unix_timestamp(),
            )
            .await
            .is_err()
            {
                self.publish(
                    engagement_id,
                    "approval/historyWriteFailed",
                    json!({"approvalId": pending.view.id, "operation": "cancel"}),
                )
                .await;
            }
            match pending.kind {
                PendingApprovalKind::Command(command) => {
                    if let Some(app_server) = self.app_server(&pending.profile_name) {
                        let _ = app_server
                            .decide_command_approval(
                                *command,
                                codex_riftx_app_server_adapter::OperatorApprovalDecision::Deny,
                            )
                            .await;
                    }
                }
                PendingApprovalKind::Tool { decision_tx } => {
                    let _ = decision_tx.send(false);
                }
            }
        }
        tokio::spawn(crate::artifacts::capture_pending(
            self.clone(),
            engagement_id.to_string(),
        ));
    }
}
