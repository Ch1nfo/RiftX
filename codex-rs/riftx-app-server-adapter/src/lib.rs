//! Restricted typed facade between RiftX and the embedded agent runtime.
//!
//! The facade deliberately exposes no raw request API. Every thread and turn is
//! bound to an explicitly selected local workspace.

mod approval_policy;
mod events;

pub use approval_policy::approval_policy_for_mode;
pub use events::*;

use codex_app_server_client::DEFAULT_IN_PROCESS_CHANNEL_CAPACITY;
use codex_app_server_client::EnvironmentManager;
use codex_app_server_client::ExecServerRuntimePaths;
use codex_app_server_client::InProcessAppServerClient;
use codex_app_server_client::InProcessAppServerRequestHandle;
use codex_app_server_client::InProcessClientStartArgs;
use codex_app_server_client::InProcessServerEvent;
use codex_app_server_client::TypedRequestError;
use codex_app_server_protocol::ClientRequest;
use codex_app_server_protocol::CommandExecutionApprovalDecision;
use codex_app_server_protocol::CommandExecutionRequestApprovalResponse;
use codex_app_server_protocol::ConfigWarningNotification;
use codex_app_server_protocol::DynamicToolCallOutputContentItem;
use codex_app_server_protocol::DynamicToolCallResponse;
use codex_app_server_protocol::DynamicToolFunctionSpec;
use codex_app_server_protocol::DynamicToolSpec;
use codex_app_server_protocol::FileChangeApprovalDecision;
use codex_app_server_protocol::FileChangeRequestApprovalResponse;
use codex_app_server_protocol::JSONRPCErrorError;
use codex_app_server_protocol::PermissionsRequestApprovalResponse;
use codex_app_server_protocol::RequestId;
use codex_app_server_protocol::SandboxMode;
use codex_app_server_protocol::ServerRequest;
use codex_app_server_protocol::SkillsExtraRootsSetParams;
use codex_app_server_protocol::SkillsExtraRootsSetResponse;
use codex_app_server_protocol::SkillsListEntry;
use codex_app_server_protocol::SkillsListParams;
use codex_app_server_protocol::SkillsListResponse;
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
use codex_riftx_domain::ExecutionMode;
use codex_riftx_guard::GuardNetworkPolicy;
use codex_riftx_guard::RIFTX_GUARD_NET_ENV;
use codex_riftx_guard::RIFTX_GUARD_WORK_ROOT_ENV;
use codex_utils_absolute_path::AbsolutePathBuf;
use std::collections::HashMap;
use std::io;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::AtomicI64;
use std::sync::atomic::Ordering;
use thiserror::Error;

const UNSUPPORTED_REQUEST_CODE: i64 = -32601;
pub const RIFTX_CREDENTIAL_TOOL_NAME: &str = "riftx_credential_tool";

/// Optional OS-isolation settings for a local agent thread.
///
/// Legacy: `riftxd` starts threads with `None`. Retained for adapter tests and
/// experimental hardened launch wiring — not a v0.8 product requirement.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HardenedThreadGuard {
    pub work_root: PathBuf,
    pub network: GuardNetworkPolicy,
}

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
    #[error("RiftX Skills Directory must be an absolute path: {0}")]
    InvalidSkillRoot(PathBuf),
    #[error("RiftX model runtime returned no skill catalog for workspace {0}")]
    MissingSkillCatalog(PathBuf),
    #[error("RiftX API key cannot be empty")]
    EmptyApiKey,
}

#[derive(Clone, PartialEq, Eq)]
pub struct RiftxApiKey(String);

impl RiftxApiKey {
    pub fn new(value: String) -> Result<Self, AdapterError> {
        if value.trim().is_empty() {
            return Err(AdapterError::EmptyApiKey);
        }
        Ok(Self(value))
    }

    pub fn expose(&self) -> &str {
        &self.0
    }

    fn into_inner(self) -> String {
        self.0
    }
}

impl std::fmt::Debug for RiftxApiKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("RiftxApiKey([REDACTED])")
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RiftxLlmRuntimeConfig {
    pub runtime_home: PathBuf,
    pub model: String,
    pub reasoning_effort: String,
    pub context_window: u32,
    pub base_url: String,
    pub excluded_api_key_envs: Vec<String>,
    pub api_key: RiftxApiKey,
    pub process_path: String,
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

struct BuiltRuntimeConfig {
    config: Arc<codex_core::config::Config>,
    cli_overrides: Vec<(String, toml::Value)>,
}

impl RiftxAppServerAdapter {
    pub async fn start_embedded(
        runtime: RiftxLlmRuntimeConfig,
        arg0_paths: Arg0DispatchPaths,
    ) -> Result<Self, AdapterError> {
        let BuiltRuntimeConfig {
            config,
            cli_overrides,
        } = build_runtime_config(&runtime).await?;
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
        let local_runtime_paths = ExecServerRuntimePaths::from_optional_paths(
            arg0_paths.codex_self_exe.clone(),
            arg0_paths.codex_linux_sandbox_exe.clone(),
        )?;
        let environment_manager = Arc::new(
            EnvironmentManager::from_codex_home(
                config.codex_home.clone(),
                Some(local_runtime_paths),
            )
            .await
            .map_err(|error| io::Error::other(error.to_string()))?,
        );
        let client = InProcessAppServerClient::start_with_static_api_key(
            InProcessClientStartArgs {
                arg0_paths,
                config,
                cli_overrides,
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
            },
            runtime.api_key.into_inner(),
        )
        .await?;
        Ok(Self::from_client(client))
    }

    pub async fn start(args: InProcessClientStartArgs) -> Result<Self, AdapterError> {
        let client = InProcessAppServerClient::start(args).await?;
        Ok(Self::from_client(client))
    }

    fn from_client(client: InProcessAppServerClient) -> Self {
        Self {
            request_handle: RiftxAppServerRequestHandle {
                client: client.request_handle(),
                next_request_id: Arc::new(AtomicI64::new(1)),
            },
            client,
        }
    }

    pub fn request_handle(&self) -> RiftxAppServerRequestHandle {
        self.request_handle.clone()
    }

    pub async fn start_local_thread(
        &self,
        cwd: &Path,
        hardened: Option<HardenedThreadGuard>,
        mode: ExecutionMode,
    ) -> Result<String, AdapterError> {
        self.request_handle
            .start_local_thread(cwd, hardened, mode)
            .await
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
) -> Result<BuiltRuntimeConfig, AdapterError> {
    tokio::fs::create_dir_all(&runtime.runtime_home).await?;
    let cli_overrides = runtime_overrides(runtime);
    let config = Arc::new(
        ConfigBuilder::default()
            .codex_home(runtime.runtime_home.clone())
            .cli_overrides(cli_overrides.clone())
            .strict_config(true)
            .build()
            .await?,
    );
    let enforced = config.model.as_deref() == Some(runtime.model.as_str())
        && config
            .model_reasoning_effort
            .as_ref()
            .is_some_and(|effort| effort.to_string() == runtime.reasoning_effort)
        && config.model_context_window == Some(i64::from(runtime.context_window))
        && config.model_provider_id == "riftx"
        && config.model_provider.name == "RiftX LLM"
        && config.model_provider.base_url.as_deref() == Some(runtime.base_url.as_str())
        && config.model_provider.env_key.is_none()
        && !config.model_provider.requires_openai_auth
        && config.forced_login_method == Some(ForcedLoginMethod::Api)
        && config.cli_auth_credentials_store_mode == AuthCredentialsStoreMode::Ephemeral
        && !config.bundled_skills_enabled();
    let path_is_enforced = config
        .permissions
        .shell_environment_policy
        .r#set
        .get("PATH")
        .is_some_and(|path| path == &runtime.process_path);
    let api_keys_are_excluded = runtime.excluded_api_key_envs.iter().all(|variable| {
        config
            .permissions
            .shell_environment_policy
            .exclude
            .iter()
            .any(|name| *name == variable.as_str())
    });
    if !enforced || !path_is_enforced || !api_keys_are_excluded {
        return Err(AdapterError::UnsafeRuntimeConfig(
            "API-key-only provider, isolated runtime home, and ephemeral auth are required"
                .to_string(),
        ));
    }
    Ok(BuiltRuntimeConfig {
        config,
        cli_overrides,
    })
}

fn runtime_overrides(runtime: &RiftxLlmRuntimeConfig) -> Vec<(String, toml::Value)> {
    let mut overrides = vec![
        (
            "model".to_string(),
            toml::Value::String(runtime.model.clone()),
        ),
        (
            "model_provider".to_string(),
            toml::Value::String("riftx".to_string()),
        ),
        (
            "model_reasoning_effort".to_string(),
            toml::Value::String(runtime.reasoning_effort.clone()),
        ),
        (
            "model_reasoning_summary".to_string(),
            toml::Value::String("none".to_string()),
        ),
        (
            "model_context_window".to_string(),
            toml::Value::Integer(i64::from(runtime.context_window)),
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
        (
            "shell_environment_policy.set.PATH".to_string(),
            toml::Value::String(runtime.process_path.clone()),
        ),
        (
            "skills.bundled.enabled".to_string(),
            toml::Value::Boolean(false),
        ),
    ];
    if !runtime.excluded_api_key_envs.is_empty() {
        overrides.push((
            "shell_environment_policy.exclude".to_string(),
            toml::Value::Array(
                runtime
                    .excluded_api_key_envs
                    .iter()
                    .cloned()
                    .map(toml::Value::String)
                    .collect(),
            ),
        ));
    }
    overrides
}

impl RiftxAppServerRequestHandle {
    pub async fn set_exclusive_skill_root(&self, root: &Path) -> Result<(), AdapterError> {
        let root = AbsolutePathBuf::from_absolute_path(root)
            .map_err(|_| AdapterError::InvalidSkillRoot(root.to_path_buf()))?;
        let _: SkillsExtraRootsSetResponse = self
            .client
            .request_typed(ClientRequest::SkillsExtraRootsSet {
                request_id: self.next_request_id(),
                params: SkillsExtraRootsSetParams {
                    extra_roots: vec![root],
                    exclusive: true,
                },
            })
            .await?;
        Ok(())
    }

    pub async fn list_skills(
        &self,
        cwd: &Path,
        force_reload: bool,
    ) -> Result<SkillsListEntry, AdapterError> {
        if !cwd.is_absolute() {
            return Err(AdapterError::InvalidWorkspace(cwd.to_path_buf()));
        }
        let response: SkillsListResponse = self
            .client
            .request_typed(ClientRequest::SkillsList {
                request_id: self.next_request_id(),
                params: SkillsListParams {
                    cwds: vec![cwd.to_path_buf()],
                    force_reload,
                },
            })
            .await?;
        response
            .data
            .into_iter()
            .next()
            .ok_or_else(|| AdapterError::MissingSkillCatalog(cwd.to_path_buf()))
    }

    pub async fn start_local_thread(
        &self,
        cwd: &Path,
        hardened: Option<HardenedThreadGuard>,
        mode: ExecutionMode,
    ) -> Result<String, AdapterError> {
        let mut config = HashMap::new();
        if let Some(guard) = hardened {
            let work_root = workspace_string(&guard.work_root)?;
            config.insert(
                format!("shell_environment_policy.set.{RIFTX_GUARD_WORK_ROOT_ENV}"),
                serde_json::Value::String(work_root),
            );
            config.insert(
                format!("shell_environment_policy.set.{RIFTX_GUARD_NET_ENV}"),
                serde_json::Value::String(guard.network.encode_env()),
            );
        }
        let response: ThreadStartResponse = self
            .client
            .request_typed(ClientRequest::ThreadStart {
                request_id: self.next_request_id(),
                params: ThreadStartParams {
                    cwd: Some(workspace_string(cwd)?),
                    approval_policy: Some(approval_policy_for_mode(mode)),
                    sandbox: Some(SandboxMode::DangerFullAccess),
                    developer_instructions: Some(MAIN_AGENT_INSTRUCTIONS.to_string()),
                    environments: None,
                    dynamic_tools: Some(riftx_dynamic_tools()),
                    config: (!config.is_empty()).then_some(config),
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
                    environments: None,
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

    pub async fn resolve_dynamic_tool_text(
        &self,
        pending: PendingDynamicToolCall,
        text: String,
        success: bool,
    ) -> Result<(), AdapterError> {
        self.client
            .resolve_server_request(
                pending.request_id,
                serde_json::to_value(DynamicToolCallResponse {
                    content_items: vec![DynamicToolCallOutputContentItem::InputText { text }],
                    success,
                })?,
            )
            .await?;
        Ok(())
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

fn riftx_dynamic_tools() -> Vec<DynamicToolSpec> {
    vec![DynamicToolSpec::Function(DynamicToolFunctionSpec {
        name: RIFTX_CREDENTIAL_TOOL_NAME.to_string(),
        description: "Run one credential-aware local tool using an existing operator grant. The tool, capability, target template, use limits, and secret injection are enforced by RiftX. Never place secrets or arbitrary argv in this call.".to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "additionalProperties": false,
            "required": ["grantId", "tool", "target"],
            "properties": {
                "grantId": {
                    "type": "string",
                    "description": "Existing CredentialGrant identifier"
                },
                "tool": {
                    "type": "string",
                    "description": "Credential-aware tool name from the RiftX inventory"
                },
                "target": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["host"],
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": ["integer", "null"], "minimum": 1, "maximum": 65535}
                    }
                }
            }
        }),
        defer_loading: false,
    })]
}

const MAIN_AGENT_INSTRUCTIONS: &str = "Act as the RiftX main security-testing agent. Work only within the operator-authorized scope and objective. Treat entry points as starting clues, build hypotheses from observations, request approval for risky actions, preserve evidence, and never claim success without validated evidence. Tools are local executables available through the RiftX process environment; do not assume any tool is installed. Secrets are never available in shell or conversation context. Use riftx_credential_tool only with an operator-created CredentialGrant and a credential-aware tool from the inventory.";

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
