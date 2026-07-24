use crate::bridge::DesktopError;
use crate::bridge::DesktopState;
use codex_riftx_credentials::LlmApiKey;
use codex_riftx_credentials::LlmCredentialStore;
use serde::Deserialize;
use serde::Serialize;
use std::path::Path;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct LlmSettingsView {
    model: String,
    base_url: String,
    credential_source: String,
    credential_name: String,
    configured: bool,
    daemon_restart_required: bool,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct SaveLlmApiKeyInput {
    api_key: String,
}

#[derive(Debug, Deserialize)]
struct SettingsConfig {
    llm: SettingsLlmConfig,
}

#[derive(Debug, Deserialize)]
struct SettingsLlmConfig {
    model: String,
    base_url: String,
    api_key: SettingsApiKeySource,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
#[serde(tag = "source", rename_all = "snake_case", deny_unknown_fields)]
enum SettingsApiKeySource {
    Keyring { profile: String },
    Environment { variable: String },
}

#[tauri::command]
pub(crate) async fn llm_settings(
    state: tauri::State<'_, DesktopState>,
) -> Result<LlmSettingsView, DesktopError> {
    let config = load_config(state.config_path()?).await?;
    settings_view(config, false).await
}

#[tauri::command]
pub(crate) async fn save_llm_api_key(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
    input: SaveLlmApiKeyInput,
) -> Result<LlmSettingsView, DesktopError> {
    let config = load_config(state.config_path()?).await?;
    let SettingsApiKeySource::Keyring { profile } = &config.llm.api_key else {
        return Err(read_only_source());
    };
    let profile = profile.clone();
    let api_key = LlmApiKey::new(input.api_key).map_err(credential_error)?;
    tokio::task::spawn_blocking(move || LlmCredentialStore::default().save(&profile, api_key))
        .await
        .map_err(|error| DesktopError::new("credential_store", error.to_string()))?
        .map_err(credential_error)?;
    let daemon_restart_required = state.daemon.reload_after_api_key_save(&app, &state).await?;
    settings_view(config, daemon_restart_required).await
}

#[tauri::command]
pub(crate) async fn delete_llm_api_key(
    state: tauri::State<'_, DesktopState>,
) -> Result<LlmSettingsView, DesktopError> {
    let config = load_config(state.config_path()?).await?;
    let SettingsApiKeySource::Keyring { profile } = &config.llm.api_key else {
        return Err(read_only_source());
    };
    let profile = profile.clone();
    tokio::task::spawn_blocking(move || LlmCredentialStore::default().delete(&profile))
        .await
        .map_err(|error| DesktopError::new("credential_store", error.to_string()))?
        .map_err(credential_error)?;
    let daemon_restart_required = state.daemon.stop_after_api_key_delete(&state).await?;
    settings_view(config, daemon_restart_required).await
}

async fn settings_view(
    config: SettingsConfig,
    daemon_restart_required: bool,
) -> Result<LlmSettingsView, DesktopError> {
    let (credential_source, credential_name, configured) = match &config.llm.api_key {
        SettingsApiKeySource::Keyring { profile } => {
            let credential_name = profile.clone();
            let profile = profile.clone();
            let configured = tokio::task::spawn_blocking(move || {
                LlmCredentialStore::default()
                    .load(&profile)
                    .map(|key| key.is_some())
            })
            .await
            .map_err(|error| DesktopError::new("credential_store", error.to_string()))?
            .map_err(credential_error)?;
            ("keyring".to_string(), credential_name, configured)
        }
        SettingsApiKeySource::Environment { variable } => (
            "environment".to_string(),
            variable.clone(),
            std::env::var_os(variable).is_some(),
        ),
    };
    Ok(LlmSettingsView {
        model: config.llm.model,
        base_url: config.llm.base_url,
        credential_source,
        credential_name,
        configured,
        daemon_restart_required,
    })
}

async fn load_config(path: &Path) -> Result<SettingsConfig, DesktopError> {
    let content = tokio::fs::read_to_string(path).await.map_err(|error| {
        DesktopError::new(
            "config_unavailable",
            format!("read {}: {error}", path.display()),
        )
    })?;
    toml::from_str(&content).map_err(|error| DesktopError::new("invalid_config", error.to_string()))
}

fn credential_error(error: impl std::fmt::Display) -> DesktopError {
    DesktopError::new("credential_store", error.to_string())
}

fn read_only_source() -> DesktopError {
    DesktopError::new(
        "credential_source_read_only",
        "Environment-backed API keys cannot be changed from RiftX Desktop",
    )
}

pub(crate) async fn sidecar_api_key(path: &Path) -> Result<Option<LlmApiKey>, DesktopError> {
    let config = load_config(path).await?;
    let SettingsApiKeySource::Keyring { profile } = config.llm.api_key else {
        return Ok(None);
    };
    let missing_profile = profile.clone();
    let api_key = tokio::task::spawn_blocking(move || LlmCredentialStore::default().load(&profile))
        .await
        .map_err(|error| DesktopError::new("credential_store", error.to_string()))?
        .map_err(credential_error)?
        .ok_or_else(|| {
            DesktopError::new(
                "credential_missing",
                format!("LLM API key profile {missing_profile:?} is not configured"),
            )
        })?;
    Ok(Some(api_key))
}

#[cfg(test)]
#[path = "settings_tests.rs"]
mod tests;
