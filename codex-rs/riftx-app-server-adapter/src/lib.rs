//! Restricted typed facade between RiftX Gateway and the embedded Codex App Server.
//!
//! The facade deliberately exposes no raw request API. Every thread and turn is
//! bound to an explicitly selected remote environment, so Gateway code cannot
//! invoke host execution methods such as `thread/shellCommand` or `process/spawn`.

mod events;
mod tools;

pub use events::*;
pub use tools::*;

use codex_app_server_client::DEFAULT_IN_PROCESS_CHANNEL_CAPACITY;
use codex_app_server_client::EnvironmentManager;
use codex_app_server_client::InProcessAppServerClient;
use codex_app_server_client::InProcessAppServerRequestHandle;
use codex_app_server_client::InProcessClientStartArgs;
use codex_app_server_client::InProcessServerEvent;
use codex_app_server_client::TypedRequestError;
use codex_app_server_protocol::ClientRequest;
use codex_app_server_protocol::CommandExecutionApprovalDecision;
use codex_app_server_protocol::CommandExecutionRequestApprovalResponse;
use codex_app_server_protocol::ConfigWarningNotification;
use codex_app_server_protocol::DynamicToolCallResponse;
use codex_app_server_protocol::EnvironmentAddParams;
use codex_app_server_protocol::EnvironmentAddResponse;
use codex_app_server_protocol::EnvironmentInfoParams;
use codex_app_server_protocol::EnvironmentInfoResponse;
use codex_app_server_protocol::EnvironmentStatusParams;
use codex_app_server_protocol::EnvironmentStatusResponse;
use codex_app_server_protocol::FileChangeApprovalDecision;
use codex_app_server_protocol::FileChangeRequestApprovalResponse;
use codex_app_server_protocol::JSONRPCErrorError;
use codex_app_server_protocol::PermissionsRequestApprovalResponse;
use codex_app_server_protocol::RequestId;
use codex_app_server_protocol::SensitiveString;
use codex_app_server_protocol::ServerRequest;
use codex_app_server_protocol::ThreadStartParams;
use codex_app_server_protocol::ThreadStartResponse;
use codex_app_server_protocol::TurnEnvironmentParams;
use codex_app_server_protocol::TurnInterruptParams;
use codex_app_server_protocol::TurnInterruptResponse;
use codex_app_server_protocol::TurnStartParams;
use codex_app_server_protocol::TurnStartResponse;
use codex_app_server_protocol::UserInput;
use codex_arg0::Arg0DispatchPaths;
use codex_config::CloudConfigBundleLoader;
use codex_config::LoaderOverrides;
use codex_core::config::ConfigBuilder;
use codex_core::init_state_db;
use codex_feedback::CodexFeedback;
use codex_protocol::protocol::SessionSource;
use codex_utils_path_uri::LegacyAppPathString;
use std::io;
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
    #[error(transparent)]
    Tool(#[from] StructuredToolError),
    #[error("report agent cannot be bound to a remote execution environment")]
    ReportEnvironment,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnvironmentRegistration {
    pub environment_id: String,
    pub exec_server_url: String,
    pub connect_timeout_ms: Option<u64>,
    bootstrap_token: Option<SensitiveString>,
}

impl EnvironmentRegistration {
    pub fn new(
        environment_id: String,
        exec_server_url: String,
        connect_timeout_ms: Option<u64>,
        bootstrap_token: String,
    ) -> Self {
        Self {
            environment_id,
            exec_server_url,
            connect_timeout_ms,
            bootstrap_token: Some(SensitiveString::new(bootstrap_token)),
        }
    }

    #[cfg(test)]
    fn without_auth_for_test(
        environment_id: String,
        exec_server_url: String,
        connect_timeout_ms: Option<u64>,
    ) -> Self {
        Self {
            environment_id,
            exec_server_url,
            connect_timeout_ms,
            bootstrap_token: None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct RemoteThreadStartParams {
    pub environment: TurnEnvironmentParams,
    pub app_server: ThreadStartParams,
}

#[derive(Debug, Clone)]
pub struct RemoteTurnStartParams {
    pub environment: TurnEnvironmentParams,
    pub app_server: TurnStartParams,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RemoteEnvironment {
    pub environment_id: String,
    pub cwd: String,
}

#[derive(Debug, Clone, Copy, Hash, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AgentRole {
    Recon,
    Exploit,
    Report,
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
    environment_manager: Arc<EnvironmentManager>,
    next_request_id: Arc<AtomicI64>,
}

/// Embedded Codex App Server surface available to RiftX Gateway.
pub struct RiftxAppServerAdapter {
    client: InProcessAppServerClient,
    request_handle: RiftxAppServerRequestHandle,
}

impl RiftxAppServerAdapter {
    pub async fn start_embedded() -> Result<Self, AdapterError> {
        let config = Arc::new(ConfigBuilder::default().strict_config(true).build().await?);
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
            session_source: SessionSource::Custom("riftx-gateway".to_string()),
            enable_codex_api_key_env: true,
            client_name: "riftx-gateway".to_string(),
            client_version: env!("CARGO_PKG_VERSION").to_string(),
            experimental_api: true,
            mcp_server_openai_form_elicitation: false,
            opt_out_notification_methods: Vec::new(),
            channel_capacity: DEFAULT_IN_PROCESS_CHANNEL_CAPACITY,
        })
        .await
    }

    pub async fn start(args: InProcessClientStartArgs) -> Result<Self, AdapterError> {
        let environment_manager = Arc::clone(&args.environment_manager);
        let client = InProcessAppServerClient::start(args).await?;
        Ok(Self {
            request_handle: RiftxAppServerRequestHandle {
                client: client.request_handle(),
                environment_manager,
                next_request_id: Arc::new(AtomicI64::new(1)),
            },
            client,
        })
    }

    pub fn request_handle(&self) -> RiftxAppServerRequestHandle {
        self.request_handle.clone()
    }

    pub async fn add_environment(
        &self,
        registration: EnvironmentRegistration,
    ) -> Result<EnvironmentAddResponse, AdapterError> {
        self.request_handle.add_environment(registration).await
    }

    pub async fn environment_info(
        &self,
        environment_id: String,
    ) -> Result<EnvironmentInfoResponse, AdapterError> {
        self.request_handle.environment_info(environment_id).await
    }

    pub async fn environment_status(
        &self,
        environment_id: String,
    ) -> Result<EnvironmentStatusResponse, AdapterError> {
        self.request_handle.environment_status(environment_id).await
    }

    pub async fn start_thread(
        &self,
        params: RemoteThreadStartParams,
    ) -> Result<ThreadStartResponse, AdapterError> {
        self.request_handle.start_thread(params).await
    }

    pub async fn start_turn(
        &self,
        params: RemoteTurnStartParams,
    ) -> Result<TurnStartResponse, AdapterError> {
        self.request_handle.start_turn(params).await
    }

    pub async fn interrupt_turn(
        &self,
        thread_id: String,
        turn_id: String,
    ) -> Result<TurnInterruptResponse, AdapterError> {
        self.request_handle.interrupt_turn(thread_id, turn_id).await
    }

    pub async fn start_remote_thread(
        &self,
        environment: RemoteEnvironment,
    ) -> Result<String, AdapterError> {
        self.request_handle.start_remote_thread(environment).await
    }

    pub async fn start_remote_turn(
        &self,
        thread_id: String,
        environment: RemoteEnvironment,
        input: String,
    ) -> Result<String, AdapterError> {
        self.request_handle
            .start_remote_turn(thread_id, environment, input)
            .await
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

    pub async fn resolve_dynamic_tool_call(
        &self,
        pending: PendingDynamicToolCall,
        response: DynamicToolCallResponse,
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
                                        "RiftX Gateway does not support App Server request {method}"
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

impl RiftxAppServerRequestHandle {
    pub async fn start_agent_thread(
        &self,
        role: AgentRole,
        environment: RemoteEnvironment,
    ) -> Result<String, AdapterError> {
        if role == AgentRole::Report {
            return Err(AdapterError::ReportEnvironment);
        }
        let response = self
            .start_thread(RemoteThreadStartParams {
                environment: turn_environment(environment),
                app_server: ThreadStartParams {
                    developer_instructions: Some(agent_instructions(role).to_string()),
                    dynamic_tools: Some(structured_tool_specs_for(role)),
                    ..Default::default()
                },
            })
            .await?;
        Ok(response.thread.id)
    }

    pub async fn start_report_thread(&self) -> Result<String, AdapterError> {
        let mut params = ThreadStartParams {
            developer_instructions: Some(agent_instructions(AgentRole::Report).to_string()),
            environments: Some(Vec::new()),
            dynamic_tools: Some(Vec::new()),
            ..Default::default()
        };
        params.cwd = None;
        params.runtime_workspace_roots = None;
        let response: ThreadStartResponse = self
            .client
            .request_typed(ClientRequest::ThreadStart {
                request_id: self.next_request_id(),
                params,
            })
            .await?;
        Ok(response.thread.id)
    }

    pub async fn execute_structured_tool(
        &self,
        environment_id: &str,
        request: StructuredToolRequest,
    ) -> Result<StructuredToolOutput, AdapterError> {
        Ok(execute_structured_tool(&self.environment_manager, environment_id, request).await?)
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

    pub async fn resolve_dynamic_tool_call(
        &self,
        pending: PendingDynamicToolCall,
        response: DynamicToolCallResponse,
    ) -> Result<(), AdapterError> {
        self.client
            .resolve_server_request(pending.request_id, serde_json::to_value(response)?)
            .await?;
        Ok(())
    }

    pub async fn complete_dynamic_tool_call(
        &self,
        pending: PendingDynamicToolCall,
        result: Result<&StructuredToolOutput, &str>,
    ) -> Result<(), AdapterError> {
        let (text, success) = match result {
            Ok(output) => (serde_json::to_string(output)?, output.exit_code == 0),
            Err(message) => (message.to_string(), false),
        };
        self.resolve_dynamic_tool_call(
            pending,
            DynamicToolCallResponse {
                content_items: vec![
                    codex_app_server_protocol::DynamicToolCallOutputContentItem::InputText { text },
                ],
                success,
            },
        )
        .await
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

    pub async fn add_environment(
        &self,
        registration: EnvironmentRegistration,
    ) -> Result<EnvironmentAddResponse, AdapterError> {
        Ok(self
            .client
            .request_typed(ClientRequest::EnvironmentAdd {
                request_id: self.next_request_id(),
                params: EnvironmentAddParams {
                    environment_id: registration.environment_id,
                    exec_server_url: registration.exec_server_url,
                    connect_timeout_ms: registration.connect_timeout_ms,
                    bootstrap_token: registration.bootstrap_token,
                },
            })
            .await?)
    }

    pub async fn environment_info(
        &self,
        environment_id: String,
    ) -> Result<EnvironmentInfoResponse, AdapterError> {
        Ok(self
            .client
            .request_typed(ClientRequest::EnvironmentInfo {
                request_id: self.next_request_id(),
                params: EnvironmentInfoParams { environment_id },
            })
            .await?)
    }

    pub async fn environment_status(
        &self,
        environment_id: String,
    ) -> Result<EnvironmentStatusResponse, AdapterError> {
        Ok(self
            .client
            .request_typed(ClientRequest::EnvironmentStatus {
                request_id: self.next_request_id(),
                params: EnvironmentStatusParams { environment_id },
            })
            .await?)
    }

    pub async fn start_thread(
        &self,
        mut params: RemoteThreadStartParams,
    ) -> Result<ThreadStartResponse, AdapterError> {
        params.app_server.cwd = None;
        params.app_server.runtime_workspace_roots = None;
        params.app_server.environments = Some(vec![params.environment]);
        Ok(self
            .client
            .request_typed(ClientRequest::ThreadStart {
                request_id: self.next_request_id(),
                params: params.app_server,
            })
            .await?)
    }

    pub async fn start_turn(
        &self,
        mut params: RemoteTurnStartParams,
    ) -> Result<TurnStartResponse, AdapterError> {
        params.app_server.cwd = None;
        params.app_server.runtime_workspace_roots = None;
        params.app_server.environments = Some(vec![params.environment]);
        Ok(self
            .client
            .request_typed(ClientRequest::TurnStart {
                request_id: self.next_request_id(),
                params: params.app_server,
            })
            .await?)
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

    pub async fn start_remote_thread(
        &self,
        environment: RemoteEnvironment,
    ) -> Result<String, AdapterError> {
        let response = self
            .start_thread(RemoteThreadStartParams {
                environment: turn_environment(environment),
                app_server: ThreadStartParams {
                    dynamic_tools: Some(structured_tool_specs()),
                    ..Default::default()
                },
            })
            .await?;
        Ok(response.thread.id)
    }

    pub async fn start_remote_turn(
        &self,
        thread_id: String,
        environment: RemoteEnvironment,
        input: String,
    ) -> Result<String, AdapterError> {
        let response = self
            .start_turn(RemoteTurnStartParams {
                environment: turn_environment(environment),
                app_server: TurnStartParams {
                    thread_id,
                    input: vec![UserInput::Text {
                        text: input,
                        text_elements: Vec::new(),
                    }],
                    ..Default::default()
                },
            })
            .await?;
        Ok(response.turn.id)
    }

    pub async fn start_report_turn(
        &self,
        thread_id: String,
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
                    cwd: None,
                    runtime_workspace_roots: None,
                    ..Default::default()
                },
            })
            .await?;
        Ok(response.turn.id)
    }

    fn next_request_id(&self) -> RequestId {
        RequestId::Integer(self.next_request_id.fetch_add(1, Ordering::Relaxed))
    }
}

fn turn_environment(environment: RemoteEnvironment) -> TurnEnvironmentParams {
    TurnEnvironmentParams {
        environment_id: environment.environment_id,
        cwd: LegacyAppPathString::from_string(environment.cwd),
        runtime_workspace_roots: None,
    }
}

fn structured_tool_specs_for(role: AgentRole) -> Vec<codex_app_server_protocol::DynamicToolSpec> {
    structured_tool_specs()
        .into_iter()
        .filter(|spec| match spec {
            codex_app_server_protocol::DynamicToolSpec::Function(function) => match role {
                AgentRole::Recon => true,
                AgentRole::Exploit => matches!(function.name.as_str(), "rt_nuclei" | "rt_ffuf"),
                AgentRole::Report => false,
            },
            codex_app_server_protocol::DynamicToolSpec::Namespace(_) => false,
        })
        .collect()
}

fn agent_instructions(role: AgentRole) -> &'static str {
    match role {
        AgentRole::Recon => {
            "Act as the RiftX reconnaissance agent. Use only registered RiftX tools and remain within the authorized scope. Record assets and services before drawing conclusions."
        }
        AgentRole::Exploit => {
            "Act as the RiftX validation agent. Validate only authorized findings, request approval for risky actions, minimize impact, and preserve concise evidence."
        }
        AgentRole::Report => {
            "Act as the RiftX report agent. Use only the supplied structured state. Do not request shell, filesystem, network, or dynamic tools. Produce evidence-based findings and remediation."
        }
    }
}

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
