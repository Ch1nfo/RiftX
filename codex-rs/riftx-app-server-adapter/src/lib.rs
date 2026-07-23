//! Restricted typed facade between RiftX and the embedded agent runtime.
//!
//! The facade deliberately exposes no raw request API. Every thread and turn is
//! bound to an explicitly selected local workspace.

mod events;

pub use events::*;

use codex_app_server_client::DEFAULT_IN_PROCESS_CHANNEL_CAPACITY;
use codex_app_server_client::EnvironmentManager;
use codex_app_server_client::InProcessAppServerClient;
use codex_app_server_client::InProcessAppServerRequestHandle;
use codex_app_server_client::InProcessClientStartArgs;
use codex_app_server_client::InProcessServerEvent;
use codex_app_server_client::TypedRequestError;
use codex_app_server_protocol::AskForApproval;
use codex_app_server_protocol::ClientRequest;
use codex_app_server_protocol::CommandExecutionApprovalDecision;
use codex_app_server_protocol::CommandExecutionRequestApprovalResponse;
use codex_app_server_protocol::ConfigWarningNotification;
use codex_app_server_protocol::FileChangeApprovalDecision;
use codex_app_server_protocol::FileChangeRequestApprovalResponse;
use codex_app_server_protocol::JSONRPCErrorError;
use codex_app_server_protocol::PermissionsRequestApprovalResponse;
use codex_app_server_protocol::RequestId;
use codex_app_server_protocol::SandboxMode;
use codex_app_server_protocol::ServerRequest;
use codex_app_server_protocol::ThreadStartParams;
use codex_app_server_protocol::ThreadStartResponse;
use codex_app_server_protocol::TurnInterruptParams;
use codex_app_server_protocol::TurnInterruptResponse;
use codex_app_server_protocol::TurnStartParams;
use codex_app_server_protocol::TurnStartResponse;
use codex_app_server_protocol::UserInput;
use codex_arg0::Arg0DispatchPaths;
use codex_config::CloudConfigBundleLoader;
use codex_config::LoaderOverrides;
use codex_config::types::AuthCredentialsStoreMode;
use codex_core::config::ConfigBuilder;
use codex_core::init_state_db;
use codex_feedback::CodexFeedback;
use codex_protocol::config_types::ForcedLoginMethod;
use codex_protocol::protocol::SessionSource;
use std::io;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::AtomicI64;
use std::sync::atomic::Ordering;
use thiserror::Error;

const UNSUPPORTED_REQUEST_CODE: i64 = -32601;

#[derive(Debug, Error)]
pub enum AdapterError {
    #[error(transparent)]
    Request(#[from] TypedRequestError),
    #[error(transparent)]
    Serialize(#[from] serde_json::Error),
    #[error(transparent)]
    Transport(#[from] io::Error),
    #[error("RiftX model runtime configuration was not enforced: {0}")]
    UnsafeRuntimeConfig(String),
    #[error("RiftX workspace must be an absolute path: {0}")]
    InvalidWorkspace(PathBuf),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RiftxLlmRuntimeConfig {
    pub runtime_home: PathBuf,
    pub model: String,
    pub base_url: String,
    pub api_key_env: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OperatorApprovalDecision {
    Approve,
    Deny,
}

/// Cloneable, restricted command surface used by RiftX Gateway request handlers.
///
/// The handle intentionally cannot issue arbitrary App Server requests.
#[derive(Clone)]
pub struct RiftxAppServerRequestHandle {
    client: InProcessAppServerRequestHandle,
    next_request_id: Arc<AtomicI64>,
}

/// Embedded model-runtime surface available to RiftX Gateway.
pub struct RiftxAppServerAdapter {
    client: InProcessAppServerClient,
    request_handle: RiftxAppServerRequestHandle,
}

impl RiftxAppServerAdapter {
    pub async fn start_embedded(runtime: RiftxLlmRuntimeConfig) -> Result<Self, AdapterError> {
        let config = build_runtime_config(&runtime).await?;
        let config_warnings = config
            .startup_warnings
            .iter()
            .map(|warning| ConfigWarningNotification {
                summary: warning.clone(),
                details: None,
                path: None,
                range: None,
            })
            .collect();
        let state_db = init_state_db(config.as_ref()).await;
        let environment_manager = Arc::new(
            EnvironmentManager::from_codex_home(config.codex_home.clone(), None)
                .await
                .map_err(|error| io::Error::other(error.to_string()))?,
        );
        Self::start(InProcessClientStartArgs {
            arg0_paths: Arg0DispatchPaths::default(),
            config,
            cli_overrides: Vec::new(),
            loader_overrides: LoaderOverrides::default(),
            strict_config: true,
            cloud_config_bundle: CloudConfigBundleLoader::default(),
            feedback: CodexFeedback::new(),
            log_db: None,
            state_db,
            environment_manager,
            config_warnings,
            session_source: SessionSource::Custom("riftxd".to_string()),
            enable_codex_api_key_env: false,
            client_name: "riftxd".to_string(),
            client_version: env!("CARGO_PKG_VERSION").to_string(),
            experimental_api: true,
            mcp_server_openai_form_elicitation: false,
            opt_out_notification_methods: Vec::new(),
            channel_capacity: DEFAULT_IN_PROCESS_CHANNEL_CAPACITY,
        })
        .await
    }

    pub async fn start(args: InProcessClientStartArgs) -> Result<Self, AdapterError> {
        let client = InProcessAppServerClient::start(args).await?;
        Ok(Self {
            request_handle: RiftxAppServerRequestHandle {
                client: client.request_handle(),
                next_request_id: Arc::new(AtomicI64::new(1)),
            },
            client,
        })
    }

    pub fn request_handle(&self) -> RiftxAppServerRequestHandle {
        self.request_handle.clone()
    }

    pub async fn start_local_thread(&self, cwd: &Path) -> Result<String, AdapterError> {
        self.request_handle.start_local_thread(cwd).await
    }

    pub async fn start_local_turn(
        &self,
        thread_id: String,
        cwd: &Path,
        input: String,
    ) -> Result<String, AdapterError> {
        self.request_handle
            .start_local_turn(thread_id, cwd, input)
            .await
    }

    pub async fn interrupt_turn(
        &self,
        thread_id: String,
        turn_id: String,
    ) -> Result<TurnInterruptResponse, AdapterError> {
        self.request_handle.interrupt_turn(thread_id, turn_id).await
    }

    pub async fn resolve_command_approval(
        &self,
        pending: PendingCommandApproval,
        decision: CommandExecutionApprovalDecision,
    ) -> Result<(), AdapterError> {
        self.client
            .resolve_server_request(
                pending.request_id,
                serde_json::to_value(CommandExecutionRequestApprovalResponse { decision })?,
            )
            .await?;
        Ok(())
    }

    pub async fn resolve_file_change_approval(
        &self,
        pending: PendingFileChangeApproval,
        decision: FileChangeApprovalDecision,
    ) -> Result<(), AdapterError> {
        self.client
            .resolve_server_request(
                pending.request_id,
                serde_json::to_value(FileChangeRequestApprovalResponse { decision })?,
            )
            .await?;
        Ok(())
    }

    pub async fn resolve_permissions_approval(
        &self,
        pending: PendingPermissionsApproval,
        response: PermissionsRequestApprovalResponse,
    ) -> Result<(), AdapterError> {
        self.client
            .resolve_server_request(pending.request_id, serde_json::to_value(response)?)
            .await?;
        Ok(())
    }

    pub async fn next_event(&mut self) -> Result<Option<RiftxAppServerEvent>, AdapterError> {
        let Some(event) = self.client.next_event().await else {
            return Ok(None);
        };
        match event {
            InProcessServerEvent::ServerNotification(notification) => {
                Ok(Some(RiftxAppServerEvent::Notification(notification)))
            }
            InProcessServerEvent::Lagged { skipped } => {
                Ok(Some(RiftxAppServerEvent::Lagged { skipped }))
            }
            InProcessServerEvent::ServerRequest(request) => {
                let event = match request {
                    ServerRequest::CommandExecutionRequestApproval { request_id, params } => {
                        RiftxAppServerEvent::CommandApproval(PendingCommandApproval {
                            request_id,
                            params,
                        })
                    }
                    ServerRequest::FileChangeRequestApproval { request_id, params } => {
                        RiftxAppServerEvent::FileChangeApproval(PendingFileChangeApproval {
                            request_id,
                            params,
                        })
                    }
                    ServerRequest::PermissionsRequestApproval { request_id, params } => {
                        RiftxAppServerEvent::PermissionsApproval(PendingPermissionsApproval {
                            request_id,
                            params,
                        })
                    }
                    ServerRequest::DynamicToolCall { request_id, params } => {
                        RiftxAppServerEvent::DynamicToolCall(PendingDynamicToolCall {
                            request_id,
                            params,
                        })
                    }
                    unsupported => {
                        let request_id = unsupported.id().clone();
                        let method = serde_json::to_value(&unsupported)?
                            .get("method")
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or("unknown")
                            .to_string();
                        self.client
                            .reject_server_request(
                                request_id,
                                JSONRPCErrorError {
                                    code: UNSUPPORTED_REQUEST_CODE,
                                    message: format!(
                                        "RiftX Gateway does not support model-runtime request {method}"
                                    ),
                                    data: None,
                                },
                            )
                            .await?;
                        RiftxAppServerEvent::UnsupportedServerRequest { method }
                    }
                };
                Ok(Some(event))
            }
        }
    }

    pub async fn shutdown(self) -> Result<(), AdapterError> {
        self.client.shutdown().await?;
        Ok(())
    }
}

async fn build_runtime_config(
    runtime: &RiftxLlmRuntimeConfig,
) -> Result<Arc<codex_core::config::Config>, AdapterError> {
    tokio::fs::create_dir_all(&runtime.runtime_home).await?;
    let config = Arc::new(
        ConfigBuilder::default()
            .codex_home(runtime.runtime_home.clone())
            .cli_overrides(vec![
                (
                    "model".to_string(),
                    toml::Value::String(runtime.model.clone()),
                ),
                (
                    "model_provider".to_string(),
                    toml::Value::String("riftx".to_string()),
                ),
                (
                    "model_providers.riftx.name".to_string(),
                    toml::Value::String("RiftX LLM".to_string()),
                ),
                (
                    "model_providers.riftx.base_url".to_string(),
                    toml::Value::String(runtime.base_url.clone()),
                ),
                (
                    "model_providers.riftx.env_key".to_string(),
                    toml::Value::String(runtime.api_key_env.clone()),
                ),
                (
                    "model_providers.riftx.wire_api".to_string(),
                    toml::Value::String("responses".to_string()),
                ),
                (
                    "model_providers.riftx.requires_openai_auth".to_string(),
                    toml::Value::Boolean(false),
                ),
                (
                    "forced_login_method".to_string(),
                    toml::Value::String("api".to_string()),
                ),
                (
                    "cli_auth_credentials_store".to_string(),
                    toml::Value::String("ephemeral".to_string()),
                ),
            ])
            .strict_config(true)
            .build()
            .await?,
    );
    let enforced = config.model.as_deref() == Some(runtime.model.as_str())
        && config.model_provider_id == "riftx"
        && config.model_provider.name == "RiftX LLM"
        && config.model_provider.base_url.as_deref() == Some(runtime.base_url.as_str())
        && config.model_provider.env_key.as_deref() == Some(runtime.api_key_env.as_str())
        && !config.model_provider.requires_openai_auth
        && config.forced_login_method == Some(ForcedLoginMethod::Api)
        && config.cli_auth_credentials_store_mode == AuthCredentialsStoreMode::Ephemeral;
    if !enforced {
        return Err(AdapterError::UnsafeRuntimeConfig(
            "API-key-only provider, isolated runtime home, and ephemeral auth are required"
                .to_string(),
        ));
    }
    Ok(config)
}

impl RiftxAppServerRequestHandle {
    pub async fn start_local_thread(&self, cwd: &Path) -> Result<String, AdapterError> {
        let response: ThreadStartResponse = self
            .client
            .request_typed(ClientRequest::ThreadStart {
                request_id: self.next_request_id(),
                params: ThreadStartParams {
                    cwd: Some(workspace_string(cwd)?),
                    approval_policy: Some(AskForApproval::OnRequest),
                    sandbox: Some(SandboxMode::DangerFullAccess),
                    developer_instructions: Some(MAIN_AGENT_INSTRUCTIONS.to_string()),
                    environments: Some(Vec::new()),
                    dynamic_tools: Some(Vec::new()),
                    ..Default::default()
                },
            })
            .await?;
        Ok(response.thread.id)
    }

    pub async fn start_local_turn(
        &self,
        thread_id: String,
        cwd: &Path,
        input: String,
    ) -> Result<String, AdapterError> {
        let response: TurnStartResponse = self
            .client
            .request_typed(ClientRequest::TurnStart {
                request_id: self.next_request_id(),
                params: TurnStartParams {
                    thread_id,
                    input: vec![UserInput::Text {
                        text: input,
                        text_elements: Vec::new(),
                    }],
                    environments: Some(Vec::new()),
                    cwd: Some(cwd.to_path_buf()),
                    ..Default::default()
                },
            })
            .await?;
        Ok(response.turn.id)
    }

    pub async fn resolve_command_approval(
        &self,
        pending: PendingCommandApproval,
        decision: CommandExecutionApprovalDecision,
    ) -> Result<(), AdapterError> {
        self.client
            .resolve_server_request(
                pending.request_id,
                serde_json::to_value(CommandExecutionRequestApprovalResponse { decision })?,
            )
            .await?;
        Ok(())
    }

    pub async fn decide_command_approval(
        &self,
        pending: PendingCommandApproval,
        decision: OperatorApprovalDecision,
    ) -> Result<(), AdapterError> {
        self.resolve_command_approval(
            pending,
            match decision {
                OperatorApprovalDecision::Approve => CommandExecutionApprovalDecision::Accept,
                OperatorApprovalDecision::Deny => CommandExecutionApprovalDecision::Decline,
            },
        )
        .await
    }

    pub async fn resolve_file_change_approval(
        &self,
        pending: PendingFileChangeApproval,
        decision: FileChangeApprovalDecision,
    ) -> Result<(), AdapterError> {
        self.client
            .resolve_server_request(
                pending.request_id,
                serde_json::to_value(FileChangeRequestApprovalResponse { decision })?,
            )
            .await?;
        Ok(())
    }

    pub async fn resolve_permissions_approval(
        &self,
        pending: PendingPermissionsApproval,
        response: PermissionsRequestApprovalResponse,
    ) -> Result<(), AdapterError> {
        self.client
            .resolve_server_request(pending.request_id, serde_json::to_value(response)?)
            .await?;
        Ok(())
    }

    pub async fn deny_file_change(
        &self,
        pending: PendingFileChangeApproval,
    ) -> Result<(), AdapterError> {
        self.resolve_file_change_approval(pending, FileChangeApprovalDecision::Decline)
            .await
    }

    pub async fn reject_permissions(
        &self,
        pending: PendingPermissionsApproval,
        message: String,
    ) -> Result<(), AdapterError> {
        self.reject_server_request(pending.request_id, message)
            .await
    }

    pub async fn reject_dynamic_tool(
        &self,
        pending: PendingDynamicToolCall,
        message: String,
    ) -> Result<(), AdapterError> {
        self.reject_server_request(pending.request_id, message)
            .await
    }

    pub async fn reject_server_request(
        &self,
        request_id: RequestId,
        message: String,
    ) -> Result<(), AdapterError> {
        self.client
            .reject_server_request(
                request_id,
                JSONRPCErrorError {
                    code: UNSUPPORTED_REQUEST_CODE,
                    message,
                    data: None,
                },
            )
            .await?;
        Ok(())
    }

    pub async fn interrupt_turn(
        &self,
        thread_id: String,
        turn_id: String,
    ) -> Result<TurnInterruptResponse, AdapterError> {
        Ok(self
            .client
            .request_typed(ClientRequest::TurnInterrupt {
                request_id: self.next_request_id(),
                params: TurnInterruptParams { thread_id, turn_id },
            })
            .await?)
    }

    fn next_request_id(&self) -> RequestId {
        RequestId::Integer(self.next_request_id.fetch_add(1, Ordering::Relaxed))
    }
}

fn workspace_string(cwd: &Path) -> Result<String, AdapterError> {
    if !cwd.is_absolute() {
        return Err(AdapterError::InvalidWorkspace(cwd.to_path_buf()));
    }
    Ok(cwd.to_string_lossy().into_owned())
}

const MAIN_AGENT_INSTRUCTIONS: &str = "Act as the RiftX main security-testing agent. Work only within the operator-authorized scope and objective. Treat entry points as starting clues, build hypotheses from observations, request approval for risky actions, preserve evidence, and never claim success without validated evidence. Tools are local executables available through the RiftX process environment; do not assume any tool is installed.";

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
