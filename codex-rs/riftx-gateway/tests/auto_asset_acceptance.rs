mod common;

use anyhow::Context;
use axum::http::StatusCode;
use codex_riftx_core::AUTO_MODE_CONFIRMATION;
use codex_riftx_core::AutoRun;
use codex_riftx_core::AutoRunState;
use codex_riftx_core::AutoStopReason;
use codex_riftx_core::Engagement;
use codex_riftx_core::LlmProtocol;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use common::ensure_status;
use common::spawn_daemon;
use common::test_config;
use common::wait_for_daemon;
use serde_json::Value;
use serde_json::json;
use std::sync::Arc;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use std::time::Duration;
use tempfile::TempDir;
use wiremock::Mock;
use wiremock::MockServer;
use wiremock::ResponseTemplate;
use wiremock::matchers::method;
use wiremock::matchers::path;

const COMPLETION_ATTEMPTS: usize = 150;
const POLL_INTERVAL: Duration = Duration::from_millis(100);

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn auto_records_multiple_scoped_assets_before_reaching_the_evidence_goal()
-> anyhow::Result<()> {
    let temp = TempDir::new().context("create Auto asset acceptance directory")?;
    let server = MockServer::start().await;
    let (tools_directory, tool_path) = install_evidence_tool(temp.path()).await?;
    mount_multi_asset_sequence(&server, &tool_path).await?;

    let (client, daemon) = start_daemon(&temp, &server, vec![tools_directory]).await?;
    let engagement = create_auto_engagement(&client, "Auto multi-asset acceptance").await?;
    activate(&client, &engagement.id).await?;

    let run = wait_for_state(&client, &engagement.id, AutoRunState::Succeeded).await?;
    anyhow::ensure!(run.stop_reason == Some(AutoStopReason::SuccessCriteriaMet));
    anyhow::ensure!(run.turns_started == 3 && run.turns_completed == 3);
    anyhow::ensure!(run.tool_calls == 3);

    let report = report(&client, &engagement.id).await?;
    let assets = report["assets"].as_array().context("assets missing")?;
    anyhow::ensure!(
        assets.len() == 2
            && assets.iter().any(|asset| asset["value"] == "10.10.10.20")
            && assets.iter().any(|asset| asset["value"] == "10.10.10.30"),
        "structured assets missing: {report}"
    );
    anyhow::ensure!(
        report["evidence"]
            .as_array()
            .is_some_and(|evidence| evidence.len() == 1),
        "objective evidence missing: {report}"
    );

    let requests = server
        .received_requests()
        .await
        .context("asset mock request recording disabled")?;
    anyhow::ensure!(
        requests.len() == 6,
        "unexpected model requests: {requests:?}"
    );
    let third_request = String::from_utf8_lossy(&requests[2].body);
    let fifth_request = String::from_utf8_lossy(&requests[4].body);
    anyhow::ensure!(
        third_request.contains("10.10.10.20"),
        "second controller turn did not receive the first asset"
    );
    anyhow::ensure!(
        fifth_request.contains("10.10.10.20") && fifth_request.contains("10.10.10.30"),
        "result turn did not receive both structured assets"
    );

    drop(daemon);
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn auto_pauses_without_persisting_an_out_of_scope_asset() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create Auto scope acceptance directory")?;
    let server = MockServer::start().await;
    mount_out_of_scope_asset(&server).await?;

    let (client, daemon) = start_daemon(&temp, &server, Vec::new()).await?;
    let engagement = create_auto_engagement(&client, "Auto scope acceptance").await?;
    activate(&client, &engagement.id).await?;

    let run = wait_for_state(&client, &engagement.id, AutoRunState::NeedsInput).await?;
    anyhow::ensure!(run.stop_reason == Some(AutoStopReason::ScopeNeedsInput));
    anyhow::ensure!(run.turns_started == 1);
    anyhow::ensure!(run.tool_calls == 0);
    tokio::time::sleep(Duration::from_millis(300)).await;

    let report = report(&client, &engagement.id).await?;
    anyhow::ensure!(
        report["assets"].as_array().is_some_and(Vec::is_empty),
        "out-of-scope asset was persisted: {report}"
    );
    let approvals = client
        .get(&format!("/v1/engagements/{}/approvals", engagement.id))
        .await?;
    anyhow::ensure!(approvals.status() == StatusCode::OK);
    let approvals: Value = serde_json::from_slice(&approvals.bytes().await?)?;
    anyhow::ensure!(
        approvals.as_array().is_some_and(Vec::is_empty),
        "scope precheck created an approval: {approvals}"
    );
    let requests = server
        .received_requests()
        .await
        .context("scope mock request recording disabled")?;
    anyhow::ensure!(
        requests.len() == 1,
        "Auto continued after the scope pause: {requests:?}"
    );

    drop(daemon);
    Ok(())
}

async fn start_daemon(
    temp: &TempDir,
    server: &MockServer,
    tool_directories: Vec<std::path::PathBuf>,
) -> anyhow::Result<(LocalIpcClient, common::DaemonGuard)> {
    let mut config = test_config(temp.path(), format!("{}/v1", server.uri()));
    config.tools.directories = tool_directories;
    let profile = config
        .llm
        .profiles
        .get_mut("secondary")
        .context("secondary profile missing")?;
    profile.protocol = LlmProtocol::ChatCompletions;
    profile.model = "chat-auto-asset-model".to_string();
    profile.base_url = server.uri();
    profile.timeout_seconds = 30;

    let config_path = temp.path().join("riftx.toml");
    tokio::fs::write(&config_path, toml::to_string(&config)?).await?;
    let mut daemon = spawn_daemon(&config_path)?;
    let client = LocalIpcClient::new(LocalIpcEndpoint::new(&config.daemon.ipc_dir));
    wait_for_daemon(&client, &mut daemon.child).await?;
    Ok((client, daemon))
}

async fn create_auto_engagement(client: &LocalIpcClient, name: &str) -> anyhow::Result<Engagement> {
    let response = client
        .post_json(
            "/v1/engagements",
            serde_json::to_vec(&json!({
                "name": name,
                "objective": {
                    "summary": "Discover all in-scope assets, then preserve objective evidence",
                    "successCriteria": ["Preserve one artifact-backed evidence item"],
                    "structuredCriteria": [{
                        "id": "artifact-evidence",
                        "description": "One valid artifact-backed evidence item exists",
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
    Ok(serde_json::from_slice(&response.bytes().await?)?)
}

async fn activate(client: &LocalIpcClient, engagement_id: &str) -> anyhow::Result<()> {
    let response = client
        .post(&format!("/v1/engagements/{engagement_id}/activate"))
        .await?;
    ensure_status(response, StatusCode::OK, "activate Auto asset engagement").await
}

async fn wait_for_state(
    client: &LocalIpcClient,
    engagement_id: &str,
    expected: AutoRunState,
) -> anyhow::Result<AutoRun> {
    let path = format!("/v1/engagements/{engagement_id}/auto");
    let mut last_run = None;
    for _ in 0..COMPLETION_ATTEMPTS {
        let response = client.get(&path).await?;
        if response.status() == StatusCode::OK {
            let run: AutoRun = serde_json::from_slice(&response.bytes().await?)?;
            if run.state == expected {
                return Ok(run);
            }
            if matches!(
                run.state,
                AutoRunState::Succeeded
                    | AutoRunState::NeedsInput
                    | AutoRunState::Expired
                    | AutoRunState::BudgetExhausted
                    | AutoRunState::Failed
                    | AutoRunState::Killed
            ) {
                anyhow::bail!("Auto stopped in an unexpected state: {run:?}");
            }
            last_run = Some(run);
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("Auto did not reach {expected:?}: {last_run:?}")
}

async fn report(client: &LocalIpcClient, engagement_id: &str) -> anyhow::Result<Value> {
    let response = client
        .get(&format!(
            "/v1/engagements/{engagement_id}/report?format=json"
        ))
        .await?;
    anyhow::ensure!(response.status() == StatusCode::OK);
    Ok(serde_json::from_slice(&response.bytes().await?)?)
}

async fn mount_multi_asset_sequence(
    server: &MockServer,
    tool_path: &std::path::Path,
) -> anyhow::Result<()> {
    let first = asset_arguments("10.10.10.20")?;
    let second = asset_arguments("10.10.10.30")?;
    let evidence = serde_json::to_string(&json!({
        "cmd": format!("{} evidence", tool_path.display()),
        "yield_time_ms": 1_000,
        "max_output_tokens": 10_000,
    }))?;
    let calls = Arc::new(AtomicUsize::new(0));
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(move |_: &wiremock::Request| {
            let body = match calls.fetch_add(1, Ordering::SeqCst) {
                0 => chat_tool_call_sse("asset-one", "riftx_record_asset", &first),
                1 => chat_text_sse("asset one recorded"),
                2 => chat_tool_call_sse("asset-two", "riftx_record_asset", &second),
                3 => chat_text_sse("asset two recorded"),
                4 => chat_tool_call_sse("asset-evidence", "exec_command", &evidence),
                5 => chat_text_sse("objective evidence captured"),
                _ => return ResponseTemplate::new(500),
            };
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(body)
        })
        .up_to_n_times(6)
        .mount(server)
        .await;
    Ok(())
}

async fn mount_out_of_scope_asset(server: &MockServer) -> anyhow::Result<()> {
    let arguments = asset_arguments("10.20.20.20")?;
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(chat_tool_call_sse(
                    "out-of-scope-asset",
                    "riftx_record_asset",
                    &arguments,
                )),
        )
        .up_to_n_times(1)
        .mount(server)
        .await;
    Ok(())
}

fn asset_arguments(value: &str) -> anyhow::Result<String> {
    Ok(serde_json::to_string(&json!({
        "kind": "host",
        "value": value,
    }))?)
}

fn chat_tool_call_sse(call_id: &str, name: &str, arguments: &str) -> String {
    format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({
            "id": format!("chatcmpl-{call_id}"),
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        })
    )
}

fn chat_text_sse(text: &str) -> String {
    format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({
            "id": "chatcmpl-asset-summary",
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
        })
    )
}

async fn install_evidence_tool(
    root: &std::path::Path,
) -> anyhow::Result<(std::path::PathBuf, std::path::PathBuf)> {
    let directory = root.join("tools");
    tokio::fs::create_dir_all(&directory).await?;
    let tool_path = directory.join(tool_file_name());
    tokio::fs::write(&tool_path, tool_script()).await?;
    make_executable(&tool_path).await?;
    tokio::fs::write(
        directory.join(format!("{}.riftx.toml", tool_file_name())),
        concat!(
            "schema_version = 1\n",
            "capabilities = [\"evidence.capture\"]\n",
            "risk = \"low\"\n",
        ),
    )
    .await?;
    Ok((directory, tool_path))
}

#[cfg(not(windows))]
fn tool_file_name() -> &'static str {
    "auto-asset-evidence"
}

#[cfg(windows)]
fn tool_file_name() -> &'static str {
    "auto-asset-evidence.cmd"
}

#[cfg(not(windows))]
fn tool_script() -> &'static [u8] {
    b"#!/bin/sh\nset -eu\ntest \"$1\" = evidence\nprintf 'asset-evidence' > artifacts/asset-evidence.txt\n"
}

#[cfg(windows)]
fn tool_script() -> &'static [u8] {
    b"@echo off\r\nif not \"%1\"==\"evidence\" exit /b 2\r\necho|set /p=asset-evidence>artifacts\\asset-evidence.txt\r\n"
}

#[cfg(unix)]
async fn make_executable(path: &std::path::Path) -> anyhow::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = tokio::fs::metadata(path).await?.permissions();
    permissions.set_mode(0o755);
    tokio::fs::set_permissions(path, permissions).await?;
    Ok(())
}

#[cfg(windows)]
async fn make_executable(_path: &std::path::Path) -> anyhow::Result<()> {
    Ok(())
}
