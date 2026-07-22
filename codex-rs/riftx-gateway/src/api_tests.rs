use super::*;
use axum::body::Body;
use axum::http::Request;
use codex_riftx_core::ApprovalMode;
use codex_riftx_core::ArtifactConfig;
use codex_riftx_core::AuditConfig;
use codex_riftx_core::GatewayConfig;
use codex_riftx_core::ManagedPolicyConfig;
use codex_riftx_core::ManagerConfig;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::SandboxConfig;
use codex_riftx_core::StateStore;
use codex_riftx_core::ToolProfileConfig;
use codex_riftx_manager_client::ManagerClient;
use http_body_util::BodyExt;
use pretty_assertions::assert_eq;
use std::collections::BTreeMap;
use std::time::Duration;
use tempfile::TempDir;
use tower::ServiceExt;

async fn test_state(temp: &TempDir) -> GatewayState {
    let config = RiftxConfig {
        gateway: GatewayConfig {
            listen: "127.0.0.1:0".to_string(),
            operator_token_env: "RIFTX_OPERATOR_TOKEN".to_string(),
            state_db: temp.path().join("state.sqlite"),
        },
        manager: ManagerConfig {
            socket: temp.path().join("manager.sock"),
            request_timeout_ms: 100,
        },
        sandbox: SandboxConfig {
            image: "riftx/sandbox:test".to_string(),
            cpu_limit: 1,
            memory_mib: 512,
            pids_limit: 128,
        },
        policy: ManagedPolicyConfig {
            allowed_tools: vec!["rt_nmap".to_string()],
            denied_cidrs: Vec::new(),
            denied_domains: Vec::new(),
        },
        audit: AuditConfig {
            jsonl_path: temp.path().join("audit.jsonl"),
            fsync: false,
        },
        artifacts: ArtifactConfig {
            root: temp.path().join("artifacts"),
            max_bytes_per_engagement: 1024,
        },
        tool_profiles: BTreeMap::from([(
            "recon".to_string(),
            ToolProfileConfig {
                allowed_tools: vec!["rt_nmap".to_string()],
                scope: Scope {
                    cidrs: vec!["0.0.0.0/0".parse().expect("CIDR")],
                    domains: vec!["*".to_string()],
                    ports: Vec::new(),
                },
                approval: ApprovalMode::HighRisk,
            },
        )]),
    };
    let store = StateStore::open(&config.gateway.state_db)
        .await
        .expect("state store");
    let manager = ManagerClient::new(&config.manager.socket, Duration::from_millis(100))
        .expect("manager client");
    GatewayState::new(config, store, manager)
}

async fn test_router(temp: &TempDir) -> Router {
    build_router(test_state(temp).await, "secret".to_string())
}

#[tokio::test]
async fn restart_reconciliation_interrupts_active_engagements() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = Engagement {
        id: "eng-active".to_string(),
        name: "Juice Shop".to_string(),
        status: EngagementStatus::Active,
        scope: Scope {
            cidrs: vec!["10.10.0.0/24".parse().expect("CIDR")],
            domains: Vec::new(),
            ports: vec![80],
        },
        tool_profile: "recon".to_string(),
        policy_revision: "rev-1".to_string(),
        sandbox_id: Some("sandbox-stale".to_string()),
        thread_id: Some("thread-stale".to_string()),
        created_at: 1,
        updated_at: 1,
    };
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    state
        .reconcile_after_restart()
        .await
        .expect("reconcile state");
    assert_eq!(
        state
            .store
            .engagement(&engagement.id)
            .await
            .expect("read engagement")
            .status,
        EngagementStatus::Interrupted
    );
}

#[tokio::test]
async fn bearer_token_is_required() {
    let temp = TempDir::new().expect("temp dir");
    let response = test_router(&temp)
        .await
        .oneshot(
            Request::builder()
                .uri("/v1/engagements/missing")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn engagement_can_be_created_and_read() {
    let temp = TempDir::new().expect("temp dir");
    let app = test_router(&temp).await;
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/engagements")
                .header("authorization", "Bearer secret")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"name":"Juice Shop","scope":{"cidrs":["10.10.0.0/24"],"domains":["juice.local"],"ports":[80]},"toolProfile":"recon"}"#,
                ))
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::CREATED);
    let engagement: Engagement = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("engagement JSON");
    assert_eq!(engagement.status, EngagementStatus::Draft);

    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/v1/engagements/{}", engagement.id))
                .header("authorization", "Bearer secret")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::OK);
}
