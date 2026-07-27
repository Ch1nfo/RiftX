use super::*;
use crate::api::tests::test_state;
use crate::build_router;
use axum::body::Body;
use axum::http::Request;
use axum::http::StatusCode;
use codex_riftx_core::AutoRunLimits;
use codex_riftx_core::AutoRunState;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use http_body_util::BodyExt;
use pretty_assertions::assert_eq;
use tempfile::TempDir;
use tower::ServiceExt;

#[tokio::test]
async fn preparation_persists_a_bounded_auto_snapshot() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let app = build_router(state.clone());
    let response = app
        .clone()
        .oneshot(
            Request::post("/v1/engagements")
                .header("content-type", "application/json")
                .body(Body::from(format!(
                    r#"{{"name":"Auto snapshot","objective":{{"summary":"Enumerate the lab and preserve evidence","successCriteria":["Capture evidence"],"structuredCriteria":[]}},"entryPoints":["10.10.0.10"],"mode":"auto","confirmation":"{}","llmProfile":"default","authorization":{{"network":{{"cidrs":["10.10.0.0/24"],"domains":[],"ports":[]}},"identities":[],"capabilities":["network.discovery"],"environment":"lab","window":{{"startsAt":null,"expiresAt":4000000000}}}}}}"#,
                    codex_riftx_core::AUTO_MODE_CONFIRMATION
                )))
                .expect("request"),
        )
        .await
        .expect("create response");
    assert_eq!(response.status(), StatusCode::CREATED);
    let engagement: Engagement = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("create body")
            .to_bytes(),
    )
    .expect("engagement");

    prepare(&state, &engagement)
        .await
        .expect("prepare Auto run");

    let response = app
        .oneshot(
            Request::get(format!("/v1/engagements/{}/auto", engagement.id))
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("Auto status response");
    assert_eq!(response.status(), StatusCode::OK);
    let run: AutoRun = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("Auto status body")
            .to_bytes(),
    )
    .expect("Auto run");

    assert_eq!(run.engagement_id, engagement.id);
    assert_eq!(run.state, AutoRunState::Ready);
    assert_eq!(run.config.objective, engagement.objective);
    assert_eq!(run.config.authorization, engagement.authorization);
    assert_eq!(run.config.llm_profile.name, "default");
    assert_eq!(run.config.llm_profile.model, "riftx-test-model");
    assert_eq!(run.config.llm_profile.protocol, "responses");
    assert_eq!(run.config.llm_profile.reasoning_level, "high");
    assert_eq!(run.config.llm_profile.config_sha256.len(), 64);
    assert_eq!(
        run.config.tools_snapshot_sha256,
        state.tools.snapshot_sha256
    );
    assert_eq!(run.config.policy_revision, engagement.policy_revision);
    assert_eq!(run.config.expires_at, 4_000_000_000);
    assert_eq!(run.config.limits, AutoRunLimits::default());
    assert_eq!(
        state
            .store
            .auto_run(&engagement.id)
            .await
            .expect("stored run"),
        Some(run)
    );
    assert_eq!(
        state
            .store
            .engagement(&engagement.id)
            .await
            .expect("active engagement")
            .status,
        EngagementStatus::Draft
    );
}

#[tokio::test]
async fn operator_cannot_bypass_the_auto_controller_with_a_manual_turn() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = create_auto_engagement(&state).await;
    state
        .store
        .transition_engagement(
            &engagement.id,
            EngagementStatus::Active,
            engagement.updated_at.saturating_add(1),
        )
        .await
        .expect("activate engagement state");

    let response = build_router(state)
        .oneshot(
            Request::post(format!("/v1/engagements/{}/turns", engagement.id))
                .header("content-type", "application/json")
                .body(Body::from(r#"{"input":"manual step"}"#))
                .expect("request"),
        )
        .await
        .expect("turn response");

    assert_eq!(response.status(), StatusCode::CONFLICT);
    let body: serde_json::Value = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("response body")
            .to_bytes(),
    )
    .expect("error body");
    assert_eq!(body["code"], "auto_controller_required");
}

#[test]
fn completed_turns_advance_only_when_the_controller_schedules_them() {
    let mut run = sample_run();

    prepare_next_turn(&mut run, 10);
    assert_eq!(run.state, AutoRunState::Running);
    assert_eq!(run.turns_started, 1);
    assert_eq!(run.turns_completed, 0);

    apply_turn_completion(&mut run, TurnOutcome::Completed, 11);
    assert_eq!(run.state, AutoRunState::Evaluating);
    assert_eq!(run.turns_completed, 1);
    assert_eq!(stop_decision(&run, 11), None);

    prepare_next_turn(&mut run, 12);
    assert_eq!(run.turns_started, 2);
    assert_eq!(run.turns_completed, 1);
}

#[test]
fn turn_budget_produces_a_machine_readable_stop_reason() {
    let mut run = sample_run();
    run.config.limits.max_turns = 2;

    prepare_next_turn(&mut run, 10);
    apply_turn_completion(&mut run, TurnOutcome::Completed, 11);
    prepare_next_turn(&mut run, 12);
    apply_turn_completion(&mut run, TurnOutcome::Completed, 13);

    assert_eq!(
        stop_decision(&run, 13),
        Some(StopDecision {
            state: AutoRunState::BudgetExhausted,
            reason: codex_riftx_core::AutoStopReason::TurnBudgetExhausted,
        })
    );
}

#[test]
fn auto_prompt_is_utf8_safe_and_bounded() {
    let mut run = sample_run();
    run.config.objective.summary = "目标".repeat(3_000);
    prepare_next_turn(&mut run, 10);

    let prompt = auto_turn_prompt(&run);

    assert!(prompt.len() <= AUTO_PROMPT_MAX_BYTES);
    assert!(std::str::from_utf8(prompt.as_bytes()).is_ok());
}

async fn create_auto_engagement(state: &GatewayState) -> Engagement {
    let app = build_router(state.clone());
    let response = app
        .oneshot(
            Request::post("/v1/engagements")
                .header("content-type", "application/json")
                .body(Body::from(format!(
                    r#"{{"name":"Auto controlled","objective":{{"summary":"Enumerate the lab","successCriteria":[],"structuredCriteria":[]}},"entryPoints":["10.10.0.10"],"mode":"auto","confirmation":"{}","llmProfile":"default","authorization":{{"network":{{"cidrs":["10.10.0.0/24"],"domains":[],"ports":[]}},"identities":[],"capabilities":["network.discovery"],"environment":"lab","window":{{"startsAt":null,"expiresAt":4000000000}}}}}}"#,
                    codex_riftx_core::AUTO_MODE_CONFIRMATION
                )))
                .expect("request"),
        )
        .await
        .expect("create response");
    assert_eq!(response.status(), StatusCode::CREATED);
    serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("create body")
            .to_bytes(),
    )
    .expect("engagement")
}

fn sample_run() -> AutoRun {
    AutoRun {
        engagement_id: "engagement-auto".to_string(),
        config: codex_riftx_core::AutoRunConfig {
            objective: codex_riftx_core::AssessmentObjective {
                summary: "Enumerate the authorized lab".to_string(),
                success_criteria: vec!["Preserve evidence".to_string()],
                structured_criteria: Vec::new(),
            },
            authorization: codex_riftx_core::AuthorizationScope {
                network: codex_riftx_core::Scope {
                    cidrs: Vec::new(),
                    domains: Vec::new(),
                    ports: Vec::new(),
                },
                identities: Vec::new(),
                capabilities: vec!["network.discovery".to_string()],
                environment: codex_riftx_core::EnvironmentClass::Lab,
                window: codex_riftx_core::AuthorizationWindow {
                    starts_at: None,
                    expires_at: Some(10_000),
                },
            },
            llm_profile: codex_riftx_core::AutoLlmProfileSnapshot {
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
            policy_revision: "policy-v1".to_string(),
            expires_at: 10_000,
            limits: AutoRunLimits::default(),
        },
        state: AutoRunState::Ready,
        stop_reason: None,
        current_subgoal: None,
        turns_started: 0,
        turns_completed: 0,
        tool_calls: 0,
        consecutive_failures: 0,
        no_progress_turns: 0,
        started_at: Some(10),
        updated_at: 10,
    }
}
