use super::*;
use crate::api::tests::test_state;
use codex_riftx_core::AssessmentObjective;
use codex_riftx_core::Asset;
use codex_riftx_core::AuthorizationScope;
use codex_riftx_core::AuthorizationWindow;
use codex_riftx_core::AutoLlmProfileSnapshot;
use codex_riftx_core::AutoRunConfig;
use codex_riftx_core::AutoRunLimits;
use codex_riftx_core::AutoRunState;
use codex_riftx_core::AutoStopReason;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::EnvironmentClass;
use codex_riftx_core::ExecutionMode;
use codex_riftx_core::Scope;
use pretty_assertions::assert_eq;
use serde_json::json;
use tempfile::TempDir;

#[tokio::test]
async fn newly_discovered_structured_state_is_deterministic_progress() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = engagement();
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let mut run = auto_run(&engagement);
    run.no_progress_turns = 2;
    run.progress_baseline = Some(
        snapshot(&state, &engagement.id)
            .await
            .expect("baseline snapshot"),
    );
    state
        .store
        .put_asset(&Asset {
            id: "asset-1".to_string(),
            engagement_id: engagement.id.clone(),
            kind: "host".to_string(),
            value: "10.10.0.10".to_string(),
            discovered_at: 120,
        })
        .await
        .expect("store asset");

    let assessment = evaluate(&state, &run, 130)
        .await
        .expect("evaluate progress");

    assert!(assessment.progressed);
    assert_eq!(assessment.signals, vec![AutoProgressSignal::Asset]);
    assert_eq!(assessment.no_progress_turns, 0);
    assert_eq!(assessment.action, AutoProgressAction::Continue);
}

#[tokio::test]
async fn unchanged_state_escalates_from_replan_to_strategy_switch_to_input() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = engagement();
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let baseline = snapshot(&state, &engagement.id)
        .await
        .expect("baseline snapshot");
    let mut run = auto_run(&engagement);
    run.progress_baseline = Some(baseline);

    let first = evaluate(&state, &run, 110).await.expect("first evaluation");
    assert_eq!(first.action, AutoProgressAction::Replan);
    assert_eq!(first.no_progress_turns, 1);

    run.no_progress_turns = first.no_progress_turns;
    let second = evaluate(&state, &run, 120)
        .await
        .expect("second evaluation");
    assert_eq!(second.action, AutoProgressAction::SwitchStrategy);
    assert_eq!(second.no_progress_turns, 2);

    run.no_progress_turns = second.no_progress_turns;
    let third = evaluate(&state, &run, 130).await.expect("third evaluation");
    assert_eq!(third.action, AutoProgressAction::NeedsInput);
    assert_eq!(third.no_progress_turns, 3);
}

#[tokio::test]
async fn no_progress_window_persists_needs_input_and_stops_scheduling() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = engagement();
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let mut run = auto_run(&engagement);
    run.state = AutoRunState::Running;
    run.turns_started = 3;
    run.no_progress_turns = 2;
    run.progress_baseline = Some(
        snapshot(&state, &engagement.id)
            .await
            .expect("baseline snapshot"),
    );
    state.store.put_auto_run(&run).await.expect("store run");

    crate::auto_run::on_turn_completed(
        &state,
        &engagement.id,
        &json!({"turn": {"status": "completed"}}),
    )
    .await;

    let stopped = state
        .store
        .auto_run(&engagement.id)
        .await
        .expect("load run")
        .expect("run");
    assert_eq!(stopped.state, AutoRunState::NeedsInput);
    assert_eq!(stopped.stop_reason, Some(AutoStopReason::NoProgress));
    assert_eq!(stopped.no_progress_turns, 3);
    assert_eq!(
        stopped
            .last_progress_assessment
            .as_ref()
            .map(|assessment| assessment.action),
        Some(AutoProgressAction::NeedsInput)
    );
    assert!(state.active_turns.read().await.is_empty());
}

fn engagement() -> Engagement {
    Engagement {
        id: "engagement-auto-progress".to_string(),
        name: "Auto progress".to_string(),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Make deterministic progress".to_string(),
            success_criteria: Vec::new(),
            structured_criteria: Vec::new(),
        },
        entry_points: vec!["10.10.0.10".to_string()],
        mode: ExecutionMode::Auto,
        llm_profile: "default".to_string(),
        auto_limits: None,
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: Vec::new(),
                domains: Vec::new(),
                ports: Vec::new(),
            },
            identities: Vec::new(),
            capabilities: vec!["network.discovery".to_string()],
            environment: EnvironmentClass::Lab,
            window: AuthorizationWindow {
                starts_at: None,
                expires_at: Some(4_000_000_000),
            },
        },
        policy_revision: "policy-v1".to_string(),
        thread_id: None,
        created_at: 1,
        updated_at: 1,
    }
}

fn auto_run(engagement: &Engagement) -> AutoRun {
    AutoRun {
        engagement_id: engagement.id.clone(),
        config: AutoRunConfig {
            objective: engagement.objective.clone(),
            authorization: engagement.authorization.clone(),
            llm_profile: AutoLlmProfileSnapshot {
                name: "default".to_string(),
                model: "model".to_string(),
                base_url: "https://example.invalid/v1".to_string(),
                protocol: "responses".to_string(),
                timeout_seconds: 30,
                reasoning_level: "medium".to_string(),
                context_budget: 32_000,
                config_sha256: "a".repeat(64),
            },
            tools_snapshot_sha256: "b".repeat(64),
            policy_revision: engagement.policy_revision.clone(),
            expires_at: 4_000_000_000,
            limits: AutoRunLimits::default(),
        },
        state: AutoRunState::Evaluating,
        stop_reason: None,
        current_subgoal: Some("Take one verifiable step".to_string()),
        turns_started: 1,
        turns_completed: 0,
        tool_calls: 0,
        consecutive_failures: 0,
        no_progress_turns: 0,
        unavailable_tools: Vec::new(),
        last_goal_assessment: None,
        progress_baseline: None,
        last_progress_assessment: None,
        started_at: None,
        updated_at: 100,
    }
}
