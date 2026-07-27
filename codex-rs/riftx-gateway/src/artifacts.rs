use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use codex_riftx_artifacts::ArtifactError;
use codex_riftx_artifacts::ArtifactQuota;
use codex_riftx_artifacts::CaptureArtifact;
use codex_riftx_core::Artifact;
use codex_riftx_core::AutoRunState;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::Evidence;
use codex_riftx_core::EvidencePurpose;
use codex_riftx_core::ExecutionMode;
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
    let _ = capture_pending_inner(state, engagement_id, None).await;
}

pub(crate) async fn capture_pending_for_turn(
    state: GatewayState,
    engagement_id: String,
    turn_id: &str,
) -> bool {
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
            let quota = capture_pending_inner(state.clone(), engagement_id.clone(), None).await;
            pause_auto_for_artifact_quota(&state, &engagement_id, quota).await;
            return quota.is_some();
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
            let quota = capture_pending_inner(state.clone(), engagement_id.clone(), None).await;
            pause_auto_for_artifact_quota(&state, &engagement_id, quota).await;
            return quota.is_some();
        }
    };
    let execution_id = (execution_id.len() == 1)
        .then(|| execution_id.into_iter().next())
        .flatten();
    let quota = capture_pending_inner(
        state.clone(),
        engagement_id.clone(),
        execution_id.as_deref(),
    )
    .await;
    pause_auto_for_artifact_quota(&state, &engagement_id, quota).await;
    quota.is_some()
}

async fn capture_pending_inner(
    state: GatewayState,
    engagement_id: String,
    execution_id: Option<&str>,
) -> Option<ArtifactQuota> {
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
            return None;
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
            if let Some(quota) = error.quota_exceeded() {
                publish_artifact_quota_exhausted(&state, &engagement_id, quota).await;
                return Some(quota);
            }
            state
                .publish(
                    &engagement_id,
                    "artifact/captureFailed",
                    json!({"message": error.to_string()}),
                )
                .await;
            return None;
        }
    };
    let mut exhausted_quota = None;
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
                if let CaptureError::Store(store_error) = &error
                    && let Some(quota) = store_error.quota_exceeded()
                {
                    exhausted_quota.get_or_insert(quota);
                    continue;
                }
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
    if let Some(quota) = exhausted_quota {
        publish_artifact_quota_exhausted(&state, &engagement_id, quota).await;
    }
    exhausted_quota
}

async fn publish_artifact_quota_exhausted(
    state: &GatewayState,
    engagement_id: &str,
    quota: ArtifactQuota,
) {
    state
        .publish(
            engagement_id,
            "artifact/quotaExhausted",
            artifact_quota_data(quota),
        )
        .await;
}

async fn pause_auto_for_artifact_quota(
    state: &GatewayState,
    engagement_id: &str,
    quota: Option<ArtifactQuota>,
) {
    let Some(quota) = quota else {
        return;
    };
    let Ok(_permit) = state.turn_slot.clone().acquire_owned().await else {
        state
            .emit_event(
                engagement_id,
                "auto/controllerError",
                json!({"message": "artifact quota state could not be coordinated"}),
            )
            .await;
        return;
    };
    let engagement = match state.store.engagement(engagement_id).await {
        Ok(engagement) => engagement,
        Err(error) => {
            state
                .emit_event(
                    engagement_id,
                    "auto/controllerError",
                    json!({"message": error.to_string()}),
                )
                .await;
            return;
        }
    };
    if engagement.mode != ExecutionMode::Auto || engagement.status != EngagementStatus::Active {
        return;
    }
    let run = match state.store.auto_run(engagement_id).await {
        Ok(Some(run)) => run,
        Ok(None) => return,
        Err(error) => {
            state
                .emit_event(
                    engagement_id,
                    "auto/controllerError",
                    json!({"message": error.to_string()}),
                )
                .await;
            return;
        }
    };
    if !matches!(run.state, AutoRunState::Running | AutoRunState::Evaluating) {
        return;
    }
    let mut audit_data = artifact_quota_data(quota);
    audit_data["reason"] = json!(codex_riftx_core::AutoStopReason::ArtifactQuotaExhausted);
    if state
        .append_engagement_critical(&engagement, "auto/artifactQuotaExhausted", &audit_data)
        .await
        .is_err()
    {
        if let Err(error) = crate::auto_run::lifecycle_stop(
            state,
            engagement_id,
            crate::auto_run::AutoLifecycleStop::AuditUnavailable,
        )
        .await
        {
            state
                .emit_event(
                    engagement_id,
                    "auto/controllerError",
                    json!({"message": error.to_string()}),
                )
                .await;
        }
        return;
    }
    if let Err(error) = crate::auto_run::lifecycle_stop(
        state,
        engagement_id,
        crate::auto_run::AutoLifecycleStop::ArtifactQuotaExhausted,
    )
    .await
    {
        state
            .emit_event(
                engagement_id,
                "auto/controllerError",
                json!({"message": error.to_string()}),
            )
            .await;
    }
}

fn artifact_quota_data(quota: ArtifactQuota) -> serde_json::Value {
    match quota {
        ArtifactQuota::Bytes { limit } => json!({"quota": "bytes", "limitBytes": limit}),
        ArtifactQuota::Count { limit } => json!({"quota": "count", "limitCount": limit}),
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
        purpose: EvidencePurpose::Objective,
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
