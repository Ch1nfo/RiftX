mod common;

use anyhow::Context;
use axum::http::StatusCode;
use codex_riftx_core::AUTO_MODE_CONFIRMATION;
use codex_riftx_core::AutoProgressAction;
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
async fn auto_controller_drives_multiple_chat_turns_until_no_progress_needs_input()
-> anyhow::Result<()> {
    let temp = TempDir::new().context("create Auto acceptance directory")?;
    let server = MockServer::start().await;
    mount_no_progress_sequence(&server).await;

    let mut config = test_config(temp.path(), format!("{}/v1", server.uri()));
    let secondary = config
        .llm
        .profiles
        .get_mut("secondary")
        .context("secondary profile missing")?;
    secondary.protocol = LlmProtocol::ChatCompletions;
    secondary.model = "chat-auto-model".to_string();
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
                "name": "Auto multi-turn acceptance",
                "objective": {
                    "summary": "Collect reproducible evidence without inventing progress",
                    "successCriteria": ["Preserve one reproducible evidence item"],
                    "structuredCriteria": [{
                        "id": "evidence-criterion",
                        "description": "At least one reproducible evidence item exists",
                        "predicate": {
                            "type": "evidence",
                            "minimumItems": 1,
                            "reproductionRequired": true,
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
        "create Auto engagement returned {}",
        response.status()
    );
    let engagement: Engagement = serde_json::from_slice(&response.bytes().await?)?;

    let response = client
        .post(&format!("/v1/engagements/{}/activate", engagement.id))
        .await?;
    ensure_status(response, StatusCode::OK, "activate Auto engagement").await?;

    let run = wait_for_no_progress_stop(&client, &engagement.id).await?;
    anyhow::ensure!(
        run.state == AutoRunState::NeedsInput,
        "unexpected Auto run: {run:?}"
    );
    anyhow::ensure!(run.stop_reason == Some(AutoStopReason::NoProgress));
    anyhow::ensure!(run.turns_started == 3);
    anyhow::ensure!(run.turns_completed == 3);
    anyhow::ensure!(run.tool_calls == 0);
    anyhow::ensure!(run.no_progress_turns == 3);
    anyhow::ensure!(
        run.last_goal_assessment
            .as_ref()
            .is_some_and(|assessment| !assessment.succeeded
                && assessment.evidence_ids.is_empty()
                && assessment
                    .criteria
                    .iter()
                    .all(|criterion| !criterion.satisfied)),
        "Auto accepted a model claim without Evidence: {run:?}"
    );
    anyhow::ensure!(
        run.last_progress_assessment
            .as_ref()
            .is_some_and(|assessment| assessment.action == AutoProgressAction::NeedsInput),
        "Auto did not persist the no-progress decision: {run:?}"
    );

    let requests = server
        .received_requests()
        .await
        .context("Auto mock request recording is disabled")?;
    anyhow::ensure!(
        requests.len() == 3,
        "expected exactly three Auto turns: {requests:?}"
    );
    let bodies = requests
        .iter()
        .map(|request| serde_json::from_slice::<Value>(&request.body))
        .collect::<Result<Vec<_>, _>>()?;
    let prompts = bodies
        .iter()
        .map(latest_user_prompt)
        .collect::<anyhow::Result<Vec<_>>>()?;
    anyhow::ensure!(
        prompts[0].contains("RiftX Auto controller turn 1/20")
            && prompts[0].contains("Establish the current authorized state"),
        "first Auto subgoal missing: {}",
        prompts[0]
    );
    anyhow::ensure!(
        prompts[1].contains("RiftX Auto controller turn 2/20")
            && prompts[1].contains("Replan from the structured state"),
        "Auto did not replan after the first no-progress turn: {}",
        prompts[1]
    );
    anyhow::ensure!(
        prompts[2].contains("RiftX Auto controller turn 3/20")
            && prompts[2].contains("Switch strategy, reduce concurrency"),
        "Auto did not switch strategy after the second no-progress turn: {}",
        prompts[2]
    );

    Ok(())
}

async fn mount_no_progress_sequence(server: &MockServer) {
    let calls = Arc::new(AtomicUsize::new(0));
    let response_calls = Arc::clone(&calls);
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(move |_: &wiremock::Request| {
            let call = response_calls.fetch_add(1, Ordering::SeqCst);
            if call >= 3 {
                return ResponseTemplate::new(500);
            }
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(chat_text_sse(
                    call + 1,
                    "I completed the objective, but I have no structured evidence.",
                ))
        })
        .up_to_n_times(3)
        .mount(server)
        .await;
}

fn chat_text_sse(turn: usize, text: &str) -> String {
    format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({
            "id": format!("chatcmpl-auto-{turn}"),
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
        })
    )
}

async fn wait_for_no_progress_stop(
    client: &LocalIpcClient,
    engagement_id: &str,
) -> anyhow::Result<AutoRun> {
    let path = format!("/v1/engagements/{engagement_id}/auto");
    let mut last_run = None;
    for _ in 0..COMPLETION_ATTEMPTS {
        let response = client.get(&path).await?;
        if response.status() == StatusCode::OK {
            let run: AutoRun = serde_json::from_slice(&response.bytes().await?)?;
            if run.state == AutoRunState::NeedsInput {
                return Ok(run);
            }
            if matches!(
                run.state,
                AutoRunState::Succeeded
                    | AutoRunState::Expired
                    | AutoRunState::BudgetExhausted
                    | AutoRunState::Failed
                    | AutoRunState::Killed
            ) {
                anyhow::bail!("Auto run stopped unexpectedly: {run:?}");
            }
            last_run = Some(run);
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("Auto run did not stop after its no-progress window: {last_run:?}")
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
