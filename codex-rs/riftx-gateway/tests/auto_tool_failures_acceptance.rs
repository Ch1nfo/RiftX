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

const COMPLETION_ATTEMPTS: usize = 200;
const POLL_INTERVAL: Duration = Duration::from_millis(100);
const MISSING_TOOL: &str = "riftx-definitely-missing-tool";

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn auto_marks_a_missing_tool_unavailable_and_replans_once() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create missing tool acceptance directory")?;
    let server = MockServer::start().await;
    let (tools_directory, tool_path) = install_test_tool(temp.path()).await?;
    let missing_arguments = command_arguments(&format!("{MISSING_TOOL} --scan"))?;
    let evidence_arguments = command_arguments(&tool_command(&tool_path, "evidence"))?;
    mount_missing_tool_sequence(&server, missing_arguments, evidence_arguments).await;
    let (client, engagement, _daemon) = start_case(&temp, &server, tools_directory, None).await?;

    let run = wait_for_success(&client, &engagement.id).await?;

    anyhow::ensure!(run.stop_reason == Some(AutoStopReason::SuccessCriteriaMet));
    anyhow::ensure!(run.turns_started == 2 && run.turns_completed == 2);
    anyhow::ensure!(run.tool_calls == 2);
    anyhow::ensure!(run.unavailable_tools == vec![MISSING_TOOL.to_string()]);

    let requests = server
        .received_requests()
        .await
        .context("missing tool request recording disabled")?;
    anyhow::ensure!(requests.len() == 4, "unexpected requests: {requests:?}");
    let bodies = request_bodies(&requests)?;
    let replan_prompt = latest_user_prompt(&bodies[2])?;
    anyhow::ensure!(
        replan_prompt.contains("Replan from the structured state")
            && replan_prompt.contains(&format!("Unavailable tools: {MISSING_TOOL}"))
            && replan_prompt.contains("Do not retry unavailable tools"),
        "missing tool was not injected into the replan prompt: {replan_prompt}"
    );
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn auto_interrupts_timed_out_tools_and_records_operational_evidence() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create timeout acceptance directory")?;
    let server = MockServer::start().await;
    let (tools_directory, tool_path) = install_test_tool(temp.path()).await?;
    let timeout_arguments = command_arguments(&tool_command(&tool_path, "timeout"))?;
    let evidence_arguments = command_arguments(&tool_command(&tool_path, "evidence"))?;
    mount_timeout_sequence(&server, timeout_arguments, evidence_arguments).await;
    let auto_limits = json!({
        "maxTurns": 3,
        "maxToolCalls": 3,
        "maxWallClockSeconds": 30,
        "maxSingleCommandSeconds": 1,
        "maxConsecutiveFailures": 2,
        "noProgressWindow": 3,
        "maxModelTokensOrCost": null,
    });
    let (client, engagement, _daemon) =
        start_case(&temp, &server, tools_directory, Some(auto_limits)).await?;

    let run = wait_for_success(&client, &engagement.id).await?;

    anyhow::ensure!(run.stop_reason == Some(AutoStopReason::SuccessCriteriaMet));
    anyhow::ensure!(run.turns_started == 2 && run.turns_completed == 2);
    anyhow::ensure!(run.tool_calls == 2);
    anyhow::ensure!(run.consecutive_failures == 0);
    let report = report(&client, &engagement.id).await?;
    let evidence = report["evidence"]
        .as_array()
        .context("report evidence missing")?;
    anyhow::ensure!(
        evidence.iter().any(|item| {
            item["purpose"] == "operational"
                && item["summary"]
                    .as_str()
                    .is_some_and(|summary| summary.contains("timeout of 1 seconds"))
        }),
        "timeout operational evidence missing: {report}"
    );
    anyhow::ensure!(
        evidence
            .iter()
            .any(|item| item["purpose"] == "objective" && item["artifactId"].is_string()),
        "objective evidence missing after timeout replan: {report}"
    );
    anyhow::ensure!(
        run.last_goal_assessment.as_ref().is_some_and(|assessment| {
            assessment.succeeded && assessment.evidence_ids.len() == 1
        }),
        "operational evidence incorrectly satisfied the objective: {run:?}"
    );
    let executions = report["executions"]
        .as_array()
        .context("report executions missing")?;
    anyhow::ensure!(
        executions
            .iter()
            .any(|execution| execution["status"] == "interrupted"),
        "timed out execution was not interrupted: {report}"
    );
    Ok(())
}

async fn start_case(
    temp: &TempDir,
    server: &MockServer,
    tools_directory: std::path::PathBuf,
    auto_limits: Option<Value>,
) -> anyhow::Result<(LocalIpcClient, Engagement, common::DaemonGuard)> {
    let mut config = test_config(temp.path(), format!("{}/v1", server.uri()));
    config.tools.directories = vec![tools_directory];
    let secondary = config
        .llm
        .profiles
        .get_mut("secondary")
        .context("secondary profile missing")?;
    secondary.protocol = LlmProtocol::ChatCompletions;
    secondary.model = "chat-auto-tool-failure-model".to_string();
    secondary.base_url = server.uri();
    secondary.timeout_seconds = 30;

    let config_path = temp.path().join("riftx.toml");
    tokio::fs::write(&config_path, toml::to_string(&config)?).await?;
    let mut daemon = spawn_daemon(&config_path)?;
    let client = LocalIpcClient::new(LocalIpcEndpoint::new(&config.daemon.ipc_dir));
    wait_for_daemon(&client, &mut daemon.child).await?;

    let mut body = json!({
        "name": "Auto tool failure acceptance",
        "objective": {
            "summary": "Recover from one tool failure and preserve objective evidence",
            "successCriteria": ["Preserve one artifact-backed objective evidence item"],
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
    });
    if let Some(auto_limits) = auto_limits {
        body["autoLimits"] = auto_limits;
    }
    let response = client
        .post_json("/v1/engagements", serde_json::to_vec(&body)?)
        .await?;
    anyhow::ensure!(
        response.status() == StatusCode::CREATED,
        "create engagement returned {}: {}",
        response.status(),
        String::from_utf8_lossy(&response.bytes().await?)
    );
    let engagement: Engagement = serde_json::from_slice(&response.bytes().await?)?;
    let response = client
        .post(&format!("/v1/engagements/{}/activate", engagement.id))
        .await?;
    ensure_status(response, StatusCode::OK, "activate Auto engagement").await?;
    Ok((client, engagement, daemon))
}

async fn mount_missing_tool_sequence(
    server: &MockServer,
    missing_arguments: String,
    evidence_arguments: String,
) {
    let calls = Arc::new(AtomicUsize::new(0));
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(move |_: &wiremock::Request| {
            let body = match calls.fetch_add(1, Ordering::SeqCst) {
                0 => chat_tool_call_sse("missing-tool-call", &missing_arguments),
                1 => chat_text_sse("missing-summary", "The requested tool is unavailable."),
                2 => chat_tool_call_sse("evidence-call", &evidence_arguments),
                3 => chat_text_sse("evidence-summary", "Objective evidence captured."),
                _ => return ResponseTemplate::new(500),
            };
            sse_response(body)
        })
        .up_to_n_times(4)
        .mount(server)
        .await;
}

async fn mount_timeout_sequence(
    server: &MockServer,
    timeout_arguments: String,
    evidence_arguments: String,
) {
    let calls = Arc::new(AtomicUsize::new(0));
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(move |_: &wiremock::Request| {
            let body = match calls.fetch_add(1, Ordering::SeqCst) {
                0 => chat_tool_call_sse("timeout-call", &timeout_arguments),
                1 => chat_tool_call_sse("recovery-evidence-call", &evidence_arguments),
                2 => chat_text_sse("recovery-summary", "Objective evidence captured."),
                _ => return ResponseTemplate::new(500),
            };
            sse_response(body)
        })
        .up_to_n_times(3)
        .mount(server)
        .await;
}

fn sse_response(body: String) -> ResponseTemplate {
    ResponseTemplate::new(200)
        .insert_header("content-type", "text/event-stream")
        .set_body_string(body)
}

fn chat_tool_call_sse(call_id: &str, arguments: &str) -> String {
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

fn chat_text_sse(id: &str, text: &str) -> String {
    format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({
            "id": id,
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
        })
    )
}

fn command_arguments(command: &str) -> anyhow::Result<String> {
    Ok(serde_json::to_string(&json!({
        "cmd": command,
        "yield_time_ms": 1_000,
        "max_output_tokens": 10_000,
    }))?)
}

fn request_bodies(requests: &[wiremock::Request]) -> anyhow::Result<Vec<Value>> {
    requests
        .iter()
        .map(|request| serde_json::from_slice(&request.body).map_err(Into::into))
        .collect()
}

fn latest_user_prompt(body: &Value) -> anyhow::Result<&str> {
    body["messages"]
        .as_array()
        .context("Chat messages missing")?
        .iter()
        .rev()
        .find(|message| message["role"] == "user")
        .and_then(|message| message["content"].as_str())
        .context("latest Auto user prompt missing")
}

async fn wait_for_success(client: &LocalIpcClient, engagement_id: &str) -> anyhow::Result<AutoRun> {
    let path = format!("/v1/engagements/{engagement_id}/auto");
    let mut last_run = None;
    for _ in 0..COMPLETION_ATTEMPTS {
        let response = client.get(&path).await?;
        if response.status() == StatusCode::OK {
            let run: AutoRun = serde_json::from_slice(&response.bytes().await?)?;
            if run.state == AutoRunState::Succeeded {
                return Ok(run);
            }
            if matches!(
                run.state,
                AutoRunState::NeedsInput
                    | AutoRunState::Expired
                    | AutoRunState::BudgetExhausted
                    | AutoRunState::Failed
                    | AutoRunState::Killed
            ) {
                anyhow::bail!("Auto tool failure run stopped unexpectedly: {run:?}");
            }
            last_run = Some(run);
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("Auto tool failure run did not recover: {last_run:?}")
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

async fn install_test_tool(
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

fn tool_command(tool_path: &std::path::Path, argument: &str) -> String {
    format!("{} {argument}", tool_path.display())
}

#[cfg(not(windows))]
fn tool_file_name() -> &'static str {
    "auto-tool-failure-test"
}

#[cfg(windows)]
fn tool_file_name() -> &'static str {
    "auto-tool-failure-test.cmd"
}

#[cfg(not(windows))]
fn tool_script() -> &'static [u8] {
    b"#!/bin/sh\nset -eu\ncase \"$1\" in\n  timeout) sleep 30 ;;\n  evidence) printf 'objective-evidence' > artifacts/objective-evidence.txt ;;\n  *) exit 2 ;;\nesac\n"
}

#[cfg(windows)]
fn tool_script() -> &'static [u8] {
    b"@echo off\r\nif \"%1\"==\"timeout\" ping 127.0.0.1 -n 31 >nul\r\nif \"%1\"==\"evidence\" echo|set /p=objective-evidence>artifacts\\objective-evidence.txt\r\n"
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
