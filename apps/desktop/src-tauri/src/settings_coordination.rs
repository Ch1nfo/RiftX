use crate::bridge::DesktopError;
use crate::bridge::DesktopState;
use crate::bridge::EngagementView;
use crate::bridge::json_response;
use codex_riftx_ipc::ActiveTurnStatus;
use codex_riftx_ipc::DaemonControlStatus;
use serde::Deserialize;
use serde::Serialize;
use std::collections::BTreeMap;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct SettingsReloadImpactView {
    active_turns: Vec<SettingsAffectedEngagementView>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
struct SettingsAffectedEngagementView {
    engagement_id: String,
    engagement_name: String,
    profile_name: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct PrepareSettingsReloadInput {
    expected_engagement_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct SettingsReloadPreparationView {
    runtime: DaemonControlStatus,
    interrupted_engagement_ids: Vec<String>,
}

#[tauri::command]
pub(crate) async fn settings_reload_impact(
    state: tauri::State<'_, DesktopState>,
) -> Result<SettingsReloadImpactView, DesktopError> {
    let active_turns = state.query_active_turns().await?;
    if active_turns.is_empty() {
        return Ok(settings_reload_impact_view(active_turns, Vec::new()));
    }
    let client = state.client()?;
    let engagements: Vec<EngagementView> =
        json_response(client.get("/v1/engagements").await).await?;
    Ok(settings_reload_impact_view(active_turns, engagements))
}

#[tauri::command]
pub(crate) async fn prepare_settings_reload(
    state: tauri::State<'_, DesktopState>,
    input: PrepareSettingsReloadInput,
) -> Result<SettingsReloadPreparationView, DesktopError> {
    let active_turns = state.query_active_turns().await?;
    let mut interrupted_engagement_ids = active_turns
        .into_iter()
        .map(|turn| turn.engagement_id)
        .collect::<Vec<_>>();
    interrupted_engagement_ids.sort();
    interrupted_engagement_ids.dedup();
    validate_expected_engagements(
        input.expected_engagement_ids,
        &interrupted_engagement_ids,
    )?;
    let runtime = if interrupted_engagement_ids.is_empty() {
        state.query_runtime_status().await?
    } else {
        state.update_runtime("/v1/system/pause").await?
    };
    let remaining = state.query_active_turns().await?;
    if !remaining.is_empty() {
        return Err(DesktopError::new(
            "settings_pause_incomplete",
            "RiftX could not pause all active turns; review the affected tasks and retry",
        ));
    }
    Ok(SettingsReloadPreparationView {
        runtime,
        interrupted_engagement_ids,
    })
}

fn validate_expected_engagements(
    mut expected: Vec<String>,
    actual: &[String],
) -> Result<(), DesktopError> {
    expected.sort();
    expected.dedup();
    if expected == actual {
        return Ok(());
    }
    Err(DesktopError::new(
        "settings_impact_changed",
        "Affected active tasks changed before confirmation; review the updated list and retry",
    ))
}

fn settings_reload_impact_view(
    active_turns: Vec<ActiveTurnStatus>,
    engagements: Vec<EngagementView>,
) -> SettingsReloadImpactView {
    let names = engagements
        .into_iter()
        .map(|engagement| (engagement.id, engagement.name))
        .collect::<BTreeMap<_, _>>();
    let mut active_turns = active_turns
        .into_iter()
        .map(|turn| SettingsAffectedEngagementView {
            engagement_name: names
                .get(&turn.engagement_id)
                .cloned()
                .unwrap_or_else(|| turn.engagement_id.clone()),
            engagement_id: turn.engagement_id,
            profile_name: turn.profile_name,
        })
        .collect::<Vec<_>>();
    active_turns.sort_by(|left, right| left.engagement_id.cmp(&right.engagement_id));
    active_turns.dedup_by(|left, right| left.engagement_id == right.engagement_id);
    SettingsReloadImpactView { active_turns }
}

#[cfg(test)]
#[path = "settings_coordination_tests.rs"]
mod tests;
