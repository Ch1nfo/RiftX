mod common;

use anyhow::Context;
use axum::http::StatusCode;
use codex_riftx_core::Engagement;
use codex_riftx_core::LlmProtocol;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use common::API_KEY;
use common::SECONDARY_API_KEY;
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

const COMPLETION_ATTEMPTS: usize = 100;
const POLL_INTERVAL: Duration = Duration::from_millis(100);

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn invalid_chat_tool_arguments_never_execute_or_create_artifacts() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create invalid tool acceptance directory")?;
    let server = MockServer::start().await;
    mount_invalid_tool_sequence(&server).await;

    let mut config = test_config(temp.path(), format!("{}/v1", server.uri()));
    let secondary = config
        .llm
        .profiles
        .get_mut("secondary")
        .context("secondary profile missing")?;
    secondary.protocol = LlmProtocol::ChatCompletions;
    secondary.model = "chat-invalid-tool-model".to_string();
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
                "name": "Invalid tool arguments acceptance",
                "objective": {
                    "summary": "Reject schema-invalid command arguments without execution",
                    "successCriteria": ["No process runs and no artifact is created"],
                    "structuredCriteria": [],
                },
                "entryPoints": ["10.10.10.1"],
                "mode": "native",
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
        "create invalid-tool engagement returned {}",
        response.status()
    );
    let engagement: Engagement = serde_json::from_slice(&response.bytes().await?)?;

    let response = client
        .post(&format!("/v1/engagements/{}/activate", engagement.id))
        .await?;
    ensure_status(response, StatusCode::OK, "activate invalid-tool engagement").await?;
    let response = client
        .post_json(
            &format!("/v1/engagements/{}/turns", engagement.id),
            serde_json::to_vec(&json!({
                "input": "Try the proposed command and handle invalid arguments safely."
            }))?,
        )
        .await?;
    ensure_status(response, StatusCode::ACCEPTED, "start invalid-tool turn").await?;

    let report = wait_for_completed_task(&client, &engagement.id).await?;
    anyhow::ensure!(
        report["executions"].as_array().is_some_and(Vec::is_empty),
        "schema-invalid arguments created an execution record: {report}"
    );
    anyhow::ensure!(
        report["artifacts"].as_array().is_some_and(Vec::is_empty),
        "schema-invalid arguments created an artifact: {report}"
    );
    anyhow::ensure!(
        !config
            .daemon
            .workspace_root
            .join(&engagement.id)
            .join("artifacts/invalid-tool.txt")
            .exists(),
        "schema-invalid arguments executed the embedded command"
    );

    let requests = server
        .received_requests()
        .await
        .context("invalid-tool mock request recording is disabled")?;
    anyhow::ensure!(
        requests.len() == 2,
        "expected an error tool result followed by a final summary: {requests:?}"
    );
    let bodies = requests
        .iter()
        .map(|request| serde_json::from_slice::<Value>(&request.body))
        .collect::<Result<Vec<_>, _>>()?;
    let second_messages = bodies[1]["messages"]
        .as_array()
        .context("second Chat request messages missing")?;
    anyhow::ensure!(
        second_messages.iter().any(|message| {
            message["role"] == "tool"
                && message["tool_call_id"] == "chat-invalid-command-1"
                && message["content"]
                    .as_str()
                    .is_some_and(|content| content.contains("failed to parse function arguments"))
        }),
        "Runtime did not return a structured parse failure to the model: {}",
        bodies[1]
    );
    let serialized_requests = serde_json::to_string(&bodies)?;
    anyhow::ensure!(!serialized_requests.contains(API_KEY));
    anyhow::ensure!(!serialized_requests.contains(SECONDARY_API_KEY));
    let audit = tokio::fs::read_to_string(&config.audit.jsonl_path).await?;
    anyhow::ensure!(!audit.contains(API_KEY));
    anyhow::ensure!(!audit.contains(SECONDARY_API_KEY));
    Ok(())
}

async fn mount_invalid_tool_sequence(server: &MockServer) {
    let calls = Arc::new(AtomicUsize::new(0));
    let response_calls = Arc::clone(&calls);
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(move |_: &wiremock::Request| {
            let body = match response_calls.fetch_add(1, Ordering::SeqCst) {
                0 => invalid_tool_call_sse(),
                1 => chat_text_sse("Invalid tool arguments were rejected safely."),
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

fn invalid_tool_call_sse() -> String {
    let arguments = json!({
        "cmd": artifact_command(),
        "yield_time_ms": "not-a-number",
        "max_output_tokens": 10_000,
    })
    .to_string();
    format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({
            "id": "chatcmpl-invalid-tool-1",
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "chat-invalid-command-1",
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

fn chat_text_sse(text: &str) -> String {
    format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({
            "id": "chatcmpl-invalid-summary-1",
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
        })
    )
}

async fn wait_for_completed_task(
    client: &LocalIpcClient,
    engagement_id: &str,
) -> anyhow::Result<Value> {
    let path = format!("/v1/engagements/{engagement_id}/report?format=json");
    let mut last_report = Value::Null;
    for _ in 0..COMPLETION_ATTEMPTS {
        let response = client.get(&path).await?;
        if response.status() == StatusCode::OK {
            last_report = serde_json::from_slice(&response.bytes().await?)?;
            if last_report["tasks"]
                .as_array()
                .is_some_and(|tasks| tasks.iter().any(|task| task["status"] == "completed"))
            {
                return Ok(last_report);
            }
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("invalid-tool turn did not complete: {last_report}")
}

#[cfg(not(windows))]
fn artifact_command() -> &'static str {
    "printf 'should-not-exist' > artifacts/invalid-tool.txt"
}

#[cfg(windows)]
fn artifact_command() -> &'static str {
    "[IO.File]::WriteAllText('artifacts/invalid-tool.txt', 'should-not-exist')"
}
