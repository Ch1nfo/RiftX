use crate::bridge::DesktopError;
use crate::bridge::DesktopState;
use codex_riftx_credentials::LlmApiKey;
use codex_riftx_credentials::LlmCredentialStore;
use serde::Deserialize;
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::Path;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct LlmSettingsView {
    profile_name: String,
    model: String,
    base_url: String,
    timeout_seconds: u64,
    reasoning_level: String,
    context_budget: u32,
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
#[serde(deny_unknown_fields)]
struct SettingsLlmConfig {
    default_profile: String,
    profiles: BTreeMap<String, SettingsLlmProfileConfig>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct SettingsLlmProfileConfig {
    model: String,
    base_url: String,
    api_key: SettingsApiKeySource,
    timeout_seconds: u64,
    reasoning_level: String,
    context_budget: u32,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(tag = "source", rename_all = "snake_case", deny_unknown_fields)]
enum SettingsApiKeySource {
    Keyring { credential: String },
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
    let (_, profile) = default_profile(&config)?;
    let SettingsApiKeySource::Keyring { credential } = &profile.api_key else {
        return Err(read_only_source());
    };
    let credential = credential.clone();
    let api_key = LlmApiKey::new(input.api_key).map_err(credential_error)?;
    tokio::task::spawn_blocking(move || LlmCredentialStore::default().save(&credential, api_key))
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
    let (_, profile) = default_profile(&config)?;
    let SettingsApiKeySource::Keyring { credential } = &profile.api_key else {
        return Err(read_only_source());
    };
    let credential = credential.clone();
    tokio::task::spawn_blocking(move || LlmCredentialStore::default().delete(&credential))
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
    let (profile_name, profile) = default_profile(&config)?;
    let (credential_source, credential_name, configured) = match &profile.api_key {
        SettingsApiKeySource::Keyring { credential } => {
            let credential_name = credential.clone();
            let credential = credential.clone();
            let configured = tokio::task::spawn_blocking(move || {
                LlmCredentialStore::default()
                    .load(&credential)
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
        profile_name: profile_name.to_string(),
        model: profile.model.clone(),
        base_url: profile.base_url.clone(),
        timeout_seconds: profile.timeout_seconds,
        reasoning_level: profile.reasoning_level.clone(),
        context_budget: profile.context_budget,
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

fn default_profile(
    config: &SettingsConfig,
) -> Result<(&str, &SettingsLlmProfileConfig), DesktopError> {
    let profile_name = config.llm.default_profile.as_str();
    let profile = config.llm.profiles.get(profile_name).ok_or_else(|| {
        DesktopError::new(
            "invalid_config",
            format!("default LLM profile {profile_name:?} is not configured"),
        )
    })?;
    Ok((profile_name, profile))
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

pub(crate) async fn sidecar_api_keys(
    path: &Path,
) -> Result<BTreeMap<String, LlmApiKey>, DesktopError> {
    let config = load_config(path).await?;
    let keyring_profiles = config
        .llm
        .profiles
        .into_iter()
        .filter_map(|(profile_name, profile)| match profile.api_key {
            SettingsApiKeySource::Keyring { credential } => Some((profile_name, credential)),
            SettingsApiKeySource::Environment { .. } => None,
        })
        .collect::<Vec<_>>();
    tokio::task::spawn_blocking(move || {
        let store = LlmCredentialStore::default();
        let mut api_keys = BTreeMap::new();
        for (profile_name, credential) in keyring_profiles {
            let api_key = store
                .load(&credential)
                .map_err(credential_error)?
                .ok_or_else(|| {
                    DesktopError::new(
                        "credential_missing",
                        format!("LLM API key credential {credential:?} is not configured"),
                    )
                })?;
            api_keys.insert(profile_name, api_key);
        }
        Ok(api_keys)
    })
    .await
    .map_err(|error| DesktopError::new("credential_store", error.to_string()))?
}

#[cfg(test)]
#[path = "settings_tests.rs"]
mod tests;
