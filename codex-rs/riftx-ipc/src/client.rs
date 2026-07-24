use crate::LocalIpcEndpoint;
use bytes::Bytes;
use http::Method;
use http::Request;
use http::StatusCode;
use http_body_util::BodyExt;
use http_body_util::Full;
use hyper::body::Incoming;
use hyper::client::conn::http1;
use hyper_util::rt::TokioIo;
use serde::Serialize;
use serde::de::DeserializeOwned;
use std::pin::Pin;
use thiserror::Error;

const MAX_SSE_EVENT_BYTES: usize = 256 * 1024;

#[derive(Debug, Error)]
pub enum LocalIpcError {
    #[error("local IPC I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("local IPC HTTP failed: {0}")]
    Http(#[from] hyper::Error),
    #[error("invalid local IPC request: {0}")]
    Request(#[from] http::Error),
    #[error("invalid local IPC event stream: {0}")]
    EventStream(String),
    #[error("failed to encode local IPC JSON: {0}")]
    EncodeJson(#[source] serde_json::Error),
    #[error("failed to decode local IPC JSON: {0}")]
    DecodeJson(#[source] serde_json::Error),
}

#[derive(Debug, Clone)]
pub struct LocalIpcClient {
    endpoint: LocalIpcEndpoint,
}

impl LocalIpcClient {
    pub fn new(endpoint: LocalIpcEndpoint) -> Self {
        Self { endpoint }
    }

    pub async fn get(&self, path: &str) -> Result<LocalIpcResponse, LocalIpcError> {
        self.request(Method::GET, path, RequestBody::Empty).await
    }

    pub async fn post(&self, path: &str) -> Result<LocalIpcResponse, LocalIpcError> {
        self.request(Method::POST, path, RequestBody::Empty).await
    }

    pub async fn post_json(
        &self,
        path: &str,
        json: Vec<u8>,
    ) -> Result<LocalIpcResponse, LocalIpcError> {
        self.request(Method::POST, path, RequestBody::Json(json))
            .await
    }

    pub async fn post_typed<T: Serialize + ?Sized>(
        &self,
        path: &str,
        value: &T,
    ) -> Result<LocalIpcResponse, LocalIpcError> {
        let json = serde_json::to_vec(value).map_err(LocalIpcError::EncodeJson)?;
        self.post_json(path, json).await
    }

    pub async fn post_bytes(
        &self,
        path: &str,
        bytes: Vec<u8>,
    ) -> Result<LocalIpcResponse, LocalIpcError> {
        self.request(Method::POST, path, RequestBody::Bytes(bytes))
            .await
    }

    async fn request(
        &self,
        method: Method,
        path: &str,
        body: RequestBody,
    ) -> Result<LocalIpcResponse, LocalIpcError> {
        let stream = connect(&self.endpoint).await?;
        let (mut sender, connection) = http1::handshake(TokioIo::new(stream)).await?;
        tokio::spawn(async move {
            let _ = connection.await;
        });

        let mut request = Request::builder()
            .method(method)
            .uri(path)
            .header(http::header::HOST, "riftxd.local");
        let body = match body {
            RequestBody::Empty => Full::new(Bytes::new()),
            RequestBody::Json(json) => {
                request = request.header(http::header::CONTENT_TYPE, "application/json");
                Full::new(Bytes::from(json))
            }
            RequestBody::Bytes(bytes) => {
                request = request.header(http::header::CONTENT_TYPE, "application/octet-stream");
                Full::new(Bytes::from(bytes))
            }
        };
        let response = sender.send_request(request.body(body)?).await?;
        Ok(LocalIpcResponse { inner: response })
    }
}

enum RequestBody {
    Empty,
    Json(Vec<u8>),
    Bytes(Vec<u8>),
}

pub struct LocalIpcResponse {
    inner: http::Response<Incoming>,
}

impl LocalIpcResponse {
    pub fn status(&self) -> StatusCode {
        self.inner.status()
    }

    pub async fn bytes(self) -> Result<Bytes, LocalIpcError> {
        Ok(self.inner.into_body().collect().await?.to_bytes())
    }

    pub async fn json<T: DeserializeOwned>(self) -> Result<T, LocalIpcError> {
        let bytes = self.bytes().await?;
        serde_json::from_slice(&bytes).map_err(LocalIpcError::DecodeJson)
    }

    pub fn into_data_stream(
        self,
    ) -> impl futures::Stream<Item = Result<Bytes, hyper::Error>> + Send {
        self.inner.into_body().into_data_stream()
    }

    pub fn into_sse_stream(self) -> LocalSseStream {
        LocalSseStream {
            inner: Box::pin(self.inner.into_body().into_data_stream()),
            buffer: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LocalSseEvent {
    pub event: Option<String>,
    pub data: String,
    pub id: Option<String>,
}

pub struct LocalSseStream {
    inner: Pin<Box<dyn futures::Stream<Item = Result<Bytes, hyper::Error>> + Send>>,
    buffer: Vec<u8>,
}

impl LocalSseStream {
    pub async fn next_event(&mut self) -> Result<Option<LocalSseEvent>, LocalIpcError> {
        use futures::StreamExt;

        loop {
            if let Some(frame_end) = frame_end(&self.buffer) {
                let frame = self.buffer.drain(..frame_end).collect::<Vec<_>>();
                if let Some(event) = parse_sse_frame(&frame)? {
                    return Ok(Some(event));
                }
                continue;
            }

            match self.inner.next().await {
                Some(Ok(chunk)) => {
                    self.buffer.extend_from_slice(&chunk);
                    if self.buffer.len() > MAX_SSE_EVENT_BYTES
                        && frame_end(&self.buffer)
                            .is_none_or(|frame_end| frame_end > MAX_SSE_EVENT_BYTES)
                    {
                        return Err(LocalIpcError::EventStream(format!(
                            "event exceeds the {MAX_SSE_EVENT_BYTES}-byte limit"
                        )));
                    }
                }
                Some(Err(error)) => return Err(LocalIpcError::Http(error)),
                None if self.buffer.is_empty() => return Ok(None),
                None => {
                    let frame = std::mem::take(&mut self.buffer);
                    return parse_sse_frame(&frame);
                }
            }
        }
    }
}

fn frame_end(buffer: &[u8]) -> Option<usize> {
    for index in 0..buffer.len() {
        if buffer[index..].starts_with(b"\n\n") {
            return Some(index + 2);
        }
        if buffer[index..].starts_with(b"\r\n\r\n") {
            return Some(index + 4);
        }
    }
    None
}

fn parse_sse_frame(frame: &[u8]) -> Result<Option<LocalSseEvent>, LocalIpcError> {
    let frame = std::str::from_utf8(frame)
        .map_err(|error| LocalIpcError::EventStream(error.to_string()))?;
    let mut event = None;
    let mut id = None;
    let mut data = Vec::new();
    for line in frame.lines() {
        let line = line.trim_end_matches('\r');
        if line.is_empty() || line.starts_with(':') {
            continue;
        }
        let (field, value) = line.split_once(':').map_or((line, ""), |(field, value)| {
            (field, value.strip_prefix(' ').unwrap_or(value))
        });
        match field {
            "event" => event = Some(value.to_string()),
            "data" => data.push(value),
            "id" => id = Some(value.to_string()),
            _ => {}
        }
    }
    if event.is_none() && data.is_empty() && id.is_none() {
        return Ok(None);
    }
    Ok(Some(LocalSseEvent {
        event,
        data: data.join("\n"),
        id,
    }))
}

#[cfg(unix)]
async fn connect(endpoint: &LocalIpcEndpoint) -> std::io::Result<tokio::net::UnixStream> {
    tokio::net::UnixStream::connect(endpoint.socket_path()).await
}

#[cfg(windows)]
async fn connect(
    endpoint: &LocalIpcEndpoint,
) -> std::io::Result<tokio::net::windows::named_pipe::NamedPipeClient> {
    use std::io::ErrorKind;
    use std::time::Duration;
    use tokio::net::windows::named_pipe::ClientOptions;

    const ERROR_PIPE_BUSY: i32 = 231;
    for _ in 0..20 {
        match ClientOptions::new().open(endpoint.pipe_name()) {
            Ok(client) => return Ok(client),
            Err(error) if error.raw_os_error() == Some(ERROR_PIPE_BUSY) => {
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            Err(error) => return Err(error),
        }
    }
    Err(std::io::Error::new(
        ErrorKind::TimedOut,
        "timed out waiting for the RiftX daemon named pipe",
    ))
}
