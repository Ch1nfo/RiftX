use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use codex_riftx_artifacts::ArtifactError;
use codex_riftx_artifacts::CaptureArtifact;
use codex_riftx_core::Artifact;
use codex_riftx_core::Evidence;
use codex_riftx_core::ExecutionStatus;
use codex_riftx_core::StateError;
use serde_json::json;
use std::collections::BTreeSet;
use std::path::Path;
use thiserror::Error;
use uuid::Uuid;

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
    capture_pending_inner(state, engagement_id, None).await;
}

pub(crate) async fn capture_pending_for_turn(
    state: GatewayState,
    engagement_id: String,
    turn_id: &str,
) {
    let linked_execution_ids = match state.store.evidence(&engagement_id).await {
        Ok(evidence) => evidence
            .into_iter()
            .filter_map(|evidence| evidence.execution_id)
            .collect::<BTreeSet<_>>(),
        Err(error) => {
            state
                .publish(
                    &engagement_id,
                    "evidence/captureFailed",
                    json!({"message": error.to_string()}),
                )
                .await;
            capture_pending_inner(state, engagement_id, None).await;
            return;
        }
    };
    let execution_id = match state.store.executions(&engagement_id).await {
        Ok(executions) => executions
            .into_iter()
            .filter(|execution| {
                execution.turn_id == turn_id
                    && matches!(
                        execution.status,
                        ExecutionStatus::Completed
                            | ExecutionStatus::Failed
                            | ExecutionStatus::Interrupted
                    )
                    && !linked_execution_ids.contains(&execution.id)
            })
            .map(|execution| execution.id)
            .collect::<Vec<_>>(),
        Err(error) => {
            state
                .publish(
                    &engagement_id,
                    "evidence/captureFailed",
                    json!({"message": error.to_string()}),
                )
                .await;
            capture_pending_inner(state, engagement_id, None).await;
            return;
        }
    };
    let execution_id = (execution_id.len() == 1)
        .then(|| execution_id.into_iter().next())
        .flatten();
    capture_pending_inner(state, engagement_id, execution_id.as_deref()).await;
}

async fn capture_pending_inner(
    state: GatewayState,
    engagement_id: String,
    execution_id: Option<&str>,
) {
    let existing = match state.store.artifacts(&engagement_id).await {
        Ok(existing) => existing,
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
    let mut known_artifacts = existing
        .into_iter()
        .map(|artifact| (artifact.path, artifact.sha256))
        .collect::<BTreeSet<_>>();
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
        match capture(&state, &engagement_id, &path, None, None).await {
            Ok(artifact) => {
                let key = (artifact.path.clone(), artifact.sha256.clone());
                if known_artifacts.insert(key)
                    && let Some(execution_id) = execution_id
                {
                    record_artifact_evidence(&state, &artifact, execution_id).await;
                }
            }
            Err(error) => {
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
}

async fn record_artifact_evidence(state: &GatewayState, artifact: &Artifact, execution_id: &str) {
    if state
        .store
        .evidence(&artifact.engagement_id)
        .await
        .is_ok_and(|evidence| {
            evidence.iter().any(|item| {
                item.execution_id.as_deref() == Some(execution_id)
                    && item.artifact_id.as_deref() == Some(artifact.id.as_str())
            })
        })
    {
        return;
    }
    let evidence = Evidence {
        id: Uuid::new_v4().to_string(),
        engagement_id: artifact.engagement_id.clone(),
        finding_id: None,
        execution_id: Some(execution_id.to_string()),
        artifact_id: Some(artifact.id.clone()),
        summary: format!(
            "Artifact {} captured from terminal execution {execution_id}",
            artifact.path
        ),
        reproducible: false,
        captured_at: unix_timestamp(),
    };
    if let Err(error) = state.store.put_evidence(&evidence).await {
        state
            .publish(
                &artifact.engagement_id,
                "evidence/captureFailed",
                json!({"artifactId": artifact.id, "message": error.to_string()}),
            )
            .await;
        return;
    }
    state
        .publish(
            &artifact.engagement_id,
            "evidence/captured",
            serde_json::to_value(evidence).unwrap_or_default(),
        )
        .await;
}
