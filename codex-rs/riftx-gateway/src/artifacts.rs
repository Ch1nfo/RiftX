use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use codex_riftx_artifacts::ArtifactError;
use codex_riftx_artifacts::CaptureArtifact;
use codex_riftx_core::Artifact;
use codex_riftx_core::StateError;
use serde_json::json;
use std::path::Path;
use thiserror::Error;

#[derive(Debug, Error)]
pub(crate) enum CaptureError {
    #[error(transparent)]
    State(#[from] StateError),
    #[error(transparent)]
    Store(#[from] ArtifactError),
    #[error("execution {0} does not belong to this engagement")]
    InvalidExecution(String),
}

pub(crate) async fn capture(
    state: &GatewayState,
    engagement_id: &str,
    relative_path: &Path,
    media_type: Option<&str>,
    execution_id: Option<&str>,
) -> Result<Artifact, CaptureError> {
    state.store.engagement(engagement_id).await?;
    if let Some(execution_id) = execution_id
        && !state
            .store
            .executions(engagement_id)
            .await?
            .iter()
            .any(|execution| execution.id == execution_id)
    {
        return Err(CaptureError::InvalidExecution(execution_id.to_string()));
    }
    let existing = state.store.artifacts(engagement_id).await?;
    let workspace = state.config.daemon.workspace_root.join(engagement_id);
    let candidate = state
        .artifact_store
        .capture(CaptureArtifact {
            engagement_id,
            workspace: &workspace,
            relative_path,
            media_type,
            execution_id,
            existing: &existing,
            created_at: unix_timestamp(),
        })
        .await?;
    if let Some(existing) = existing.into_iter().find(|artifact| {
        artifact.path == candidate.path
            && artifact.sha256 == candidate.sha256
            && artifact.execution_id == candidate.execution_id
    }) {
        return Ok(existing);
    }
    state.store.put_artifact(&candidate).await?;
    state
        .publish(
            engagement_id,
            "artifact/captured",
            serde_json::to_value(&candidate).unwrap_or_default(),
        )
        .await;
    Ok(candidate)
}

pub(crate) async fn capture_pending(state: GatewayState, engagement_id: String) {
    let workspace = state.config.daemon.workspace_root.join(&engagement_id);
    let paths = match state.artifact_store.discover(&workspace) {
        Ok(paths) => paths,
        Err(error) => {
            state
                .publish(
                    &engagement_id,
                    "artifact/captureFailed",
                    json!({"message": error.to_string()}),
                )
                .await;
            return;
        }
    };
    for path in paths {
        if let Err(error) = capture(&state, &engagement_id, &path, None, None).await {
            state
                .publish(
                    &engagement_id,
                    "artifact/captureFailed",
                    json!({"path": path, "message": error.to_string()}),
                )
                .await;
        }
    }
}
