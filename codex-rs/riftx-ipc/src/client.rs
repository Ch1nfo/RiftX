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
use thiserror::Error;

#[derive(Debug, Error)]
pub enum LocalIpcError {
    #[error("local IPC I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("local IPC HTTP failed: {0}")]
    Http(#[from] hyper::Error),
    #[error("invalid local IPC request: {0}")]
    Request(#[from] http::Error),
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
        };
        let response = sender.send_request(request.body(body)?).await?;
        Ok(LocalIpcResponse { inner: response })
    }
}

enum RequestBody {
    Empty,
    Json(Vec<u8>),
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

    pub fn into_data_stream(
        self,
    ) -> impl futures::Stream<Item = Result<Bytes, hyper::Error>> + Send {
        self.inner.into_body().into_data_stream()
    }
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
