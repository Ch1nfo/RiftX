mod common;

use anyhow::Context;
use axum::http::StatusCode;
use codex_riftx_core::Engagement;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use common::API_KEY;
use common::ensure_status;
use common::spawn_daemon;
use common::test_config;
use common::wait_for_daemon;
use core_test_support::responses;
use serde_json::Value;
use serde_json::json;
use std::ffi::OsString;
use std::path::Path;
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

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn cli_turn_routes_local_execution_through_daemon_intent() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create CLI acceptance directory")?;
    let server = responses::start_mock_server().await;
    let command = cli_command();
    Mock::given(method("POST"))
        .and(path("/v1/responses"))
        .respond_with(CliResponseSequence {
            calls: AtomicUsize::new(0),
            command: command.to_string(),
        })
        .up_to_n_times(2)
        .mount(&server)
        .await;

    let config = test_config(temp.path(), format!("{}/v1", server.uri()));
    let config_path = temp.path().join("riftx.toml");
    tokio::fs::write(&config_path, toml::to_string(&config)?)
        .await
        .context("write CLI acceptance config")?;
    let mut daemon = spawn_daemon(&config_path)?;
    let client = LocalIpcClient::new(LocalIpcEndpoint::new(&config.daemon.ipc_dir));
    wait_for_daemon(&client, &mut daemon.child).await?;
    let response = client.get("/v1/system/info").await?;
    ensure_status(response, StatusCode::OK, "verify daemon for CLI").await?;

    let engagement = create_engagement(&client).await?;
    run_cli(&config_path, &["activate", &engagement.id]).await?;
    run_cli(
        &config_path,
        &[
            "turn",
            &engagement.id,
            "Run the deterministic CLI acceptance command.",
        ],
    )
    .await?;

    let approval = wait_for_command_approval(&client, &engagement.id).await?;
    let approval_id = approval["id"].as_str().context("approval id missing")?;
    let intent = &approval["executionIntent"];
    anyhow::ensure!(
        intent["engagementId"] == engagement.id,
        "engagement binding missing: {approval}"
    );
    anyhow::ensure!(
        intent["threadId"].is_string(),
        "thread binding missing: {approval}"
    );
    anyhow::ensure!(
        intent["turnId"].is_string(),
        "turn binding missing: {approval}"
    );
    anyhow::ensure!(
        intent["toolCallId"] == "cli-command-1",
        "tool-call binding missing: {approval}"
    );
    anyhow::ensure!(
        intent["mode"] == "redTeam",
        "mode binding missing: {approval}"
    );
    anyhow::ensure!(
        intent["displayArgv"]
            .as_array()
            .is_some_and(|argv| argv.iter().any(|arg| arg == command)),
        "CLI command missing from execution intent: {approval}"
    );
    anyhow::ensure!(
        intent["bindingSha256"]
            .as_str()
            .is_some_and(|digest| digest.len() == 64),
        "approval binding missing: {approval}"
    );

    run_cli(&config_path, &["approve", approval_id]).await?;
    let report = wait_for_completed_report(&client, &engagement.id).await?;
    let executions = report["executions"]
        .as_array()
        .context("report executions missing")?;
    anyhow::ensure!(
        executions.len() == 1,
        "unexpected CLI executions: {executions:?}"
    );
    anyhow::ensure!(
        executions[0]["status"] == "completed" && executions[0]["exitCode"] == 0,
        "CLI execution did not complete: {}",
        executions[0]
    );
    anyhow::ensure!(
        executions[0]["stdoutBytes"].as_u64().unwrap_or_default() >= "CLI-INTENT".len() as u64,
        "CLI execution output missing: {}",
        executions[0]
    );

    let requests = wait_for_model_requests(&server, 2).await?;
    let model_requests = requests
        .iter()
        .filter(|request| request.url.path() == "/v1/responses")
        .collect::<Vec<_>>();
    anyhow::ensure!(
        model_requests.iter().all(|request| {
            request.headers.get("authorization").is_some_and(|value| {
                value
                    .to_str()
                    .is_ok_and(|value| value == format!("Bearer {API_KEY}"))
            })
        }),
        "CLI turn did not use the daemon-managed Runtime profile"
    );
    Ok(())
}

async fn wait_for_model_requests(
    server: &wiremock::MockServer,
    expected: usize,
) -> anyhow::Result<Vec<Request>> {
    let mut observed = 0;
    for _ in 0..POLL_ATTEMPTS {
        let requests = server.received_requests().await.unwrap_or_default();
        observed = requests
            .iter()
            .filter(|request| request.url.path() == "/v1/responses")
            .count();
        if observed >= expected {
            return Ok(requests);
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("model request count did not reach {expected}; observed {observed}")
}

struct CliResponseSequence {
    calls: AtomicUsize,
    command: String,
}

impl Respond for CliResponseSequence {
    fn respond(&self, _: &Request) -> ResponseTemplate {
        let body = match self.calls.fetch_add(1, Ordering::SeqCst) {
            0 => responses::sse(vec![
                responses::ev_response_created("cli-response-1"),
                responses::ev_function_call(
                    "cli-command-1",
                    "exec_command",
                    &json!({
                        "cmd": self.command,
                        "yield_time_ms": 1_000,
                        "max_output_tokens": 10_000,
                    })
                    .to_string(),
                ),
                responses::ev_completed("cli-response-1"),
            ]),
            1 => responses::sse(vec![
                responses::ev_response_created("cli-response-2"),
                responses::ev_assistant_message("cli-message-1", "CLI execution complete."),
                responses::ev_completed("cli-response-2"),
            ]),
            _ => return ResponseTemplate::new(500),
        };
        ResponseTemplate::new(200)
            .insert_header("content-type", "text/event-stream")
            .set_body_string(body)
    }
}

async fn run_cli(config_path: &Path, args: &[&str]) -> anyhow::Result<()> {
    let mut argv = vec![OsString::from("riftx"), OsString::from("--config")];
    argv.push(config_path.as_os_str().to_os_string());
    argv.extend(args.iter().map(OsString::from));
    codex_riftx_cli::run_from(argv)
        .await
        .with_context(|| format!("run riftx {}", args.join(" ")))
}

async fn create_engagement(client: &LocalIpcClient) -> anyhow::Result<Engagement> {
    let response = client
        .post_json(
            "/v1/engagements",
            serde_json::to_vec(&json!({
                "name": "CLI acceptance",
                "objective": {
                    "summary": "Verify CLI execution policy routing",
                    "successCriteria": ["Record one daemon-approved local execution"],
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
        "create CLI engagement returned {}: {}",
        response.status(),
        String::from_utf8_lossy(&response.bytes().await?)
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
        if response.status().is_success() {
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
    anyhow::bail!("timed out waiting for CLI command approval")
}

async fn wait_for_completed_report(
    client: &LocalIpcClient,
    engagement_id: &str,
) -> anyhow::Result<Value> {
    let path = format!("/v1/engagements/{engagement_id}/report?format=json");
    let mut last_report = Value::Null;
    for _ in 0..POLL_ATTEMPTS {
        let response = client.get(&path).await?;
        if response.status().is_success() {
            last_report = serde_json::from_slice(&response.bytes().await?)?;
            if last_report["executions"]
                .as_array()
                .is_some_and(|executions| {
                    executions.len() == 1 && executions[0]["status"] == "completed"
                })
            {
                return Ok(last_report);
            }
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("CLI turn did not complete: {last_report}")
}

#[cfg(not(windows))]
fn cli_command() -> &'static str {
    "printf 'CLI-INTENT'"
}

#[cfg(windows)]
fn cli_command() -> &'static str {
    "[Console]::Out.Write('CLI-INTENT')"
}
