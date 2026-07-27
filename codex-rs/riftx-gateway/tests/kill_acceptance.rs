mod common;

use anyhow::Context;
use axum::http::StatusCode;
use codex_riftx_core::Engagement;
use codex_riftx_ipc::DaemonControlStatus;
use codex_riftx_ipc::DaemonPauseReason;
use codex_riftx_ipc::DaemonRunState;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use common::ensure_status;
use common::spawn_daemon;
use common::test_config;
use common::wait_for_daemon;
use core_test_support::responses;
use serde_json::Value;
use serde_json::json;
use std::path::Path;
use std::process::Command;
use std::sync::Arc;
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

#[cfg(unix)]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn system_kill_terminates_runtime_process_tree_without_desktop() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create kill acceptance directory")?;
    let server = responses::start_mock_server().await;
    let model_calls = Arc::new(AtomicUsize::new(0));
    Mock::given(method("POST"))
        .and(path("/v1/responses"))
        .respond_with(KillResponseSequence {
            calls: Arc::clone(&model_calls),
        })
        .up_to_n_times(2)
        .mount(&server)
        .await;

    let config = test_config(temp.path(), format!("{}/v1", server.uri()));
    let config_path = temp.path().join("riftx.toml");
    tokio::fs::write(&config_path, toml::to_string(&config)?).await?;
    let mut daemon = spawn_daemon(&config_path)?;
    let client = LocalIpcClient::new(LocalIpcEndpoint::new(&config.daemon.ipc_dir));
    wait_for_daemon(&client, &mut daemon.child).await?;

    let engagement = create_engagement(&client).await?;
    let response = client
        .post(&format!("/v1/engagements/{}/activate", engagement.id))
        .await?;
    ensure_status(response, StatusCode::OK, "activate kill engagement").await?;
    let response = client
        .post_json(
            &format!("/v1/engagements/{}/turns", engagement.id),
            serde_json::to_vec(&json!({"input": "Start the long-running process tree."}))?,
        )
        .await?;
    ensure_status(response, StatusCode::ACCEPTED, "start kill turn").await?;

    let approval = wait_for_command_approval(&client, &engagement.id).await?;
    let approval_id = approval["id"].as_str().context("approval id missing")?;
    let decision = client
        .post_json(
            &format!("/v1/approvals/{approval_id}/decision"),
            serde_json::to_vec(&json!({"decision": "approve"}))?,
        )
        .await?;
    anyhow::ensure!(
        decision.status() == StatusCode::NO_CONTENT || decision.status() == StatusCode::OK,
        "approve kill command returned {}",
        decision.status()
    );

    let pid_path = config
        .daemon
        .workspace_root
        .join(&engagement.id)
        .join("kill-descendant.pid");
    let descendant_pid = wait_for_pid(&pid_path).await?;
    wait_for_model_calls(&model_calls, 2).await?;
    anyhow::ensure!(
        process_exists(descendant_pid),
        "descendant exited before kill"
    );

    let response = tokio::time::timeout(Duration::from_secs(10), client.post("/v1/system/kill"))
        .await
        .context("daemon kill endpoint did not return")??;
    let status_code = response.status();
    let body = response.bytes().await?;
    anyhow::ensure!(status_code == StatusCode::OK, "kill returned {status_code}");
    let control: DaemonControlStatus = serde_json::from_slice(&body)?;
    anyhow::ensure!(
        (control.state, control.reason)
            == (DaemonRunState::Paused, Some(DaemonPauseReason::KillSwitch)),
        "unexpected daemon control state: {control:?}"
    );

    wait_for_process_exit(descendant_pid).await?;
    let report = wait_for_interrupted_report(&client, &engagement.id).await?;
    anyhow::ensure!(
        report["engagement"]["status"] == "interrupted",
        "kill did not interrupt engagement: {report}"
    );
    let executions = report["executions"]
        .as_array()
        .context("kill report executions missing")?;
    anyhow::ensure!(
        executions.len() == 1,
        "unexpected kill executions: {executions:?}"
    );
    anyhow::ensure!(
        executions[0]["status"] == "interrupted",
        "kill did not interrupt execution: {}",
        executions[0]
    );
    Ok(())
}

#[cfg(unix)]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn engagement_interrupt_terminates_process_tree_and_remains_resumable() -> anyhow::Result<()>
{
    let temp = TempDir::new().context("create interrupt process acceptance directory")?;
    let server = responses::start_mock_server().await;
    let model_calls = Arc::new(AtomicUsize::new(0));
    Mock::given(method("POST"))
        .and(path("/v1/responses"))
        .respond_with(KillResponseSequence {
            calls: Arc::clone(&model_calls),
        })
        .up_to_n_times(2)
        .mount(&server)
        .await;

    let config = test_config(temp.path(), format!("{}/v1", server.uri()));
    let config_path = temp.path().join("riftx.toml");
    tokio::fs::write(&config_path, toml::to_string(&config)?).await?;
    let mut daemon = spawn_daemon(&config_path)?;
    let client = LocalIpcClient::new(LocalIpcEndpoint::new(&config.daemon.ipc_dir));
    wait_for_daemon(&client, &mut daemon.child).await?;

    let engagement = create_engagement(&client).await?;
    let response = client
        .post(&format!("/v1/engagements/{}/activate", engagement.id))
        .await?;
    ensure_status(response, StatusCode::OK, "activate interrupt engagement").await?;
    let response = client
        .post_json(
            &format!("/v1/engagements/{}/turns", engagement.id),
            serde_json::to_vec(&json!({"input": "Start the interrupt process tree."}))?,
        )
        .await?;
    ensure_status(response, StatusCode::ACCEPTED, "start interrupt turn").await?;

    let approval = wait_for_command_approval(&client, &engagement.id).await?;
    let approval_id = approval["id"].as_str().context("approval id missing")?;
    let decision = client
        .post_json(
            &format!("/v1/approvals/{approval_id}/decision"),
            serde_json::to_vec(&json!({"decision": "approve"}))?,
        )
        .await?;
    anyhow::ensure!(
        decision.status() == StatusCode::NO_CONTENT || decision.status() == StatusCode::OK,
        "approve interrupt command returned {}",
        decision.status()
    );

    let pid_path = config
        .daemon
        .workspace_root
        .join(&engagement.id)
        .join("kill-descendant.pid");
    let descendant_pid = wait_for_pid(&pid_path).await?;
    wait_for_model_calls(&model_calls, 2).await?;
    anyhow::ensure!(
        process_exists(descendant_pid),
        "descendant exited before interrupt"
    );

    let response = tokio::time::timeout(
        Duration::from_secs(10),
        client.post(&format!("/v1/engagements/{}/interrupt", engagement.id)),
    )
    .await
    .context("engagement interrupt endpoint did not return")??;
    let status_code = response.status();
    let body = response.bytes().await?;
    anyhow::ensure!(
        status_code == StatusCode::OK,
        "interrupt returned {status_code}"
    );
    let interrupted: Engagement = serde_json::from_slice(&body)?;
    anyhow::ensure!(
        interrupted.status == codex_riftx_core::EngagementStatus::Active,
        "interrupt ended the engagement: {interrupted:?}"
    );

    wait_for_process_exit(descendant_pid).await?;
    let report = wait_for_interrupted_report(&client, &engagement.id).await?;
    anyhow::ensure!(
        report["engagement"]["status"] == "active",
        "interrupt made the engagement non-resumable: {report}"
    );
    let response = client.get("/v1/system/status").await?;
    anyhow::ensure!(response.status() == StatusCode::OK);
    let control: DaemonControlStatus = serde_json::from_slice(&response.bytes().await?)?;
    anyhow::ensure!(
        (control.state, control.reason) == (DaemonRunState::Running, None),
        "engagement interrupt paused the daemon: {control:?}"
    );
    Ok(())
}

struct KillResponseSequence {
    calls: Arc<AtomicUsize>,
}

impl Respond for KillResponseSequence {
    fn respond(&self, _: &Request) -> ResponseTemplate {
        match self.calls.fetch_add(1, Ordering::SeqCst) {
            0 => ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(responses::sse(vec![
                    responses::ev_response_created("kill-response-1"),
                    responses::ev_function_call(
                        "kill-command-1",
                        "exec_command",
                        &json!({
                            "cmd": descendant_command(),
                            "yield_time_ms": 300,
                            "max_output_tokens": 10_000,
                        })
                        .to_string(),
                    ),
                    responses::ev_completed("kill-response-1"),
                ])),
            1 => ResponseTemplate::new(200)
                .set_delay(Duration::from_secs(60))
                .insert_header("content-type", "text/event-stream")
                .set_body_string(responses::sse(vec![
                    responses::ev_response_created("kill-response-2"),
                    responses::ev_assistant_message("kill-message-1", "should be interrupted"),
                    responses::ev_completed("kill-response-2"),
                ])),
            _ => ResponseTemplate::new(500),
        }
    }
}

async fn create_engagement(client: &LocalIpcClient) -> anyhow::Result<Engagement> {
    let response = client
        .post_json(
            "/v1/engagements",
            serde_json::to_vec(&json!({
                "name": "Kill acceptance",
                "objective": {
                    "summary": "Verify daemon kill process-tree termination",
                    "successCriteria": ["Terminate the active descendant process"],
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
        "create kill engagement returned {}",
        response.status()
    );
    Ok(serde_json::from_slice(&response.bytes().await?)?)
}

async fn wait_for_command_approval(
    client: &LocalIpcClient,
    engagement_id: &str,
) -> anyhow::Result<Value> {
    for _ in 0..POLL_ATTEMPTS {
        let response = client
            .get(&format!("/v1/engagements/{engagement_id}/approvals"))
            .await?;
        if response.status() == StatusCode::OK {
            let approvals: Vec<Value> = serde_json::from_slice(&response.bytes().await?)?;
            if let Some(approval) = approvals
                .into_iter()
                .find(|approval| approval["kind"] == "command")
            {
                return Ok(approval);
            }
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("timed out waiting for kill command approval")
}

async fn wait_for_pid(path: &Path) -> anyhow::Result<u32> {
    for _ in 0..POLL_ATTEMPTS {
        if let Ok(value) = tokio::fs::read_to_string(path).await
            && let Ok(pid) = value.trim().parse()
        {
            return Ok(pid);
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("descendant PID was not written to {}", path.display())
}

async fn wait_for_model_calls(calls: &AtomicUsize, expected: usize) -> anyhow::Result<()> {
    for _ in 0..POLL_ATTEMPTS {
        if calls.load(Ordering::SeqCst) >= expected {
            return Ok(());
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("model did not begin the blocked follow-up request")
}

async fn wait_for_process_exit(pid: u32) -> anyhow::Result<()> {
    for _ in 0..POLL_ATTEMPTS {
        if !process_exists(pid) {
            return Ok(());
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("descendant process {pid} survived daemon kill")
}

fn process_exists(pid: u32) -> bool {
    Command::new("/bin/kill")
        .arg("-0")
        .arg(pid.to_string())
        .status()
        .is_ok_and(|status| status.success())
}

async fn wait_for_interrupted_report(
    client: &LocalIpcClient,
    engagement_id: &str,
) -> anyhow::Result<Value> {
    let path = format!("/v1/engagements/{engagement_id}/report?format=json");
    let mut last_report = Value::Null;
    for _ in 0..POLL_ATTEMPTS {
        let response = client.get(&path).await?;
        if response.status() == StatusCode::OK {
            last_report = serde_json::from_slice(&response.bytes().await?)?;
            if last_report["executions"]
                .as_array()
                .is_some_and(|executions| {
                    executions.len() == 1 && executions[0]["status"] == "interrupted"
                })
            {
                return Ok(last_report);
            }
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("kill report did not reach interrupted state: {last_report}")
}

fn descendant_command() -> &'static str {
    "/bin/sh -c 'trap \"\" TERM; while :; do sleep 1; done' & child=$!; printf '%s' \"$child\" > kill-descendant.pid; wait \"$child\""
}
