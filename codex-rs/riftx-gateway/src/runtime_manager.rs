use codex_arg0::Arg0DispatchPaths;
use codex_riftx_app_server_adapter::RiftxApiKey;
use codex_riftx_app_server_adapter::RiftxAppServerAdapter;
use codex_riftx_app_server_adapter::RiftxAppServerRequestHandle;
use codex_riftx_app_server_adapter::RiftxHostedToolMode;
use codex_riftx_app_server_adapter::RiftxLlmRuntimeConfig;
use codex_riftx_core::LlmProtocol;
use codex_riftx_llm_bridge::BridgeHandle;
use codex_riftx_llm_bridge::BridgeUpstream;
use codex_riftx_llm_bridge::start_loopback_bridge;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;
use thiserror::Error;

#[derive(Clone)]
pub struct ProfileRuntimeSpec {
    pub protocol: LlmProtocol,
    pub runtime_home: PathBuf,
    pub model: String,
    pub reasoning_effort: String,
    pub context_window: u32,
    pub base_url: String,
    pub timeout: Duration,
    pub excluded_api_key_envs: Vec<String>,
    pub api_key: RiftxApiKey,
    pub process_path: String,
    pub skills_root: PathBuf,
}

pub(crate) struct StartedProfileRuntime {
    pub(crate) handle: RiftxAppServerRequestHandle,
    pub(crate) adapter: RiftxAppServerAdapter,
    bridge: Option<BridgeHandle>,
}

#[derive(Debug, Error)]
pub enum ProfileRuntimeError {
    #[error("LLM profile {0:?} has no configured API key")]
    Unconfigured(String),
    #[error("LLM profile {profile_name:?} Runtime failed: {message}")]
    Start {
        profile_name: String,
        message: String,
    },
    #[error("Runtime manager bridge state is unavailable")]
    BridgeState,
}

pub struct ProfileRuntimeManager {
    specs: BTreeMap<String, ProfileRuntimeSpec>,
    arg0_paths: Arg0DispatchPaths,
    bridges: Mutex<BTreeMap<String, BridgeHandle>>,
}

impl ProfileRuntimeManager {
    pub fn new(specs: BTreeMap<String, ProfileRuntimeSpec>, arg0_paths: Arg0DispatchPaths) -> Self {
        Self {
            specs,
            arg0_paths,
            bridges: Mutex::new(BTreeMap::new()),
        }
    }

    pub fn configured(&self, profile_name: &str) -> bool {
        self.specs.contains_key(profile_name)
    }

    pub async fn validate_profile(&self, profile_name: &str) -> Result<(), ProfileRuntimeError> {
        let runtime = self.start_profile(profile_name).await?;
        runtime
            .adapter
            .shutdown()
            .await
            .map_err(|error| ProfileRuntimeError::Start {
                profile_name: profile_name.to_string(),
                message: error.to_string(),
            })
    }

    pub(crate) async fn start_profile(
        &self,
        profile_name: &str,
    ) -> Result<StartedProfileRuntime, ProfileRuntimeError> {
        let spec = self
            .specs
            .get(profile_name)
            .cloned()
            .ok_or_else(|| ProfileRuntimeError::Unconfigured(profile_name.to_string()))?;
        let (base_url, api_key, bridge) = if spec.protocol == LlmProtocol::ChatCompletions {
            let bridge = start_loopback_bridge(BridgeUpstream {
                base_url: spec.base_url.clone(),
                api_key: spec.api_key.expose().to_string(),
                timeout: spec.timeout,
            })
            .await
            .map_err(|error| ProfileRuntimeError::Start {
                profile_name: profile_name.to_string(),
                message: error.to_string(),
            })?;
            let base_url = bridge.responses_base_url().to_string();
            let api_key = RiftxApiKey::new(bridge.bearer_token().to_string()).map_err(|error| {
                ProfileRuntimeError::Start {
                    profile_name: profile_name.to_string(),
                    message: error.to_string(),
                }
            })?;
            (base_url, api_key, Some(bridge))
        } else {
            (spec.base_url.clone(), spec.api_key.clone(), None)
        };
        let adapter = RiftxAppServerAdapter::start_embedded(
            RiftxLlmRuntimeConfig {
                runtime_home: spec.runtime_home,
                model: spec.model,
                reasoning_effort: spec.reasoning_effort,
                context_window: spec.context_window,
                base_url,
                hosted_tool_mode: match spec.protocol {
                    LlmProtocol::Responses => RiftxHostedToolMode::Responses,
                    LlmProtocol::ChatCompletions => RiftxHostedToolMode::FunctionOnly,
                },
                excluded_api_key_envs: spec.excluded_api_key_envs,
                api_key,
                process_path: spec.process_path,
            },
            self.arg0_paths.clone(),
        )
        .await
        .map_err(|error| ProfileRuntimeError::Start {
            profile_name: profile_name.to_string(),
            message: error.to_string(),
        })?;
        let handle = adapter.request_handle();
        handle
            .set_exclusive_skill_root(&spec.skills_root)
            .await
            .map_err(|error| ProfileRuntimeError::Start {
                profile_name: profile_name.to_string(),
                message: error.to_string(),
            })?;
        Ok(StartedProfileRuntime {
            handle,
            adapter,
            bridge,
        })
    }

    pub(crate) fn retain_bridge(
        &self,
        profile_name: String,
        runtime: &mut StartedProfileRuntime,
    ) -> Result<(), ProfileRuntimeError> {
        if let Some(bridge) = runtime.bridge.take() {
            self.bridges
                .lock()
                .map_err(|_| ProfileRuntimeError::BridgeState)?
                .insert(profile_name, bridge);
        }
        Ok(())
    }

    pub(crate) fn release_bridge(&self, profile_name: &str) {
        if let Ok(mut bridges) = self.bridges.lock() {
            bridges.remove(profile_name);
        }
    }
}
