mod common;

use anyhow::Context;
use axum::http::StatusCode;
use codex_riftx_core::Engagement;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use common::ensure_status;
use common::spawn_daemon;
use common::test_config;
use common::wait_for_daemon;
use core_test_support::responses;
use serde_json::Value;
use serde_json::json;
use std::collections::HashSet;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use std::time::Duration;
use tempfile::TempDir;
use wiremock::Mock;
use wiremock::Request;
use wiremock::Respond;
use wiremock::ResponseTemplate;
use wiremock::matchers::method;
use wiremock::matchers::path;

const POLL_ATTEMPTS: usize = 200;
const POLL_INTERVAL: Duration = Duration::from_millis(100);

#[cfg(not(windows))]
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn pty_input_requires_a_bound_execution_intent() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create PTY acceptance directory")?;
    let server = responses::start_mock_server().await;
    let stdin_call_id = "pty-stdin-call";
    let stdin = "printf 'PTY-APPROVED'; exit\n";
    let process_id = Arc::new(Mutex::new(None));
    Mock::given(method("POST"))
        .and(path("/v1/responses"))
        .respond_with(PtyResponseSequence {
            calls: AtomicUsize::new(0),
            process_id: Arc::clone(&process_id),
            stdin: stdin.to_string(),
        })
        .expect(3)
        .mount(&server)
        .await;

    let config = test_config(temp.path(), format!("{}/v1", server.uri()));
    let config_path = temp.path().join("riftx.toml");
    tokio::fs::write(&config_path, toml::to_string(&config)?)
        .await
        .context("write PTY acceptance config")?;
    let mut daemon = spawn_daemon(&config_path)?;
    let client = LocalIpcClient::new(LocalIpcEndpoint::new(&config.daemon.ipc_dir));
    wait_for_daemon(&client, &mut daemon.child).await?;

    let engagement = create_engagement(&client).await?;
    let response = client
        .post(&format!("/v1/engagements/{}/activate", engagement.id))
        .await?;
    ensure_status(response, StatusCode::OK, "activate PTY engagement").await?;
    let response = client
        .post_json(
            &format!("/v1/engagements/{}/turns", engagement.id),
            serde_json::to_vec(&json!({"input": "Open a PTY and run the approved input."}))?,
        )
        .await?;
    ensure_status(response, StatusCode::ACCEPTED, "start PTY turn").await?;

    let mut seen = HashSet::new();
    let initial = wait_for_new_command_approval(&client, &engagement.id, &seen)
        .await
        .context("wait for initial PTY command approval")?;
    let initial_id = approval_id(&initial)?.to_string();
    seen.insert(initial_id.clone());
    let initial_intent = &initial["executionIntent"];
    anyhow::ensure!(initial_intent["mode"] == "redTeam", "wrong mode: {initial}");
    anyhow::ensure!(
        initial_intent["toolCallId"] == "pty-open-call",
        "initial tool-call binding missing: {initial}"
    );
    approve(&client, &initial_id).await?;

    let stdin_approval = wait_for_new_command_approval(&client, &engagement.id, &seen)
        .await
        .context("wait for PTY stdin approval")?;
    let stdin_approval_id = approval_id(&stdin_approval)?.to_string();
    let intent = &stdin_approval["executionIntent"];
    anyhow::ensure!(intent["mode"] == "redTeam", "wrong mode: {stdin_approval}");
    anyhow::ensure!(
        intent["threadId"].is_string(),
        "thread missing: {stdin_approval}"
    );
    anyhow::ensure!(
        intent["turnId"].is_string(),
        "turn missing: {stdin_approval}"
    );
    anyhow::ensure!(
        intent["toolCallId"] == stdin_call_id,
        "stdin tool-call binding missing: {stdin_approval}"
    );
    let display_argv = intent["displayArgv"]
        .as_array()
        .context("stdin displayArgv missing")?;
    anyhow::ensure!(
        display_argv.iter().any(|arg| arg == "<pty-stdin>"),
        "PTY marker missing: {stdin_approval}"
    );
    let expected_session = *process_id
        .lock()
        .map_err(|_| anyhow::anyhow!("process id lock poisoned"))?;
    let expected_session = expected_session.context("dynamic PTY session id missing")?;
    anyhow::ensure!(
        display_argv
            .iter()
            .any(|arg| arg == &format!("session={expected_session}")),
        "PTY session binding missing: {stdin_approval}"
    );
    anyhow::ensure!(
        display_argv.iter().any(|arg| arg == stdin),
        "exact PTY input missing: {stdin_approval}"
    );
    anyhow::ensure!(
        intent["risk"] != "low",
        "PTY input was downgraded: {stdin_approval}"
    );
    let binding = intent["bindingSha256"]
        .as_str()
        .context("stdin approval binding missing")?;
    anyhow::ensure!(binding.len() == 64, "invalid binding: {stdin_approval}");
    anyhow::ensure!(
        binding != initial_intent["bindingSha256"],
        "PTY input reused the original command binding"
    );
    approve(&client, &stdin_approval_id).await?;

    let report = wait_for_completed_report(&client, &engagement.id).await?;
    let executions = report["executions"]
        .as_array()
        .context("report executions missing")?;
    anyhow::ensure!(
        executions.len() == 1,
        "PTY approval created a phantom execution: {executions:?}"
    );
    let execution = &executions[0];
    anyhow::ensure!(
        execution["status"] == "completed",
        "PTY did not complete: {execution}"
    );
    anyhow::ensure!(
        execution["stdinBytes"] == stdin.len() as u64,
        "PTY stdin was not recorded on the original execution: {execution}"
    );
    anyhow::ensure!(
        execution["stdinSha256"].is_string(),
        "PTY stdin hash missing: {execution}"
    );
    Ok(())
}

struct PtyResponseSequence {
    calls: AtomicUsize,
    process_id: Arc<Mutex<Option<i32>>>,
    stdin: String,
}

impl Respond for PtyResponseSequence {
    fn respond(&self, request: &Request) -> ResponseTemplate {
        let body = match self.calls.fetch_add(1, Ordering::SeqCst) {
            0 => responses::sse(vec![
                responses::ev_response_created("pty-response-1"),
                responses::ev_function_call(
                    "pty-open-call",
                    "exec_command",
                    &json!({
                        "cmd": "/bin/bash -i",
                        "yield_time_ms": 200,
                        "tty": true,
                    })
                    .to_string(),
                ),
                responses::ev_completed("pty-response-1"),
            ]),
            1 => {
                let Ok(body) = request.body_json::<Value>() else {
                    return ResponseTemplate::new(500);
                };
                let Ok(process_id) = process_id_from_tool_output(&body, "pty-open-call") else {
                    return ResponseTemplate::new(500);
                };
                let Ok(mut recorded_process_id) = self.process_id.lock() else {
                    return ResponseTemplate::new(500);
                };
                *recorded_process_id = Some(process_id);
                responses::sse(vec![
                    responses::ev_response_created("pty-response-2"),
                    responses::ev_function_call(
                        "pty-stdin-call",
                        "write_stdin",
                        &json!({
                            "chars": self.stdin,
                            "session_id": process_id,
                            "yield_time_ms": 1_000,
                        })
                        .to_string(),
                    ),
                    responses::ev_completed("pty-response-2"),
                ])
            }
            2 => responses::sse(vec![
                responses::ev_response_created("pty-response-3"),
                responses::ev_assistant_message("pty-message-1", "PTY execution complete."),
                responses::ev_completed("pty-response-3"),
            ]),
            _ => return ResponseTemplate::new(500),
        };
        ResponseTemplate::new(200)
            .insert_header("content-type", "text/event-stream")
            .set_body_string(body)
    }
}

fn process_id_from_tool_output(body: &Value, call_id: &str) -> anyhow::Result<i32> {
    let output = body["input"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|item| item["type"] == "function_call_output" && item["call_id"] == call_id)
        .and_then(|item| item["output"].as_str())
        .context("exec tool output missing")?;
    let (_, session) = output
        .split_once("session ID ")
        .context("session id marker missing")?;
    session
        .chars()
        .take_while(char::is_ascii_digit)
        .collect::<String>()
        .parse::<i32>()
        .context("invalid numeric session id")
}

async fn create_engagement(client: &LocalIpcClient) -> anyhow::Result<Engagement> {
    let response = client
        .post_json(
            "/v1/engagements",
            serde_json::to_vec(&json!({
                "name": "PTY acceptance",
                "objective": {
                    "summary": "Verify interactive terminal policy routing",
                    "successCriteria": ["Record one approved PTY execution"],
                    "structuredCriteria": [],
                },
                "entryPoints": ["10.10.10.1"],
                "mode": "redTeam",
                "llmProfile": "default",
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
        "create PTY engagement returned {}: {}",
        response.status(),
        String::from_utf8_lossy(&response.bytes().await?)
    );
    Ok(serde_json::from_slice(&response.bytes().await?)?)
}

async fn wait_for_new_command_approval(
    client: &LocalIpcClient,
    engagement_id: &str,
    seen: &HashSet<String>,
) -> anyhow::Result<Value> {
    for _ in 0..POLL_ATTEMPTS {
        let response = client
            .get(&format!("/v1/engagements/{engagement_id}/approvals"))
            .await?;
        if response.status() == StatusCode::OK {
            let approvals: Vec<Value> = serde_json::from_slice(&response.bytes().await?)?;
            if let Some(approval) = approvals.into_iter().find(|approval| {
                approval["kind"] == "command"
                    && approval_id(approval).is_ok_and(|id| !seen.contains(id))
            }) {
                return Ok(approval);
            }
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("timed out waiting for a new command approval")
}

fn approval_id(approval: &Value) -> anyhow::Result<&str> {
    approval["id"].as_str().context("approval id missing")
}

async fn approve(client: &LocalIpcClient, approval_id: &str) -> anyhow::Result<()> {
    let response = client
        .post_json(
            &format!("/v1/approvals/{approval_id}/decision"),
            serde_json::to_vec(&json!({"decision": "approve"}))?,
        )
        .await?;
    anyhow::ensure!(
        response.status() == StatusCode::NO_CONTENT || response.status() == StatusCode::OK,
        "approve command returned {}",
        response.status()
    );
    Ok(())
}

async fn wait_for_completed_report(
    client: &LocalIpcClient,
    engagement_id: &str,
) -> anyhow::Result<Value> {
    let path = format!("/v1/engagements/{engagement_id}/report?format=json");
    let mut last_report = Value::Null;
    for _ in 0..POLL_ATTEMPTS {
        let response = client.get(&path).await?;
        if response.status() == StatusCode::OK {
            last_report = serde_json::from_slice(&response.bytes().await?)?;
            let completed = last_report["executions"]
                .as_array()
                .is_some_and(|executions| {
                    executions.len() == 1 && executions[0]["status"] == "completed"
                });
            if completed {
                return Ok(last_report);
            }
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("PTY turn did not complete: {last_report}")
}
