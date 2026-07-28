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
async fn chat_completions_executes_a_complete_tool_loop() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create Chat acceptance directory")?;
    let server = MockServer::start().await;
    let arguments = serde_json::to_string(&json!({
        "cmd": native_command(),
        "yield_time_ms": 1_000,
        "max_output_tokens": 10_000,
    }))?;
    mount_chat_sequence(&server, arguments).await;

    let mut config = test_config(temp.path(), format!("{}/v1", server.uri()));
    let secondary = config
        .llm
        .profiles
        .get_mut("secondary")
        .context("secondary profile missing")?;
    secondary.protocol = LlmProtocol::ChatCompletions;
    secondary.model = "chat-tool-model".to_string();
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
                "name": "Chat acceptance",
                "objective": {
                    "summary": "Execute a deterministic local command through Chat Completions",
                    "successCriteria": ["Preserve a hashed local artifact"],
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
        "create Chat engagement returned {}",
        response.status()
    );
    let engagement: Engagement = serde_json::from_slice(&response.bytes().await?)?;
    anyhow::ensure!(engagement.llm_profile == "secondary");

    let response = client
        .post(&format!("/v1/engagements/{}/activate", engagement.id))
        .await?;
    ensure_status(response, StatusCode::OK, "activate Chat engagement").await?;
    let response = client
        .post_json(
            &format!("/v1/engagements/{}/turns", engagement.id),
            serde_json::to_vec(&json!({
                "input": "Run the deterministic Chat acceptance command."
            }))?,
        )
        .await?;
    ensure_status(response, StatusCode::ACCEPTED, "start Chat turn").await?;

    let report = wait_for_completed_report(&client, &engagement.id).await?;
    anyhow::ensure!(
        report["executions"]
            .as_array()
            .is_some_and(|executions| executions.iter().any(|execution| {
                execution["status"] == "completed" && execution["exitCode"] == 0
            })),
        "Chat tool execution did not complete: {report}"
    );
    anyhow::ensure!(
        report["artifacts"]
            .as_array()
            .is_some_and(|artifacts| artifacts.iter().any(|artifact| {
                artifact["path"] == "artifacts/chat-native.txt"
                    && artifact["sizeBytes"] == "chat-native-artifact".len() as u64
            })),
        "Chat tool artifact was not captured: {report}"
    );

    let conversation_response = client
        .get(&format!(
            "/v1/engagements/{}/conversation?limit=200",
            engagement.id
        ))
        .await?;
    anyhow::ensure!(
        conversation_response.status() == StatusCode::OK,
        "query Chat conversation returned {}",
        conversation_response.status()
    );
    let conversation: Value = serde_json::from_slice(&conversation_response.bytes().await?)?;
    anyhow::ensure!(
        conversation["data"].as_array().is_some_and(|entries| {
            entries.iter().any(|entry| {
                entry["role"] == "agent" && entry["text"] == "Chat execution complete."
            })
        }),
        "Chat final assistant summary missing: {conversation}"
    );

    let requests = server
        .received_requests()
        .await
        .context("Chat mock request recording is disabled")?;
    anyhow::ensure!(
        requests.len() == 2,
        "expected two Chat requests: {requests:?}"
    );
    let bodies = requests
        .iter()
        .map(|request| serde_json::from_slice::<Value>(&request.body))
        .collect::<Result<Vec<_>, _>>()?;
    anyhow::ensure!(
        requests.iter().all(|request| {
            request
                .headers
                .get("authorization")
                .is_some_and(|value| value == format!("Bearer {SECONDARY_API_KEY}").as_str())
        }),
        "Chat requests did not use the selected Profile credential"
    );
    anyhow::ensure!(
        bodies
            .iter()
            .all(|body| body["model"] == "chat-tool-model" && body["stream"] == true),
        "Chat requests did not use the selected Profile model: {bodies:?}"
    );
    anyhow::ensure!(
        bodies[0]["tools"].as_array().is_some_and(|tools| {
            tools.iter().any(|tool| {
                tool["type"] == "function" && tool["function"]["name"] == "exec_command"
            })
        }),
        "initial Chat request did not expose exec_command: {}",
        bodies[0]
    );
    let messages = bodies[1]["messages"]
        .as_array()
        .context("second Chat request messages missing")?;
    anyhow::ensure!(
        messages.iter().any(|message| {
            message["role"] == "assistant"
                && message["tool_calls"].as_array().is_some_and(|calls| {
                    calls.iter().any(|call| {
                        call["id"] == "chat-native-command-1"
                            && call["function"]["name"] == "exec_command"
                    })
                })
        }),
        "second Chat request did not preserve the structured tool call: {}",
        bodies[1]
    );
    anyhow::ensure!(
        messages.iter().any(|message| {
            message["role"] == "tool"
                && message["tool_call_id"] == "chat-native-command-1"
                && message["content"]
                    .as_str()
                    .is_some_and(|content| !content.is_empty())
        }),
        "second Chat request did not return matching tool output: {}",
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

async fn mount_chat_sequence(server: &MockServer, arguments: String) {
    let calls = Arc::new(AtomicUsize::new(0));
    let response_calls = Arc::clone(&calls);
    let expected_requests = 2;
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(move |_: &wiremock::Request| {
            let call = response_calls.fetch_add(1, Ordering::SeqCst);
            let body = match call {
                0 => chat_tool_call_sse(&arguments),
                1 => chat_text_sse("Chat execution complete."),
                _ => return ResponseTemplate::new(500),
            };
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(body)
        })
        .up_to_n_times(expected_requests)
        .mount(server)
        .await;
}

fn chat_tool_call_sse(arguments: &str) -> String {
    format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({
            "id": "chatcmpl-tool-1",
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "chat-native-command-1",
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
            "id": "chatcmpl-summary-1",
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
        })
    )
}

async fn wait_for_completed_report(
    client: &LocalIpcClient,
    engagement_id: &str,
) -> anyhow::Result<Value> {
    let path = format!("/v1/engagements/{engagement_id}/report?format=json");
    let mut last_report = Value::Null;
    for _ in 0..COMPLETION_ATTEMPTS {
        approve_pending_approvals(client, engagement_id).await?;
        let response = client.get(&path).await?;
        if response.status() == StatusCode::OK {
            last_report = serde_json::from_slice(&response.bytes().await?)?;
            let completed = last_report["executions"]
                .as_array()
                .is_some_and(|executions| {
                    executions
                        .iter()
                        .any(|execution| execution["status"] == "completed")
                });
            let captured = last_report["artifacts"]
                .as_array()
                .is_some_and(|artifacts| {
                    artifacts
                        .iter()
                        .any(|artifact| artifact["path"] == "artifacts/chat-native.txt")
                });
            if completed && captured {
                return Ok(last_report);
            }
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("Chat turn did not complete with an artifact: {last_report}")
}

async fn approve_pending_approvals(
    client: &LocalIpcClient,
    engagement_id: &str,
) -> anyhow::Result<()> {
    let response = client
        .get(&format!("/v1/engagements/{engagement_id}/approvals"))
        .await?;
    if response.status() != StatusCode::OK {
        return Ok(());
    }
    let approvals: Vec<Value> = serde_json::from_slice(&response.bytes().await?)?;
    for approval in approvals {
        let Some(approval_id) = approval["id"].as_str() else {
            continue;
        };
        let decision = client
            .post_json(
                &format!("/v1/approvals/{approval_id}/decision"),
                serde_json::to_vec(&json!({ "decision": "approve" }))?,
            )
            .await?;
        anyhow::ensure!(
            decision.status() == StatusCode::NO_CONTENT || decision.status() == StatusCode::OK,
            "approve pending Chat command returned {}",
            decision.status()
        );
    }
    Ok(())
}

#[cfg(not(windows))]
fn native_command() -> &'static str {
    "test -z \"${RIFTX_NATIVE_ACCEPTANCE_API_KEY+x}\" || exit 97; test -z \"${RIFTX_NATIVE_ACCEPTANCE_SECONDARY_API_KEY+x}\" || exit 98; printf 'chat-stdout'; printf 'chat-stderr' >&2; printf 'chat-native-artifact' > artifacts/chat-native.txt"
}

#[cfg(windows)]
fn native_command() -> &'static str {
    "if (Test-Path Env:RIFTX_NATIVE_ACCEPTANCE_API_KEY) { exit 97 }; if (Test-Path Env:RIFTX_NATIVE_ACCEPTANCE_SECONDARY_API_KEY) { exit 98 }; [Console]::Out.Write('chat-stdout'); [Console]::Error.Write('chat-stderr'); [IO.File]::WriteAllText('artifacts/chat-native.txt', 'chat-native-artifact')"
}
