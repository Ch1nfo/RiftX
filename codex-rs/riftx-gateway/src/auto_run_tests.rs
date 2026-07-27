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
async fn activation_prepares_and_persists_a_bounded_auto_snapshot() {
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

    let response = app
        .clone()
        .oneshot(
            Request::post(format!("/v1/engagements/{}/activate", engagement.id))
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("activate response");
    let activation_status = response.status();
    let activation_body = response
        .into_body()
        .collect()
        .await
        .expect("activate body")
        .to_bytes();
    assert_eq!(
        activation_status,
        StatusCode::OK,
        "{}",
        String::from_utf8_lossy(&activation_body)
    );

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
        EngagementStatus::Active
    );
}
