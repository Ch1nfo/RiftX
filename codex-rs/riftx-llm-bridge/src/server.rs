use crate::BridgeError;
use crate::ChatStreamConverter;
use crate::chat_completions_url;
use crate::diagnostics::sanitize_diagnostic;
use crate::request::responses_request_to_chat_with_tool_names;
use crate::sse::SseDecoder;
use axum::Router;
use axum::body::Body;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::routing::post;
use bytes::Bytes;
use futures::Stream;
use futures::StreamExt;
use reqwest::Client;
use serde_json::Value;
use std::convert::Infallible;
use std::pin::Pin;
use std::task::Context;
use std::task::Poll;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio::sync::oneshot;
use tokio::sync::watch;
use uuid::Uuid;

/// Upstream Chat Completions target owned by one bridge instance.
#[derive(Clone)]
pub struct BridgeUpstream {
    pub base_url: String,
    pub api_key: String,
    pub timeout: Duration,
}

/// Running loopback bridge. Dropping the handle aborts the accept loop.
pub struct BridgeHandle {
    pub base_url: String,
    pub bearer_token: String,
    request_cancellation: watch::Sender<u64>,
    shutdown_tx: Option<oneshot::Sender<()>>,
    join: Option<tokio::task::JoinHandle<()>>,
}

impl BridgeHandle {
    pub fn responses_base_url(&self) -> &str {
        &self.base_url
    }

    pub fn bearer_token(&self) -> &str {
        &self.bearer_token
    }

    /// Cancel requests currently proxied by this bridge without affecting future requests.
    pub fn cancel_inflight(&self) {
        let next_generation = self.request_cancellation.borrow().wrapping_add(1);
        self.request_cancellation.send_replace(next_generation);
    }
}

impl Drop for BridgeHandle {
    fn drop(&mut self) {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(());
        }
        if let Some(join) = self.join.take() {
            join.abort();
        }
    }
}

#[derive(Clone)]
struct AppState {
    bearer_token: String,
    upstream: BridgeUpstream,
    client: Client,
    request_cancellation: watch::Sender<u64>,
}

/// Bind `127.0.0.1:0`, serve `POST /v1/responses`, and return the Runtime-facing base URL.
pub async fn start_loopback_bridge(upstream: BridgeUpstream) -> Result<BridgeHandle, BridgeError> {
    let bearer_token = format!("riftx-bridge-{}", Uuid::new_v4());
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr()?;
    let base_url = format!("http://127.0.0.1:{}/v1", addr.port());
    let client = Client::builder().timeout(upstream.timeout).build()?;
    let (request_cancellation, _) = watch::channel(0);
    let state = AppState {
        bearer_token: bearer_token.clone(),
        upstream,
        client,
        request_cancellation: request_cancellation.clone(),
    };
    let app = Router::new()
        .route("/v1/responses", post(handle_responses))
        .with_state(state);
    let (shutdown_tx, shutdown_rx) = oneshot::channel::<()>();
    let join = tokio::spawn(async move {
        let server = axum::serve(listener, app).with_graceful_shutdown(async {
            let _ = shutdown_rx.await;
        });
        let _ = server.await;
    });
    Ok(BridgeHandle {
        base_url,
        bearer_token,
        request_cancellation,
        shutdown_tx: Some(shutdown_tx),
        join: Some(join),
    })
}

async fn handle_responses(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    match handle_responses_inner(state, headers, body).await {
        Ok(response) => response,
        Err(error) => error_response(error),
    }
}

async fn handle_responses_inner(
    state: AppState,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, BridgeError> {
    authorize(&headers, &state.bearer_token)?;
    let request: Value = serde_json::from_slice(&body)?;
    let converted = responses_request_to_chat_with_tool_names(&request)?;
    let mut cancellation = state.request_cancellation.subscribe();
    let upstream_url = chat_completions_url(&state.upstream.base_url);
    let request = state
        .client
        .post(upstream_url)
        .header(
            "Authorization",
            format!("Bearer {}", state.upstream.api_key),
        )
        .header("Content-Type", "application/json")
        .json(&converted.body);
    let upstream = tokio::select! {
        _ = cancellation.changed() => {
            return Err(BridgeError::Upstream("Chat Completions request was cancelled".into()));
        }
        result = request.send() => result.map_err(|error| {
            if error.is_timeout() {
                BridgeError::Upstream("Chat Completions request timed out".into())
            } else {
                BridgeError::Http(error)
            }
        })?,
    };

    let status = upstream.status();
    if !status.is_success() {
        let text = upstream.text().await.unwrap_or_default();
        return Err(BridgeError::Upstream(format!(
            "HTTP {status}: {}",
            sanitize_diagnostic(&text, 512)
        )));
    }

    let response_id = format!("resp_{}", Uuid::new_v4());
    let stream = bridge_sse_stream(upstream, response_id, converted.tool_names, cancellation);
    Ok(Response::builder()
        .status(StatusCode::OK)
        .header("Content-Type", "text/event-stream")
        .header("Cache-Control", "no-cache")
        .body(Body::from_stream(stream))
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response()))
}

fn bridge_sse_stream(
    upstream: reqwest::Response,
    response_id: String,
    tool_names: std::collections::BTreeMap<String, crate::request::ResponsesToolName>,
    mut cancellation: watch::Receiver<u64>,
) -> BridgeSseStream {
    let (tx, rx) = mpsc::channel::<Bytes>(16);
    let worker = tokio::spawn(async move {
        let mut converter = ChatStreamConverter::with_tool_names(response_id, tool_names);
        let mut decoder = SseDecoder::default();
        let mut byte_stream = upstream.bytes_stream();
        loop {
            let next = tokio::select! {
                _ = cancellation.changed() => return,
                next = byte_stream.next() => next,
            };
            match next {
                Some(Ok(chunk)) => {
                    let frames = match decoder.push(&chunk) {
                        Ok(frames) => frames,
                        Err(error) => {
                            let _ = tx.send(Bytes::from(failed_sse_frame(&error))).await;
                            return;
                        }
                    };
                    for frame in frames {
                        match converter.ingest_sse_frame(&frame) {
                            Ok(events) => {
                                for event in events {
                                    if tx.send(Bytes::from(event.to_sse_frame())).await.is_err() {
                                        return;
                                    }
                                }
                            }
                            Err(error) => {
                                let _ = tx.send(Bytes::from(failed_sse_frame(&error))).await;
                                return;
                            }
                        }
                    }
                }
                Some(Err(error)) => {
                    let _ = tx
                        .send(Bytes::from(failed_sse_frame(&BridgeError::Upstream(
                            error.to_string(),
                        ))))
                        .await;
                    return;
                }
                None => {
                    let error = decoder.finish().err().or_else(|| {
                        (!converter.is_completed()).then(|| {
                            BridgeError::Upstream(
                                "Chat Completions stream ended before the [DONE] marker".into(),
                            )
                        })
                    });
                    if let Some(error) = error {
                        let _ = tx.send(Bytes::from(failed_sse_frame(&error))).await;
                    }
                    return;
                }
            }
        }
    });
    BridgeSseStream { rx, worker }
}

struct BridgeSseStream {
    rx: mpsc::Receiver<Bytes>,
    worker: tokio::task::JoinHandle<()>,
}

impl Stream for BridgeSseStream {
    type Item = Result<Bytes, Infallible>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        Pin::new(&mut self.rx)
            .poll_recv(cx)
            .map(|item| item.map(Ok))
    }
}

impl Drop for BridgeSseStream {
    fn drop(&mut self) {
        self.worker.abort();
    }
}

fn authorize(headers: &HeaderMap, expected: &str) -> Result<(), BridgeError> {
    let value = headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .ok_or_else(|| BridgeError::Upstream("missing bridge Authorization".into()))?;
    let token = value
        .strip_prefix("Bearer ")
        .ok_or_else(|| BridgeError::Upstream("bridge Authorization must be Bearer".into()))?;
    if token != expected {
        return Err(BridgeError::Upstream("invalid bridge bearer token".into()));
    }
    Ok(())
}

fn error_response(error: BridgeError) -> Response {
    let status = match &error {
        BridgeError::Unsupported(_) | BridgeError::InvalidRequest(_) => StatusCode::BAD_REQUEST,
        _ => StatusCode::BAD_GATEWAY,
    };
    let body = serde_json::json!({
        "error": {
            "message": sanitize_diagnostic(&error.to_string(), 512),
            "type": "riftx_llm_bridge_error",
        }
    });
    (status, axum::Json(body)).into_response()
}

fn failed_sse_frame(error: &BridgeError) -> String {
    let message = sanitize_diagnostic(&error.to_string(), 512);
    format!(
        "event: response.failed\ndata: {}\n\n",
        serde_json::json!({
            "type": "response.failed",
            "response": {
                "id": "resp_failed",
                "object": "response",
                "status": "failed",
                "error": { "message": message }
            }
        })
    )
}
