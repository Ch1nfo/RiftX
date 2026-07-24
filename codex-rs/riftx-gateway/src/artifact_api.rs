use crate::api::ApiError;
use crate::artifacts::CaptureError;
use crate::gateway_state::GatewayState;
use axum::Json;
use axum::body::Body;
use axum::extract::Path;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::Response;
use codex_riftx_artifacts::ArtifactError;
use codex_riftx_ipc::Artifact;
use codex_riftx_ipc::CaptureArtifactParams;
use tokio_util::io::ReaderStream;

pub(crate) async fn list(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
) -> Result<Json<Vec<Artifact>>, ApiError> {
    state.store.engagement(&id).await?;
    Ok(Json(state.store.artifacts(&id).await?))
}

pub(crate) async fn capture(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
    Json(params): Json<CaptureArtifactParams>,
) -> Result<(StatusCode, Json<Artifact>), ApiError> {
    let artifact = crate::artifacts::capture(
        &state,
        &id,
        &params.path,
        params.media_type.as_deref(),
        params.execution_id.as_deref(),
    )
    .await
    .map_err(map_capture_error)?;
    Ok((StatusCode::CREATED, Json(artifact)))
}

pub(crate) async fn export(
    State(state): State<GatewayState>,
    Path((id, artifact_id)): Path<(String, String)>,
) -> Result<Response, ApiError> {
    state.store.engagement(&id).await?;
    let artifact = state
        .store
        .artifacts(&id)
        .await?
        .into_iter()
        .find(|artifact| artifact.id == artifact_id)
        .ok_or_else(|| ApiError::not_found(format!("artifact {artifact_id} was not found")))?;
    let file = state
        .artifact_store
        .open(&artifact)
        .await
        .map_err(map_store_error)?;
    let filename = safe_filename(&artifact.path);
    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", artifact.media_type)
        .header("content-length", artifact.size_bytes)
        .header(
            "content-disposition",
            format!("attachment; filename=\"{filename}\""),
        )
        .body(Body::from_stream(ReaderStream::new(file)))
        .map_err(|error| ApiError::internal(error.to_string()))
}

fn map_capture_error(error: CaptureError) -> ApiError {
    match error {
        CaptureError::State(error) => error.into(),
        CaptureError::Store(error) => map_store_error(error),
        CaptureError::InvalidExecution(_) => ApiError::bad_request(error.to_string()),
    }
}

fn map_store_error(error: ArtifactError) -> ApiError {
    if error.is_request_error() {
        ApiError::bad_request(error.to_string())
    } else if matches!(
        error,
        ArtifactError::Crypto(_) | ArtifactError::CryptoTask(_)
    ) {
        ApiError::internal("encrypted artifact is unavailable")
    } else {
        ApiError::internal(error.to_string())
    }
}

fn safe_filename(path: &str) -> String {
    let filename = std::path::Path::new(path)
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("artifact");
    let sanitized = filename
        .chars()
        .take(128)
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '.' | '-' | '_') {
                character
            } else {
                '_'
            }
        })
        .collect::<String>();
    if sanitized.is_empty() {
        "artifact".to_string()
    } else {
        sanitized
    }
}
