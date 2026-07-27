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
async fn auto_stops_after_the_configured_turn_budget() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create turn budget acceptance directory")?;
    let server = MockServer::start().await;
    mount_text_response(&server, Duration::ZERO).await;
    let limits = auto_limits(1, 3, 30, 5);
    let (client, engagement, _daemon) = start_case(&temp, &server, Vec::new(), limits).await?;

    let run = wait_for_budget_stop(&client, &engagement.id).await?;

    anyhow::ensure!(run.stop_reason == Some(AutoStopReason::TurnBudgetExhausted));
    anyhow::ensure!(run.turns_started == 1 && run.turns_completed == 1);
    anyhow::ensure!(run.tool_calls == 0);
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn auto_stops_after_the_configured_tool_budget() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create tool budget acceptance directory")?;
    let server = MockServer::start().await;
    let (tools_directory, tool_path) = install_noop_tool(temp.path()).await?;
    mount_tool_sequence(&server, command_arguments(&tool_path)?).await;
    let limits = auto_limits(3, 1, 30, 5);
    let (client, engagement, _daemon) =
        start_case(&temp, &server, vec![tools_directory], limits).await?;

    let run = wait_for_budget_stop(&client, &engagement.id).await?;

    anyhow::ensure!(run.stop_reason == Some(AutoStopReason::ToolBudgetExhausted));
    anyhow::ensure!(run.turns_started == 1 && run.turns_completed == 1);
    anyhow::ensure!(run.tool_calls == 1);
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn auto_interrupts_an_active_turn_at_the_wall_clock_budget() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create wall-clock budget acceptance directory")?;
    let server = MockServer::start().await;
    mount_text_response(&server, Duration::from_secs(10)).await;
    let limits = auto_limits(3, 3, 1, 1);
    let (client, engagement, _daemon) = start_case(&temp, &server, Vec::new(), limits).await?;

    let run = wait_for_budget_stop(&client, &engagement.id).await?;

    anyhow::ensure!(
        run.stop_reason == Some(AutoStopReason::WallClockBudgetExhausted),
        "unexpected wall-clock stop: {run:?}"
    );
    anyhow::ensure!(run.turns_started == 1);
    anyhow::ensure!(run.turns_completed == 0);
    Ok(())
}

fn auto_limits(
    max_turns: u32,
    max_tool_calls: u32,
    max_wall_clock_seconds: u64,
    max_single_command_seconds: u64,
) -> Value {
    json!({
        "maxTurns": max_turns,
        "maxToolCalls": max_tool_calls,
        "maxWallClockSeconds": max_wall_clock_seconds,
        "maxSingleCommandSeconds": max_single_command_seconds,
        "maxConsecutiveFailures": 2,
        "noProgressWindow": 1,
        "maxModelTokensOrCost": null,
    })
}

async fn start_case(
    temp: &TempDir,
    server: &MockServer,
    tool_directories: Vec<std::path::PathBuf>,
    auto_limits: Value,
) -> anyhow::Result<(LocalIpcClient, Engagement, common::DaemonGuard)> {
    let mut config = test_config(temp.path(), format!("{}/v1", server.uri()));
    config.tools.directories = tool_directories;
    let secondary = config
        .llm
        .profiles
        .get_mut("secondary")
        .context("secondary profile missing")?;
    secondary.protocol = LlmProtocol::ChatCompletions;
    secondary.model = "chat-auto-budget-model".to_string();
    secondary.base_url = server.uri();
    secondary.timeout_seconds = 30;

    let config_path = temp.path().join("riftx.toml");
    tokio::fs::write(&config_path, toml::to_string(&config)?).await?;
    let mut daemon = spawn_daemon(&config_path)?;
    let client = LocalIpcClient::new(LocalIpcEndpoint::new(&config.daemon.ipc_dir));
    wait_for_daemon(&client, &mut daemon.child).await?;

    let response = client
        .post_json(
            "/v1/engagements",
            serde_json::to_vec(&json!({
                "name": "Auto budget acceptance",
                "objective": {
                    "summary": "Remain bounded while looking for objective evidence",
                    "successCriteria": ["Preserve one objective evidence item"],
                    "structuredCriteria": [{
                        "id": "objective-evidence",
                        "description": "One objective evidence item exists",
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
                "autoLimits": auto_limits,
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
    ensure_status(response, StatusCode::OK, "activate budget engagement").await?;
    Ok((client, engagement, daemon))
}

async fn mount_text_response(server: &MockServer, delay: Duration) {
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_delay(delay)
                .set_body_string(chat_text_sse()),
        )
        .mount(server)
        .await;
}

async fn mount_tool_sequence(server: &MockServer, arguments: String) {
    let calls = Arc::new(AtomicUsize::new(0));
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(move |_: &wiremock::Request| {
            let body = match calls.fetch_add(1, Ordering::SeqCst) {
                0 => chat_tool_call_sse(&arguments),
                1 => chat_text_sse(),
                _ => return ResponseTemplate::new(500),
            };
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(body)
        })
        .up_to_n_times(2)
        .mount(server)
        .await;
}

fn chat_text_sse() -> String {
    format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({
            "id": "chatcmpl-budget-text",
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": "No objective evidence yet."},
                "finish_reason": "stop",
            }],
        })
    )
}

fn chat_tool_call_sse(arguments: &str) -> String {
    format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({
            "id": "chatcmpl-budget-tool",
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "budget-tool-call",
                        "type": "function",
                        "function": {
                            "name": "exec_command",
                            "arguments": arguments,
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        })
    )
}

fn command_arguments(tool_path: &std::path::Path) -> anyhow::Result<String> {
    Ok(serde_json::to_string(&json!({
        "cmd": tool_path.display().to_string(),
        "yield_time_ms": 1_000,
        "max_output_tokens": 10_000,
    }))?)
}

async fn wait_for_budget_stop(
    client: &LocalIpcClient,
    engagement_id: &str,
) -> anyhow::Result<AutoRun> {
    let path = format!("/v1/engagements/{engagement_id}/auto");
    let mut last_run = None;
    for _ in 0..COMPLETION_ATTEMPTS {
        let response = client.get(&path).await?;
        if response.status() == StatusCode::OK {
            let run: AutoRun = serde_json::from_slice(&response.bytes().await?)?;
            if run.state == AutoRunState::BudgetExhausted {
                return Ok(run);
            }
            if matches!(
                run.state,
                AutoRunState::NeedsInput
                    | AutoRunState::Succeeded
                    | AutoRunState::Expired
                    | AutoRunState::Failed
                    | AutoRunState::Killed
            ) {
                anyhow::bail!("Auto stopped outside its budget state: {run:?}");
            }
            last_run = Some(run);
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("Auto did not stop at its configured budget: {last_run:?}")
}

async fn install_noop_tool(
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
    "auto-budget-noop"
}

#[cfg(windows)]
fn tool_file_name() -> &'static str {
    "auto-budget-noop.cmd"
}

#[cfg(not(windows))]
fn tool_script() -> &'static [u8] {
    b"#!/bin/sh\nset -eu\nprintf 'no objective evidence'\n"
}

#[cfg(windows)]
fn tool_script() -> &'static [u8] {
    b"@echo off\r\necho no objective evidence\r\n"
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
