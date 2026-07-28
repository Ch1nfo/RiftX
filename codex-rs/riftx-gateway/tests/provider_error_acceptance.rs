mod common;

use anyhow::Context;
use axum::http::StatusCode;
use codex_riftx_core::AUTO_MODE_CONFIRMATION;
use codex_riftx_core::AutoRun;
use codex_riftx_core::AutoRunState;
use codex_riftx_core::AutoStopReason;
use codex_riftx_core::Engagement;
use codex_riftx_core::LlmProtocol;
use codex_riftx_core::RiftxConfig;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use common::DaemonGuard;
use common::ensure_status;
use common::spawn_daemon;
use common::test_config;
use common::wait_for_daemon;
use serde_json::json;
use std::time::Duration;
use std::time::Instant;
use tempfile::TempDir;
use wiremock::Mock;
use wiremock::MockServer;
use wiremock::ResponseTemplate;
use wiremock::matchers::method;
use wiremock::matchers::path;

const COMPLETION_ATTEMPTS: usize = 600;
const POLL_INTERVAL: Duration = Duration::from_millis(100);
const SENSITIVE_MARKER: &str = "riftx-provider-error-secret-marker";

struct ProviderErrorCase {
    _temp: TempDir,
    server: MockServer,
    config: RiftxConfig,
    _daemon: DaemonGuard,
    client: LocalIpcClient,
    engagement: Engagement,
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn provider_authentication_error_pauses_auto_for_profile_repair() -> anyhow::Result<()> {
    run_terminal_provider_error_case(
        StatusCode::UNAUTHORIZED,
        AutoRunState::Paused,
        AutoStopReason::ProviderAuthentication,
    )
    .await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn provider_protocol_error_fails_auto_without_starting_another_turn() -> anyhow::Result<()> {
    run_terminal_provider_error_case(
        StatusCode::NOT_FOUND,
        AutoRunState::Failed,
        AutoStopReason::ProviderProtocolError,
    )
    .await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn provider_rate_limit_uses_bounded_backoff_and_failure_budget() -> anyhow::Result<()> {
    let case = start_provider_error_case(StatusCode::TOO_MANY_REQUESTS).await?;
    let started = Instant::now();
    let run = wait_for_stop(&case.client, &case.engagement.id, AutoRunState::Failed).await?;

    anyhow::ensure!(
        run.stop_reason == Some(AutoStopReason::ConsecutiveFailures),
        "unexpected Auto stop: {run:?}"
    );
    anyhow::ensure!(run.turns_started == run.config.limits.max_consecutive_failures);
    anyhow::ensure!(run.turns_completed == run.config.limits.max_consecutive_failures);
    anyhow::ensure!(run.tool_calls == 0);
    anyhow::ensure!(
        started.elapsed() >= Duration::from_secs(6),
        "rate-limit retries did not apply the expected bounded backoff"
    );
    let requests = case
        .server
        .received_requests()
        .await
        .context("provider error mock request recording is disabled")?;
    anyhow::ensure!(requests.len() >= run.turns_started as usize);
    assert_audit_is_sanitized(&case.config).await
}

async fn run_terminal_provider_error_case(
    provider_status: StatusCode,
    expected_state: AutoRunState,
    expected_reason: AutoStopReason,
) -> anyhow::Result<()> {
    let case = start_provider_error_case(provider_status).await?;
    let run = wait_for_stop(&case.client, &case.engagement.id, expected_state).await?;
    anyhow::ensure!(
        run.stop_reason == Some(expected_reason),
        "unexpected Auto stop: {run:?}"
    );
    anyhow::ensure!(run.turns_started == 1);
    anyhow::ensure!(run.turns_completed == 0);
    anyhow::ensure!(run.tool_calls == 0);

    tokio::time::sleep(Duration::from_millis(250)).await;
    let requests = case
        .server
        .received_requests()
        .await
        .context("provider error mock request recording is disabled")?;
    anyhow::ensure!(
        requests.len() == 1,
        "terminal Provider error made {} upstream requests instead of one",
        requests.len()
    );
    assert_audit_is_sanitized(&case.config).await
}

async fn start_provider_error_case(
    provider_status: StatusCode,
) -> anyhow::Result<ProviderErrorCase> {
    let temp = TempDir::new().context("create provider error acceptance directory")?;
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(provider_status.as_u16()).set_body_json(json!({
                "error": {
                    "message": format!("provider rejected request: {SENSITIVE_MARKER}"),
                    "type": "provider_error",
                },
            })),
        )
        .mount(&server)
        .await;

    let mut config = test_config(temp.path(), format!("{}/v1", server.uri()));
    let secondary = config
        .llm
        .profiles
        .get_mut("secondary")
        .context("secondary profile missing")?;
    secondary.protocol = LlmProtocol::ChatCompletions;
    secondary.model = "chat-provider-error-model".to_string();
    secondary.base_url = server.uri();
    secondary.timeout_seconds = 10;

    let config_path = temp.path().join("riftx.toml");
    tokio::fs::write(&config_path, toml::to_string(&config)?).await?;
    let mut daemon = spawn_daemon(&config_path)?;
    let client = LocalIpcClient::new(LocalIpcEndpoint::new(&config.daemon.ipc_dir));
    wait_for_daemon(&client, &mut daemon.child).await?;

    let response = client
        .post_json(
            "/v1/engagements",
            serde_json::to_vec(&json!({
                "name": format!("Provider error {provider_status}"),
                "objective": {
                    "summary": "Stop safely when the selected Provider is unusable",
                    "successCriteria": ["Preserve valid evidence"],
                    "structuredCriteria": [{
                        "id": "provider-error-evidence",
                        "description": "A valid evidence item exists",
                        "predicate": {
                            "type": "evidence",
                            "minimumItems": 1,
                            "reproductionRequired": false,
                        },
                    }],
                },
                "entryPoints": ["10.10.10.1"],
                "mode": "auto",
                "confirmation": AUTO_MODE_CONFIRMATION,
                "llmProfile": "secondary",
                "authorization": {
                    "network": {
                        "cidrs": ["10.10.10.0/24"],
                        "domains": [],
                        "ports": [],
                    },
                    "identities": [],
                    "capabilities": ["evidence.capture"],
                    "environment": "lab",
                    "window": {
                        "startsAt": null,
                        "expiresAt": 4_000_000_000_i64,
                    },
                },
            }))?,
        )
        .await?;
    anyhow::ensure!(response.status() == StatusCode::CREATED);
    let engagement: Engagement = serde_json::from_slice(&response.bytes().await?)?;

    let response = client
        .post(&format!("/v1/engagements/{}/activate", engagement.id))
        .await?;
    ensure_status(
        response,
        StatusCode::OK,
        "activate provider error engagement",
    )
    .await?;

    Ok(ProviderErrorCase {
        _temp: temp,
        server,
        config,
        _daemon: daemon,
        client,
        engagement,
    })
}

async fn assert_audit_is_sanitized(config: &RiftxConfig) -> anyhow::Result<()> {
    let audit = tokio::fs::read_to_string(&config.audit.jsonl_path).await?;
    anyhow::ensure!(!audit.contains(SENSITIVE_MARKER));
    Ok(())
}

async fn wait_for_stop(
    client: &LocalIpcClient,
    engagement_id: &str,
    expected_state: AutoRunState,
) -> anyhow::Result<AutoRun> {
    let path = format!("/v1/engagements/{engagement_id}/auto");
    let mut last_run = None;
    for _ in 0..COMPLETION_ATTEMPTS {
        let response = client.get(&path).await?;
        if response.status() == StatusCode::OK {
            let run: AutoRun = serde_json::from_slice(&response.bytes().await?)?;
            if run.state == expected_state {
                return Ok(run);
            }
            if !matches!(run.state, AutoRunState::Running | AutoRunState::Evaluating) {
                anyhow::bail!("Auto stopped in the wrong state: {run:?}");
            }
            last_run = Some(run);
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("Auto did not classify the terminal Provider error: {last_run:?}")
}
