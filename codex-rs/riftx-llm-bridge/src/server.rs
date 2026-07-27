use crate::BridgeError;
use crate::ChatStreamConverter;
use crate::chat_completions_url;
use crate::responses_request_to_chat;
use axum::Router;
use axum::body::Body;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::routing::post;
use bytes::Bytes;
use futures::StreamExt;
use futures::stream::BoxStream;
use reqwest::Client;
use serde_json::Value;
use std::convert::Infallible;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio::sync::oneshot;
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
}

/// Bind `127.0.0.1:0`, serve `POST /v1/responses`, and return the Runtime-facing base URL.
pub async fn start_loopback_bridge(upstream: BridgeUpstream) -> Result<BridgeHandle, BridgeError> {
    let bearer_token = format!("riftx-bridge-{}", Uuid::new_v4());
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr()?;
    let base_url = format!("http://127.0.0.1:{}/v1", addr.port());
    let client = Client::builder().timeout(upstream.timeout).build()?;
    let state = AppState {
        bearer_token: bearer_token.clone(),
        upstream,
        client,
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
    let chat_body = responses_request_to_chat(&request)?;
    let upstream_url = chat_completions_url(&state.upstream.base_url);
    let upstream = state
        .client
        .post(upstream_url)
        .header(
            "Authorization",
            format!("Bearer {}", state.upstream.api_key),
        )
        .header("Content-Type", "application/json")
        .json(&chat_body)
        .send()
        .await?;

    let status = upstream.status();
    if !status.is_success() {
        let text = upstream.text().await.unwrap_or_default();
        return Err(BridgeError::Upstream(format!(
            "HTTP {status}: {}",
            truncate_for_error(&text)
        )));
    }

    let response_id = format!("resp_{}", Uuid::new_v4());
    let stream = bridge_sse_stream(upstream, response_id);
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
) -> BoxStream<'static, Result<Bytes, Infallible>> {
    let (tx, rx) = mpsc::channel::<Bytes>(16);
    tokio::spawn(async move {
        let mut converter = ChatStreamConverter::new(response_id);
        let mut buffer = String::new();
        let mut byte_stream = upstream.bytes_stream();
        loop {
            match byte_stream.next().await {
                Some(Ok(chunk)) => {
                    buffer.push_str(&String::from_utf8_lossy(&chunk));
                    match converter.ingest_sse_buffer(&mut buffer) {
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
                Some(Err(error)) => {
                    let _ = tx
                        .send(Bytes::from(failed_sse_frame(&BridgeError::Upstream(
                            error.to_string(),
                        ))))
                        .await;
                    return;
                }
                None => {
                    match converter.finish(None) {
                        Ok(events) => {
                            for event in events {
                                if tx.send(Bytes::from(event.to_sse_frame())).await.is_err() {
                                    return;
                                }
                            }
                        }
                        Err(error) => {
                            let _ = tx.send(Bytes::from(failed_sse_frame(&error))).await;
                        }
                    }
                    return;
                }
            }
        }
    });
    Box::pin(futures::stream::unfold(rx, |mut rx| async move {
        match rx.recv().await {
            Some(bytes) => Some((Ok(bytes), rx)),
            None => None,
        }
    }))
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
            "message": error.to_string(),
            "type": "riftx_llm_bridge_error",
        }
    });
    (status, axum::Json(body)).into_response()
}

fn failed_sse_frame(error: &BridgeError) -> String {
    format!(
        "event: response.failed\ndata: {}\n\n",
        serde_json::json!({
            "type": "response.failed",
            "response": {
                "id": "resp_failed",
                "object": "response",
                "status": "failed",
                "error": { "message": error.to_string() }
            }
        })
    )
}

fn truncate_for_error(text: &str) -> String {
    const MAX: usize = 512;
    let trimmed = text.trim();
    if trimmed.len() <= MAX {
        trimmed.to_string()
    } else {
        format!("{}…", &trimmed[..MAX])
    }
}
