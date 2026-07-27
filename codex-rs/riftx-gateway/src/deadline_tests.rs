use super::*;
use crate::api::build_router;
use crate::api::tests::block_audit;
use crate::api::tests::native_engagement;
use crate::api::tests::test_state;
use crate::gateway_state::ActiveCredentialProcess;
use crate::gateway_state::PendingApprovalKind;
use crate::gateway_state::PendingApprovalRequest;
use axum::body::Body;
use axum::http::Request;
use codex_riftx_core::ApprovalActor;
use codex_riftx_core::ApprovalDecisionReason;
use codex_riftx_core::ApprovalOutcome;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::Task;
use codex_riftx_core::TaskStatus;
use codex_riftx_ipc::ApprovalKind;
use codex_riftx_ipc::AuditHealthState;
use codex_riftx_ipc::PendingApproval;
use pretty_assertions::assert_eq;
use tempfile::TempDir;
use tokio_util::sync::CancellationToken;
use tower::ServiceExt;

#[tokio::test]
async fn watchdog_expires_active_work_and_revokes_pending_execution() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let mut engagement = native_engagement(&state, "eng-deadline", EngagementStatus::Active);
    engagement.authorization.window.expires_at = Some(unix_timestamp() + 1);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let task = Task {
        id: "task-deadline".to_string(),
        engagement_id: engagement.id.clone(),
        kind: "main_agent_turn".to_string(),
        status: TaskStatus::Running,
        turn_id: Some("turn-deadline".to_string()),
        error: None,
    };
    state.store.put_task(&task).await.expect("store task");

    let process_cancellation = CancellationToken::new();
    state.credential_processes.write().await.insert(
        "credential-use".to_string(),
        ActiveCredentialProcess {
            engagement_id: engagement.id.clone(),
            cancellation: process_cancellation.clone(),
        },
    );
    let (decision_tx, decision_rx) = tokio::sync::oneshot::channel();
    state.pending_approvals.write().await.insert(
        "approval-deadline".to_string(),
        PendingApprovalRequest {
            profile_name: "default".to_string(),
            engagement_id: engagement.id.clone(),
            view: PendingApproval {
                id: "approval-deadline".to_string(),
                engagement_id: engagement.id.clone(),
                policy_revision: engagement.policy_revision.clone(),
                kind: ApprovalKind::Tool,
                requested_at: unix_timestamp(),
                command: None,
                cwd: None,
                reason: Some("deadline test".to_string()),
                execution_intent: None,
            },
            kind: PendingApprovalKind::Tool { decision_tx },
        },
    );
    let mut events = state.event_sender(&engagement.id).await.subscribe();

    state.register_authorization_deadline(&engagement).await;

    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let current = state
                .store
                .engagement(&engagement.id)
                .await
                .expect("read engagement");
            if current.status == EngagementStatus::Expired {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("deadline watchdog");

    assert!(process_cancellation.is_cancelled());
    assert!(!decision_rx.await.expect("approval decision"));
    assert_eq!(
        state
            .store
            .approvals(&engagement.id)
            .await
            .expect("approval history")
            .into_iter()
            .map(|record| (record.outcome, record.actor, record.decision_reason))
            .collect::<Vec<_>>(),
        vec![(
            ApprovalOutcome::Cancelled,
            Some(ApprovalActor::System),
            Some(ApprovalDecisionReason::EngagementStopped),
        )]
    );
    assert!(state.deadline_tasks.read().await.is_empty());
    assert_eq!(
        state.store.tasks(&engagement.id).await.expect("tasks"),
        vec![Task {
            status: TaskStatus::Expired,
            ..task
        }]
    );
    let event = events.recv().await.expect("expiration event");
    assert_eq!(event.kind, "engagementExpired");
    let audit = state.audit.read_records(100).await.expect("audit");
    assert!(
        audit
            .iter()
            .any(|record| record.event == "engagement/authorizationExpired")
    );
}

#[tokio::test]
async fn expiration_stops_work_and_persists_when_critical_audit_is_unavailable() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let mut engagement = native_engagement(&state, "eng-deadline-audit", EngagementStatus::Active);
    engagement.authorization.window.expires_at = Some(unix_timestamp() - 1);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let process_cancellation = CancellationToken::new();
    state.credential_processes.write().await.insert(
        "credential-use-audit".to_string(),
        ActiveCredentialProcess {
            engagement_id: engagement.id.clone(),
            cancellation: process_cancellation.clone(),
        },
    );
    block_audit(&temp).await;

    assert!(
        state
            .expire_engagement_locked(engagement.clone(), ExpirationTrigger::CurrentTime)
            .await
            .expect("expire despite audit failure")
    );

    assert!(process_cancellation.is_cancelled());
    assert_eq!(
        state
            .store
            .engagement(&engagement.id)
            .await
            .expect("expired engagement")
            .status,
        EngagementStatus::Expired
    );
    assert_eq!(
        state.control_status().await.audit.state,
        AuditHealthState::Degraded
    );
}

#[tokio::test]
async fn restart_marks_past_deadline_active_work_expired_without_extending_it() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let mut engagement =
        native_engagement(&state, "eng-restart-deadline", EngagementStatus::Active);
    engagement.authorization.window.expires_at = Some(unix_timestamp() - 1);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    state
        .store
        .put_task(&Task {
            id: "task-restart-deadline".to_string(),
            engagement_id: engagement.id.clone(),
            kind: "main_agent_turn".to_string(),
            status: TaskStatus::Running,
            turn_id: Some("turn-restart-deadline".to_string()),
            error: None,
        })
        .await
        .expect("store task");

    let restarted = GatewayState::new(
        state.config.as_ref().clone(),
        state.store.clone(),
        state.skills.as_ref().clone(),
        state.tools.as_ref().clone(),
    );
    restarted
        .reconcile_after_restart()
        .await
        .expect("reconcile restart");

    assert_eq!(
        restarted
            .store
            .engagement(&engagement.id)
            .await
            .expect("engagement")
            .status,
        EngagementStatus::Expired
    );
    assert_eq!(
        restarted.store.tasks(&engagement.id).await.expect("tasks")[0].status,
        TaskStatus::Expired
    );
}

#[tokio::test]
async fn operator_interrupt_preserves_the_registered_deadline() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let mut engagement = native_engagement(&state, "eng-cancel-deadline", EngagementStatus::Active);
    engagement.authorization.window.expires_at = Some(unix_timestamp() + 60);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    state.register_authorization_deadline(&engagement).await;

    let response = build_router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/engagements/{}/interrupt", engagement.id))
                .body(Body::empty())
                .expect("interrupt request"),
        )
        .await
        .expect("interrupt response");

    assert_eq!(response.status(), axum::http::StatusCode::OK);
    assert!(
        state
            .deadline_tasks
            .read()
            .await
            .contains_key(&engagement.id)
    );
    assert_eq!(
        state
            .store
            .engagement(&engagement.id)
            .await
            .expect("engagement")
            .status,
        EngagementStatus::Active
    );
}
