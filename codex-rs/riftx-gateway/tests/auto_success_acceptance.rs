mod common;

use anyhow::Context;
use axum::http::StatusCode;
use codex_riftx_core::AUTO_MODE_CONFIRMATION;
use codex_riftx_core::AutoProgressAction;
use codex_riftx_core::AutoRun;
use codex_riftx_core::AutoRunState;
use codex_riftx_core::AutoStopReason;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
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
async fn auto_controller_replans_then_completes_with_artifact_backed_evidence() -> anyhow::Result<()>
{
    let temp = TempDir::new().context("create Auto success acceptance directory")?;
    let server = MockServer::start().await;
    let (tools_directory, tool_path) = install_evidence_tool(temp.path()).await?;
    let arguments = command_arguments(&tool_command(&tool_path, "evidence"))?;
    mount_success_sequence(&server, arguments).await;

    let mut config = test_config(temp.path(), format!("{}/v1", server.uri()));
    config.tools.directories = vec![tools_directory];
    let secondary = config
        .llm
        .profiles
        .get_mut("secondary")
        .context("secondary profile missing")?;
    secondary.protocol = LlmProtocol::ChatCompletions;
    secondary.model = "chat-auto-success-model".to_string();
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
                "name": "Auto evidence success acceptance",
                "objective": {
                    "summary": "Replan once, then collect one independently captured evidence artifact",
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
    anyhow::ensure!(
        response.status() == StatusCode::CREATED,
        "create Auto success engagement returned {}",
        response.status()
    );
    let engagement: Engagement = serde_json::from_slice(&response.bytes().await?)?;

    let response = client
        .post(&format!("/v1/engagements/{}/activate", engagement.id))
        .await?;
    ensure_status(response, StatusCode::OK, "activate Auto success engagement").await?;

    let run = wait_for_success(&client, &engagement.id).await?;
    anyhow::ensure!(
        run.state == AutoRunState::Succeeded,
        "unexpected Auto run: {run:?}"
    );
    anyhow::ensure!(run.stop_reason == Some(AutoStopReason::SuccessCriteriaMet));
    anyhow::ensure!(run.turns_started == 2);
    anyhow::ensure!(run.turns_completed == 2);
    anyhow::ensure!(run.tool_calls == 1);
    anyhow::ensure!(run.no_progress_turns == 1);
    anyhow::ensure!(
        run.last_progress_assessment
            .as_ref()
            .is_some_and(|assessment| !assessment.progressed
                && assessment.action == AutoProgressAction::Replan),
        "first no-progress turn did not trigger replanning: {run:?}"
    );
    anyhow::ensure!(
        run.last_goal_assessment.as_ref().is_some_and(|assessment| {
            assessment.succeeded
                && assessment.evidence_ids.len() == 1
                && assessment
                    .criteria
                    .iter()
                    .all(|criterion| criterion.satisfied)
        }),
        "Auto did not persist its successful evidence assessment: {run:?}"
    );

    let engagement_response = client
        .get(&format!("/v1/engagements/{}", engagement.id))
        .await?;
    anyhow::ensure!(engagement_response.status() == StatusCode::OK);
    let engagement: Engagement = serde_json::from_slice(&engagement_response.bytes().await?)?;
    anyhow::ensure!(engagement.status == EngagementStatus::Completed);

    let report_response = client
        .get(&format!(
            "/v1/engagements/{}/report?format=json",
            engagement.id
        ))
        .await?;
    anyhow::ensure!(report_response.status() == StatusCode::OK);
    let report: Value = serde_json::from_slice(&report_response.bytes().await?)?;
    anyhow::ensure!(
        report["schema"] == "riftx.report/v1"
            && report["generatedAt"]
                .as_i64()
                .is_some_and(|value| value > 0)
            && report["llmProfile"]["name"] == "secondary"
            && report["llmProfile"]["protocol"] == "chatCompletions",
        "versioned report metadata missing: {report}"
    );
    anyhow::ensure!(
        report["autoRun"]["state"] == "succeeded"
            && report["autoRun"]["stopReason"] == "successCriteriaMet"
            && report["autoRun"]["lastGoalAssessment"]["succeeded"] == true,
        "Auto outcome missing from report: {report}"
    );
    anyhow::ensure!(
        report["limitations"].as_array().is_some_and(|limitations| {
            limitations.iter().any(|limitation| {
                limitation
                    .as_str()
                    .is_some_and(|text| text.contains("not an OS-enforced network isolation"))
            })
        }),
        "scope limitation missing from report: {report}"
    );
    anyhow::ensure!(
        report["executions"]
            .as_array()
            .is_some_and(|executions| executions.len() == 1
                && executions.iter().all(|execution| {
                    execution["status"] == "completed" || execution["status"] == "failed"
                })),
        "Auto executions missing from report: {report}"
    );
    anyhow::ensure!(
        report["artifacts"]
            .as_array()
            .is_some_and(|artifacts| artifacts.len() == 1),
        "Auto artifacts missing from report: {report}"
    );
    anyhow::ensure!(
        report["evidence"].as_array().is_some_and(|evidence| {
            evidence.len() == 1
                && evidence.iter().all(|item| {
                    item["executionId"].as_str().is_some()
                        && item["artifactId"].as_str().is_some()
                        && item["summary"]
                            .as_str()
                            .is_some_and(|summary| !summary.is_empty())
                })
        }),
        "Auto evidence chain missing from report: {report}"
    );

    let requests = server
        .received_requests()
        .await
        .context("Auto success mock request recording is disabled")?;
    anyhow::ensure!(
        requests.len() == 3,
        "expected one planning turn and one tool turn: {requests:?}"
    );
    let bodies = requests
        .iter()
        .map(|request| serde_json::from_slice::<Value>(&request.body))
        .collect::<Result<Vec<_>, _>>()?;
    let first_prompt = latest_user_prompt(&bodies[0])?;
    let second_prompt = latest_user_prompt(&bodies[1])?;
    anyhow::ensure!(
        first_prompt.contains("RiftX Auto controller turn 1/20")
            && first_prompt.contains("Establish the current authorized state"),
        "first Auto success subgoal missing: {first_prompt}"
    );
    anyhow::ensure!(
        second_prompt.contains("RiftX Auto controller turn 2/20")
            && second_prompt.contains("Replan from the structured state"),
        "controller did not replan before the evidence turn: {second_prompt}"
    );
    Ok(())
}

async fn mount_success_sequence(server: &MockServer, arguments: String) {
    let calls = Arc::new(AtomicUsize::new(0));
    let response_calls = Arc::clone(&calls);
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(move |_: &wiremock::Request| {
            let body = match response_calls.fetch_add(1, Ordering::SeqCst) {
                0 => chat_text_sse(
                    "chatcmpl-auto-plan",
                    "No structured evidence exists yet; the controller should replan.",
                ),
                1 => chat_tool_call_sse("auto-evidence-call", &arguments),
                2 => chat_text_sse("chatcmpl-auto-summary", "Evidence artifact captured."),
                _ => return ResponseTemplate::new(500),
            };
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(body)
        })
        .up_to_n_times(3)
        .mount(server)
        .await;
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
                anyhow::bail!("Auto success run stopped unexpectedly: {run:?}");
            }
            if run.turns_started > 2 {
                let report_response = client
                    .get(&format!(
                        "/v1/engagements/{engagement_id}/report?format=json"
                    ))
                    .await?;
                let report = String::from_utf8_lossy(&report_response.bytes().await?).into_owned();
                anyhow::bail!("Auto scheduled an unexpected third turn: {run:?}; report: {report}");
            }
            last_run = Some(run);
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("Auto run did not satisfy its evidence goal: {last_run:?}")
}

fn latest_user_prompt(body: &Value) -> anyhow::Result<&str> {
    body["messages"]
        .as_array()
        .context("Chat request messages missing")?
        .iter()
        .rev()
        .find(|message| message["role"] == "user")
        .and_then(|message| message["content"].as_str())
        .context("latest Auto user prompt missing")
}

async fn install_evidence_tool(
    root: &std::path::Path,
) -> anyhow::Result<(std::path::PathBuf, std::path::PathBuf)> {
    let directory = root.join("tools");
    tokio::fs::create_dir_all(&directory).await?;
    let tool_path = directory.join(tool_file_name());
    tokio::fs::write(&tool_path, tool_script()).await?;
    make_executable(&tool_path).await?;
    let metadata_path = directory.join(format!("{}.riftx.toml", tool_file_name()));
    tokio::fs::write(
        metadata_path,
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
    "auto-evidence-tool"
}

#[cfg(windows)]
fn tool_file_name() -> &'static str {
    "auto-evidence-tool.cmd"
}

#[cfg(not(windows))]
fn tool_script() -> &'static [u8] {
    b"#!/bin/sh\nset -eu\ntest \"$1\" = evidence\nprintf 'auto-evidence' > artifacts/auto-evidence.txt\n"
}

#[cfg(windows)]
fn tool_script() -> &'static [u8] {
    b"@echo off\r\nif not \"%1\"==\"evidence\" exit /b 2\r\necho|set /p=auto-evidence>artifacts\\auto-evidence.txt\r\n"
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
