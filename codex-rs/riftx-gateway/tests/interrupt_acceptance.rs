mod common;

use anyhow::Context;
use axum::Router;
use axum::body::Body;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::routing::post;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::LlmProtocol;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use common::SECONDARY_API_KEY;
use common::ensure_status;
use common::spawn_daemon;
use common::test_config;
use common::wait_for_daemon;
use serde_json::Value;
use serde_json::json;
use std::convert::Infallible;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use std::time::Duration;
use tempfile::TempDir;
use tokio::sync::Notify;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

const COMPLETION_ATTEMPTS: usize = 100;
const POLL_INTERVAL: Duration = Duration::from_millis(100);

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn pause_cancels_live_work_and_resume_accepts_only_a_fresh_turn() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create interrupt acceptance directory")?;
    let mock = BlockingChatMock::start().await?;

    let mut config = test_config(temp.path(), mock.base_url());
    let secondary = config
        .llm
        .profiles
        .get_mut("secondary")
        .context("secondary profile missing")?;
    secondary.protocol = LlmProtocol::ChatCompletions;
    secondary.model = "chat-interrupt-model".to_string();
    secondary.base_url = mock.base_url();
    secondary.timeout_seconds = 30;

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
            serde_json::to_vec(&json!({
                "input": "Wait for the model and never continue after interruption."
            }))?,
        )
        .await?;
    ensure_status(response, StatusCode::ACCEPTED, "start interrupt turn").await?;

    mock.wait_for_request_count(1).await?;
    let response = tokio::time::timeout(Duration::from_secs(10), client.post("/v1/system/pause"))
        .await
        .context("pause did not return after interrupting App Server")??;
    ensure_status(response, StatusCode::OK, "pause active model turn").await?;
    anyhow::ensure!(mock.request_count() == 1);
    anyhow::ensure!(mock.authorization_valid());
    wait_for_interrupted_task(&client, &engagement.id).await?;
    // Force a valid post-interrupt tool call onto the original HTTP stream. Even if the mock
    // server has not observed the peer closing its socket yet, the interrupted Runtime must not
    // consume this chunk or start execution.
    let _ = mock.release_tool_call();
    tokio::time::sleep(Duration::from_millis(500)).await;
    let report = read_report(&client, &engagement.id).await?;
    anyhow::ensure!(
        report["engagement"]["status"] == "active",
        "pause made the engagement non-resumable: {report}"
    );
    anyhow::ensure!(
        report["executions"].as_array().is_some_and(Vec::is_empty),
        "an execution began after interruption: {report}"
    );
    anyhow::ensure!(
        report["artifacts"].as_array().is_some_and(Vec::is_empty),
        "an artifact was created after interruption: {report}"
    );
    anyhow::ensure!(
        !config
            .daemon
            .workspace_root
            .join(&engagement.id)
            .join("artifacts/after-interrupt.txt")
            .exists(),
        "the delayed tool call executed after interruption"
    );

    let response = client.post("/v1/system/resume").await?;
    ensure_status(
        response,
        StatusCode::OK,
        "resume after interrupted model turn",
    )
    .await?;
    tokio::time::sleep(Duration::from_millis(500)).await;
    anyhow::ensure!(
        mock.request_count() == 1,
        "resume replayed the interrupted model request"
    );
    let response = client
        .get(&format!("/v1/engagements/{}", engagement.id))
        .await?;
    anyhow::ensure!(
        response.status() == StatusCode::OK,
        "read paused engagement returned {}",
        response.status()
    );
    let current: Engagement = serde_json::from_slice(&response.bytes().await?)?;
    anyhow::ensure!(current.status == EngagementStatus::Active);

    let response = client
        .post_json(
            &format!("/v1/engagements/{}/turns", engagement.id),
            serde_json::to_vec(&json!({
                "input": "Start a new turn after the operator explicitly resumes."
            }))?,
        )
        .await?;
    ensure_status(response, StatusCode::ACCEPTED, "start fresh resumed turn").await?;
    mock.wait_for_request_count(2).await?;
    anyhow::ensure!(
        mock.request_count() == 2,
        "resume did not route exactly one fresh model request"
    );

    let response = client
        .post(&format!("/v1/engagements/{}/interrupt", engagement.id))
        .await?;
    ensure_status(response, StatusCode::OK, "clean up fresh resumed turn").await?;
    Ok(())
}

async fn create_engagement(client: &LocalIpcClient) -> anyhow::Result<Engagement> {
    let response = client
        .post_json(
            "/v1/engagements",
            serde_json::to_vec(&json!({
                "name": "Interrupt propagation acceptance",
                "objective": {
                    "summary": "Cancel an in-flight model stream",
                    "successCriteria": ["No replay or delayed execution after pause"],
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
        "create interrupt engagement returned {}",
        response.status()
    );
    Ok(serde_json::from_slice(&response.bytes().await?)?)
}

async fn wait_for_interrupted_task(
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
                .is_some_and(|tasks| tasks.iter().any(|task| task["status"] == "interrupted"))
            {
                return Ok(last_report);
            }
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("interrupted task did not reach a terminal state: {last_report}")
}

async fn read_report(client: &LocalIpcClient, engagement_id: &str) -> anyhow::Result<Value> {
    let response = client
        .get(&format!(
            "/v1/engagements/{engagement_id}/report?format=json"
        ))
        .await?;
    anyhow::ensure!(
        response.status() == StatusCode::OK,
        "read interrupt report returned {}",
        response.status()
    );
    Ok(serde_json::from_slice(&response.bytes().await?)?)
}

struct BlockingChatMock {
    address: std::net::SocketAddr,
    state: Arc<BlockingChatState>,
    shutdown: CancellationToken,
    server: tokio::task::JoinHandle<()>,
}

impl BlockingChatMock {
    async fn start() -> anyhow::Result<Self> {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        let state = Arc::new(BlockingChatState::default());
        let app = Router::new()
            .route("/chat/completions", post(blocking_chat_response))
            .with_state(Arc::clone(&state));
        let shutdown = CancellationToken::new();
        let shutdown_signal = shutdown.clone();
        let server = tokio::spawn(async move {
            let _ = axum::serve(listener, app)
                .with_graceful_shutdown(shutdown_signal.cancelled_owned())
                .await;
        });
        Ok(Self {
            address,
            state,
            shutdown,
            server,
        })
    }

    fn base_url(&self) -> String {
        format!("http://{}", self.address)
    }

    async fn wait_for_request_count(&self, expected: usize) -> anyhow::Result<()> {
        tokio::time::timeout(Duration::from_secs(10), async {
            while self.request_count() < expected {
                self.state.started.notified().await;
            }
        })
        .await
        .with_context(|| {
            format!(
                "model request count did not reach {expected}; observed {}",
                self.request_count()
            )
        })?;
        Ok(())
    }

    fn request_count(&self) -> usize {
        self.state.request_count.load(Ordering::SeqCst)
    }

    fn authorization_valid(&self) -> bool {
        self.state.authorization_valid.load(Ordering::SeqCst)
    }

    fn release_tool_call(&self) -> bool {
        let Some(sender) = self
            .state
            .response_sender
            .lock()
            .ok()
            .and_then(|sender| sender.as_ref().cloned())
        else {
            return false;
        };
        sender.send(Ok(Bytes::from(tool_call_sse()))).is_ok()
    }
}

impl Drop for BlockingChatMock {
    fn drop(&mut self) {
        self.shutdown.cancel();
        self.server.abort();
    }
}

#[derive(Default)]
struct BlockingChatState {
    request_count: AtomicUsize,
    authorization_valid: AtomicBool,
    started: Notify,
    response_sender: Mutex<Option<mpsc::UnboundedSender<Result<Bytes, Infallible>>>>,
}

async fn blocking_chat_response(
    State(state): State<Arc<BlockingChatState>>,
    headers: HeaderMap,
) -> Response<Body> {
    state.request_count.fetch_add(1, Ordering::SeqCst);
    state.authorization_valid.store(
        headers
            .get("authorization")
            .is_some_and(|value| value == format!("Bearer {SECONDARY_API_KEY}").as_str()),
        Ordering::SeqCst,
    );
    let (sender, receiver) = mpsc::unbounded_channel();
    let _ = sender.send(Ok(Bytes::from_static(b": keep-alive\n\n")));
    let Ok(mut response_sender) = state.response_sender.lock() else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    *response_sender = Some(sender);
    drop(response_sender);
    state.started.notify_waiters();
    let stream = futures::stream::unfold(receiver, |mut receiver| async move {
        receiver.recv().await.map(|item| (item, receiver))
    });
    let body = Body::from_stream(stream);
    ([("content-type", "text/event-stream")], body).into_response()
}

fn tool_call_sse() -> String {
    let arguments = json!({
        "cmd": artifact_command(),
        "yield_time_ms": 1_000,
        "max_output_tokens": 10_000,
    })
    .to_string();
    format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({
            "id": "chatcmpl-after-interrupt",
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "chat-after-interrupt-command",
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

#[cfg(not(windows))]
fn artifact_command() -> &'static str {
    "printf 'should-not-exist' > artifacts/after-interrupt.txt"
}

#[cfg(windows)]
fn artifact_command() -> &'static str {
    "[IO.File]::WriteAllText('artifacts/after-interrupt.txt', 'should-not-exist')"
}
