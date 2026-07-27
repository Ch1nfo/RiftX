use crate::api::ApiError;
use crate::gateway_state::GatewayState;
use axum::Json;
use axum::extract::Path;
use axum::extract::State;
use codex_riftx_app_server_adapter::RiftxApiKey;
use codex_riftx_core::LlmApiKeySource;
use codex_riftx_core::LlmProtocol;
use codex_riftx_credentials::LlmApiKey;
use codex_riftx_credentials::LlmCredentialStore;
use codex_riftx_ipc::LlmCapabilityCheck;
use codex_riftx_ipc::LlmCapabilityMatrix;
use codex_riftx_ipc::LlmCheckStatus;
use codex_riftx_ipc::LlmConnectionTestResult;
use codex_riftx_ipc::LlmProfileList;
use codex_riftx_ipc::LlmProfileState;
use codex_riftx_ipc::LlmProfileSummary;
use codex_riftx_llm_bridge::ProbeProtocol;
use codex_riftx_llm_bridge::ProbeTarget;
use codex_riftx_llm_bridge::probe_connection;
use codex_riftx_llm_bridge::sanitize_error;
use std::time::Duration;

pub(crate) async fn list_profiles(
    State(state): State<GatewayState>,
) -> Result<Json<LlmProfileList>, ApiError> {
    let engagements = state.store.engagements().await?;
    let profiles = state
        .config
        .llm
        .profiles
        .iter()
        .map(|(name, profile)| {
            let configured = key_is_configured(name, &profile.api_key);
            let runtime_ready = state.app_servers.contains_key(name);
            let in_use = engagements
                .iter()
                .any(|engagement| engagement.llm_profile == *name);
            let (profile_state, state_detail) = if in_use {
                (
                    LlmProfileState::InUse,
                    "referenced by an existing engagement",
                )
            } else if !configured {
                (LlmProfileState::Unconfigured, "API key is not configured")
            } else if runtime_ready {
                (LlmProfileState::Ready, "Runtime is ready")
            } else {
                (
                    LlmProfileState::Invalid,
                    "Runtime is not ready; run the connection test for details",
                )
            };
            LlmProfileSummary {
                name: name.clone(),
                protocol: profile.protocol.as_str().to_string(),
                model: profile.model.clone(),
                base_url: profile.base_url.clone(),
                is_default: name == &state.config.llm.default_profile,
                state: profile_state,
                state_detail: state_detail.to_string(),
                configured,
                runtime_ready,
            }
        })
        .collect();
    Ok(Json(LlmProfileList {
        default_profile: state.config.llm.default_profile.clone(),
        profiles,
    }))
}

pub(crate) async fn test_profile(
    State(state): State<GatewayState>,
    Path(profile_name): Path<String>,
) -> Result<Json<LlmConnectionTestResult>, ApiError> {
    let profile = state
        .config
        .llm
        .profiles
        .get(&profile_name)
        .ok_or_else(|| ApiError::not_found(format!("LLM profile {profile_name:?} was not found")))?
        .clone();

    let mut capabilities = LlmCapabilityMatrix {
        config: LlmCapabilityCheck {
            status: LlmCheckStatus::Passed,
            detail: "profile schema is valid".into(),
        },
        stream_text: skipped("waiting for config check"),
        function_tools: skipped("waiting for config check"),
    };

    if let Err(error) = profile.validate(&profile_name) {
        capabilities.config = LlmCapabilityCheck {
            status: LlmCheckStatus::Failed,
            detail: sanitize_error(&error.to_string()),
        };
        capabilities.stream_text = skipped("skipped because config check failed");
        capabilities.function_tools = skipped("skipped because config check failed");
        return Ok(Json(LlmConnectionTestResult {
            profile_name,
            protocol: profile.protocol.as_str().to_string(),
            model: profile.model,
            ok: false,
            capabilities,
        }));
    }

    let api_key = match load_profile_api_key(&profile_name, &profile.api_key).await {
        Ok(Some(api_key)) => api_key,
        Ok(None) => {
            capabilities.config = LlmCapabilityCheck {
                status: LlmCheckStatus::Failed,
                detail: "API key is not configured".into(),
            };
            capabilities.stream_text = skipped("skipped because API key is missing");
            capabilities.function_tools = skipped("skipped because API key is missing");
            return Ok(Json(LlmConnectionTestResult {
                profile_name,
                protocol: profile.protocol.as_str().to_string(),
                model: profile.model,
                ok: false,
                capabilities,
            }));
        }
        Err(error) => {
            capabilities.config = LlmCapabilityCheck {
                status: LlmCheckStatus::Failed,
                detail: sanitize_error(&error),
            };
            capabilities.stream_text = skipped("skipped because API key could not be loaded");
            capabilities.function_tools = skipped("skipped because API key could not be loaded");
            return Ok(Json(LlmConnectionTestResult {
                profile_name,
                protocol: profile.protocol.as_str().to_string(),
                model: profile.model,
                ok: false,
                capabilities,
            }));
        }
    };

    let outcome = probe_connection(ProbeTarget {
        protocol: match profile.protocol {
            LlmProtocol::Responses => ProbeProtocol::Responses,
            LlmProtocol::ChatCompletions => ProbeProtocol::ChatCompletions,
        },
        base_url: profile.base_url.clone(),
        api_key: api_key.expose().to_string(),
        model: profile.model.clone(),
        timeout: Duration::from_secs(profile.timeout_seconds.clamp(5, 60)),
    })
    .await;

    capabilities.stream_text = LlmCapabilityCheck {
        status: if outcome.stream_text.ok {
            LlmCheckStatus::Passed
        } else {
            LlmCheckStatus::Failed
        },
        detail: outcome.stream_text.detail,
    };
    capabilities.function_tools = LlmCapabilityCheck {
        status: if outcome.function_tools.ok {
            LlmCheckStatus::Passed
        } else {
            LlmCheckStatus::Failed
        },
        detail: outcome.function_tools.detail,
    };

    let ok = matches!(capabilities.config.status, LlmCheckStatus::Passed)
        && matches!(capabilities.stream_text.status, LlmCheckStatus::Passed)
        && matches!(capabilities.function_tools.status, LlmCheckStatus::Passed);

    Ok(Json(LlmConnectionTestResult {
        profile_name,
        protocol: profile.protocol.as_str().to_string(),
        model: profile.model,
        ok,
        capabilities,
    }))
}

fn skipped(detail: impl Into<String>) -> LlmCapabilityCheck {
    LlmCapabilityCheck {
        status: LlmCheckStatus::Skipped,
        detail: detail.into(),
    }
}

fn key_is_configured(_profile_name: &str, source: &LlmApiKeySource) -> bool {
    match source {
        LlmApiKeySource::Environment { variable } => std::env::var_os(variable).is_some(),
        LlmApiKeySource::Keyring { credential } => LlmCredentialStore::default()
            .load(credential)
            .ok()
            .flatten()
            .is_some(),
    }
}

async fn load_profile_api_key(
    _profile_name: &str,
    source: &LlmApiKeySource,
) -> Result<Option<RiftxApiKey>, String> {
    let api_key = match source {
        LlmApiKeySource::Keyring { credential } => {
            let credential = credential.clone();
            tokio::task::spawn_blocking(move || LlmCredentialStore::default().load(&credential))
                .await
                .map_err(|error| format!("credential store task failed: {error}"))?
                .map_err(|error| error.to_string())?
        }
        LlmApiKeySource::Environment { variable } => match std::env::var(variable) {
            Ok(value) => Some(LlmApiKey::new(value).map_err(|error| error.to_string())?),
            Err(_) => None,
        },
    };
    api_key
        .map(|api_key| RiftxApiKey::new(api_key.into_inner()).map_err(|error| error.to_string()))
        .transpose()
}
