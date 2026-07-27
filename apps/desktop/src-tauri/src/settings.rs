use crate::bridge::DesktopError;
use crate::bridge::DesktopState;
use crate::bridge::EngagementView;
use crate::bridge::json_response;
use codex_riftx_core::LlmApiKeySource;
use codex_riftx_core::LlmProfileConfig;
use codex_riftx_core::LlmProtocol;
use codex_riftx_core::LlmReasoningLevel;
use codex_riftx_core::RiftxConfig;
use codex_riftx_credentials::LlmApiKey;
use codex_riftx_credentials::LlmCredentialStore;
use serde::Deserialize;
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::Path;
use std::path::PathBuf;

const DEFAULT_TIMEOUT_SECONDS: u64 = 300;
const DEFAULT_CONTEXT_BUDGET: u32 = 200_000;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct LlmSettingsView {
    default_profile: String,
    profiles: Vec<LlmProfileSettingsView>,
    daemon_restart_required: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
struct LlmProfileSettingsView {
    profile_name: String,
    protocol: String,
    model: String,
    base_url: String,
    timeout_seconds: u64,
    reasoning_level: String,
    context_budget: u32,
    credential_source: String,
    credential_name: String,
    configured: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ToolsSettingsView {
    directories: Vec<String>,
    daemon_restart_required: bool,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct SaveLlmApiKeyInput {
    profile_name: String,
    api_key: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct SelectLlmProfileInput {
    profile_name: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct SaveToolsSettingsInput {
    directories: Vec<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct UpsertLlmProfileInput {
    profile_name: String,
    model: String,
    base_url: String,
    #[serde(default)]
    protocol: Option<String>,
    #[serde(default)]
    make_default: bool,
}

#[tauri::command]
pub(crate) async fn llm_settings(
    state: tauri::State<'_, DesktopState>,
) -> Result<LlmSettingsView, DesktopError> {
    let config = load_riftx_config(state.config_path()?).await?;
    settings_view(&config, false).await
}

#[tauri::command]
pub(crate) async fn get_tools_settings(
    state: tauri::State<'_, DesktopState>,
) -> Result<ToolsSettingsView, DesktopError> {
    let config = load_riftx_config(state.config_path()?).await?;
    Ok(tools_settings_view(&config, false))
}

#[tauri::command]
pub(crate) async fn save_tools_settings(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
    input: SaveToolsSettingsInput,
) -> Result<ToolsSettingsView, DesktopError> {
    let path = state.config_path()?;
    let mut config = load_riftx_config(path).await?;
    config.tools.directories = input
        .directories
        .into_iter()
        .map(|directory| directory.trim().to_string())
        .filter(|directory| !directory.is_empty())
        .map(PathBuf::from)
        .collect();
    config
        .validate()
        .map_err(|error| DesktopError::new("invalid_config", error.to_string()))?;
    write_riftx_config(path, &config).await?;
    let daemon_restart_required = state.daemon.reload_after_api_key_save(&app, &state).await?;
    Ok(tools_settings_view(&config, daemon_restart_required))
}

#[tauri::command]
pub(crate) async fn upsert_llm_profile(
    state: tauri::State<'_, DesktopState>,
    input: UpsertLlmProfileInput,
) -> Result<LlmSettingsView, DesktopError> {
    let path = state.config_path()?;
    let mut config = load_riftx_config(path).await?;
    let profile_name = input.profile_name.trim().to_string();
    if profile_name.is_empty() {
        return Err(DesktopError::new(
            "invalid_config",
            "profile name is required",
        ));
    }
    let model = input.model.trim().to_string();
    let base_url = input.base_url.trim().to_string();
    let protocol = parse_protocol(input.protocol.as_deref())?;
    let updating_existing = config.llm.profiles.contains_key(&profile_name);
    let was_configured = if updating_existing {
        profile_is_configured(&config, &profile_name).await?
    } else {
        false
    };
    if let Some(existing) = config.llm.profiles.get_mut(&profile_name) {
        existing.model = model;
        existing.base_url = base_url;
        existing.protocol = protocol;
    } else {
        config.llm.profiles.insert(
            profile_name.clone(),
            LlmProfileConfig {
                protocol,
                model,
                base_url,
                api_key: LlmApiKeySource::Keyring {
                    credential: profile_name.clone(),
                },
                timeout_seconds: DEFAULT_TIMEOUT_SECONDS,
                reasoning_level: LlmReasoningLevel::High,
                context_budget: DEFAULT_CONTEXT_BUDGET,
            },
        );
    }
    if input.make_default || config.llm.profiles.len() == 1 {
        config.llm.default_profile = profile_name;
    }
    config
        .validate()
        .map_err(|error| DesktopError::new("invalid_config", error.to_string()))?;
    write_riftx_config(path, &config).await?;
    // Never restart here: new profiles are often still missing a key (G-02), and
    // daemon startup skips unconfigured profiles. Restart only after save_llm_api_key
    // or when the operator changes tools directories.
    settings_view(&config, was_configured).await
}

#[tauri::command]
pub(crate) async fn delete_llm_profile(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
    input: SelectLlmProfileInput,
) -> Result<LlmSettingsView, DesktopError> {
    let path = state.config_path()?;
    let mut config = load_riftx_config(path).await?;
    let profile_name = input.profile_name.trim();
    if profile_name.is_empty() {
        return Err(DesktopError::new(
            "invalid_config",
            "profile name is required",
        ));
    }
    if config.llm.profiles.len() <= 1 {
        return Err(DesktopError::new(
            "invalid_config",
            "at least one LLM profile is required",
        ));
    }
    if config.llm.default_profile == profile_name {
        return Err(DesktopError::new(
            "invalid_config",
            "set another default profile before deleting the current default",
        ));
    }
    reject_if_profile_in_use(&state, profile_name).await?;
    let removed = config.llm.profiles.remove(profile_name).ok_or_else(|| {
        DesktopError::new(
            "invalid_config",
            format!("LLM profile {profile_name:?} is not configured"),
        )
    })?;
    config
        .validate()
        .map_err(|error| DesktopError::new("invalid_config", error.to_string()))?;
    write_riftx_config(path, &config).await?;
    if let LlmApiKeySource::Keyring { credential } = removed.api_key
        && !credential_still_referenced(&config, &credential)
    {
        let credential = credential.clone();
        let _ =
            tokio::task::spawn_blocking(move || LlmCredentialStore::default().delete(&credential))
                .await
                .map_err(|error| DesktopError::new("credential_store", error.to_string()))?;
    }
    let daemon_restart_required = state.daemon.reload_after_api_key_save(&app, &state).await?;
    settings_view(&config, daemon_restart_required).await
}

#[tauri::command]
pub(crate) async fn set_default_llm_profile(
    state: tauri::State<'_, DesktopState>,
    input: SelectLlmProfileInput,
) -> Result<LlmSettingsView, DesktopError> {
    let path = state.config_path()?;
    let mut config = load_riftx_config(path).await?;
    let profile_name = input.profile_name.trim().to_string();
    if !config.llm.profiles.contains_key(&profile_name) {
        return Err(DesktopError::new(
            "invalid_config",
            format!("LLM profile {profile_name:?} is not configured"),
        ));
    }
    config.llm.default_profile = profile_name;
    config
        .validate()
        .map_err(|error| DesktopError::new("invalid_config", error.to_string()))?;
    write_riftx_config(path, &config).await?;
    settings_view(&config, false).await
}

#[tauri::command]
pub(crate) async fn save_llm_api_key(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
    input: SaveLlmApiKeyInput,
) -> Result<LlmSettingsView, DesktopError> {
    let config = load_riftx_config(state.config_path()?).await?;
    let profile = profile(&config, &input.profile_name)?;
    let LlmApiKeySource::Keyring { credential } = &profile.api_key else {
        return Err(read_only_source());
    };
    let credential = credential.clone();
    let api_key = LlmApiKey::new(input.api_key).map_err(credential_error)?;
    tokio::task::spawn_blocking(move || LlmCredentialStore::default().save(&credential, api_key))
        .await
        .map_err(|error| DesktopError::new("credential_store", error.to_string()))?
        .map_err(credential_error)?;
    let daemon_restart_required = state.daemon.reload_after_api_key_save(&app, &state).await?;
    settings_view(&config, daemon_restart_required).await
}

#[tauri::command]
pub(crate) async fn delete_llm_api_key(
    state: tauri::State<'_, DesktopState>,
    input: SelectLlmProfileInput,
) -> Result<LlmSettingsView, DesktopError> {
    let config = load_riftx_config(state.config_path()?).await?;
    let profile = profile(&config, &input.profile_name)?;
    let LlmApiKeySource::Keyring { credential } = &profile.api_key else {
        return Err(read_only_source());
    };
    let credential = credential.clone();
    tokio::task::spawn_blocking(move || LlmCredentialStore::default().delete(&credential))
        .await
        .map_err(|error| DesktopError::new("credential_store", error.to_string()))?
        .map_err(credential_error)?;
    let daemon_restart_required = state.daemon.stop_after_api_key_delete(&state).await?;
    settings_view(&config, daemon_restart_required).await
}

pub(crate) async fn load_riftx_config(path: &Path) -> Result<RiftxConfig, DesktopError> {
    RiftxConfig::load_migrating(path)
        .await
        .map_err(|error| DesktopError::new("invalid_config", error.to_string()))
}

pub(crate) async fn write_riftx_config(
    path: &Path,
    config: &RiftxConfig,
) -> Result<(), DesktopError> {
    config.write_atomic(path).await.map_err(|error| {
        DesktopError::new(
            "config_unavailable",
            format!("write {}: {error}", path.display()),
        )
    })
}

pub(crate) fn tools_settings_view(
    config: &RiftxConfig,
    daemon_restart_required: bool,
) -> ToolsSettingsView {
    ToolsSettingsView {
        directories: config
            .tools
            .directories
            .iter()
            .map(|path| path.display().to_string())
            .collect(),
        daemon_restart_required,
    }
}

async fn settings_view(
    config: &RiftxConfig,
    daemon_restart_required: bool,
) -> Result<LlmSettingsView, DesktopError> {
    let default_profile = config.llm.default_profile.clone();
    if !config.llm.profiles.contains_key(&default_profile) {
        return Err(DesktopError::new(
            "invalid_config",
            format!("default LLM profile {default_profile:?} is not configured"),
        ));
    }
    let profiles = config.llm.profiles.clone();
    let profiles = tokio::task::spawn_blocking(move || {
        let store = LlmCredentialStore::default();
        profiles
            .into_iter()
            .map(|(profile_name, profile)| {
                let (credential_source, credential_name, configured) = match &profile.api_key {
                    LlmApiKeySource::Keyring { credential } => (
                        "keyring".to_string(),
                        credential.clone(),
                        store
                            .load(credential)
                            .map(|key| key.is_some())
                            .map_err(credential_error)?,
                    ),
                    LlmApiKeySource::Environment { variable } => (
                        "environment".to_string(),
                        variable.clone(),
                        std::env::var_os(variable).is_some(),
                    ),
                };
                Ok(LlmProfileSettingsView {
                    profile_name,
                    protocol: profile.protocol.as_str().to_string(),
                    model: profile.model,
                    base_url: profile.base_url,
                    timeout_seconds: profile.timeout_seconds,
                    reasoning_level: reasoning_level_label(profile.reasoning_level).to_string(),
                    context_budget: profile.context_budget,
                    credential_source,
                    credential_name,
                    configured,
                })
            })
            .collect::<Result<Vec<_>, DesktopError>>()
    })
    .await
    .map_err(|error| DesktopError::new("credential_store", error.to_string()))??;
    Ok(LlmSettingsView {
        default_profile,
        profiles,
        daemon_restart_required,
    })
}

fn profile<'a>(
    config: &'a RiftxConfig,
    profile_name: &str,
) -> Result<&'a LlmProfileConfig, DesktopError> {
    config.llm.profiles.get(profile_name).ok_or_else(|| {
        DesktopError::new(
            "invalid_config",
            format!("LLM profile {profile_name:?} is not configured"),
        )
    })
}

fn credential_still_referenced(config: &RiftxConfig, credential: &str) -> bool {
    config.llm.profiles.values().any(|profile| {
        matches!(
            &profile.api_key,
            LlmApiKeySource::Keyring { credential: name } if name == credential
        )
    })
}

async fn profile_is_configured(
    config: &RiftxConfig,
    profile_name: &str,
) -> Result<bool, DesktopError> {
    let profile = profile(config, profile_name)?;
    match &profile.api_key {
        LlmApiKeySource::Keyring { credential } => {
            let credential = credential.clone();
            tokio::task::spawn_blocking(move || {
                LlmCredentialStore::default()
                    .load(&credential)
                    .map(|key| key.is_some())
                    .map_err(credential_error)
            })
            .await
            .map_err(|error| DesktopError::new("credential_store", error.to_string()))?
        }
        LlmApiKeySource::Environment { variable } => Ok(std::env::var_os(variable).is_some()),
    }
}

async fn reject_if_profile_in_use(
    state: &DesktopState,
    profile_name: &str,
) -> Result<(), DesktopError> {
    let Ok(client) = state.client() else {
        return Ok(());
    };
    let engagements: Vec<EngagementView> =
        json_response(client.get("/v1/engagements").await).await?;
    if engagements
        .iter()
        .any(|engagement| engagement.llm_profile == profile_name)
    {
        return Err(DesktopError::new(
            "profile_in_use",
            format!(
                "LLM profile {profile_name:?} is referenced by an existing engagement and cannot be deleted"
            ),
        ));
    }
    Ok(())
}

fn parse_protocol(value: Option<&str>) -> Result<LlmProtocol, DesktopError> {
    match value.map(str::trim).filter(|value| !value.is_empty()) {
        None | Some("responses") => Ok(LlmProtocol::Responses),
        Some("chat_completions") => Ok(LlmProtocol::ChatCompletions),
        Some(other) => Err(DesktopError::new(
            "invalid_config",
            format!("unsupported LLM protocol {other:?}; expected responses or chat_completions"),
        )),
    }
}

fn reasoning_level_label(level: LlmReasoningLevel) -> &'static str {
    match level {
        LlmReasoningLevel::Minimal => "minimal",
        LlmReasoningLevel::Low => "low",
        LlmReasoningLevel::Medium => "medium",
        LlmReasoningLevel::High => "high",
        LlmReasoningLevel::XHigh => "x_high",
    }
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
    let config = load_riftx_config(path).await?;
    let keyring_profiles = config
        .llm
        .profiles
        .into_iter()
        .filter_map(|(profile_name, profile)| match profile.api_key {
            LlmApiKeySource::Keyring { credential } => Some((profile_name, credential)),
            LlmApiKeySource::Environment { .. } => None,
        })
        .collect::<Vec<_>>();
    tokio::task::spawn_blocking(move || {
        let store = LlmCredentialStore::default();
        let mut api_keys = BTreeMap::new();
        for (profile_name, credential) in keyring_profiles {
            match store.load(&credential).map_err(credential_error)? {
                Some(api_key) => {
                    api_keys.insert(profile_name, api_key);
                }
                None => {
                    // Unconfigured profiles must not block daemon startup (G-03).
                }
            }
        }
        Ok(api_keys)
    })
    .await
    .map_err(|error| DesktopError::new("credential_store", error.to_string()))?
}

#[cfg(test)]
#[path = "settings_tests.rs"]
mod tests;
