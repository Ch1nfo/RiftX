use thiserror::Error;

#[derive(Debug, Error)]
pub enum BridgeError {
    #[error("{0}")]
    Unsupported(String),
    #[error("invalid Responses request: {0}")]
    InvalidRequest(String),
    #[error("upstream Chat Completions error: {0}")]
    Upstream(String),
    #[error(transparent)]
    Http(#[from] reqwest::Error),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
}
