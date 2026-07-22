//! Restricted typed facade between RiftX Gateway and the embedded Codex App Server.
//!
//! The facade deliberately exposes no raw request API. Every thread and turn is
//! bound to an explicitly selected remote environment, so Gateway code cannot
//! invoke host execution methods such as `thread/shellCommand` or `process/spawn`.

use codex_app_server_client::InProcessAppServerClient;
use codex_app_server_client::InProcessClientStartArgs;
use codex_app_server_client::InProcessServerEvent;
use codex_app_server_client::TypedRequestError;
use codex_app_server_protocol::ClientRequest;
use codex_app_server_protocol::CommandExecutionApprovalDecision;
use codex_app_server_protocol::CommandExecutionRequestApprovalParams;
use codex_app_server_protocol::CommandExecutionRequestApprovalResponse;
use codex_app_server_protocol::DynamicToolCallParams;
use codex_app_server_protocol::DynamicToolCallResponse;
use codex_app_server_protocol::EnvironmentAddParams;
use codex_app_server_protocol::EnvironmentAddResponse;
use codex_app_server_protocol::EnvironmentInfoParams;
use codex_app_server_protocol::EnvironmentInfoResponse;
use codex_app_server_protocol::EnvironmentStatusParams;
use codex_app_server_protocol::EnvironmentStatusResponse;
use codex_app_server_protocol::FileChangeApprovalDecision;
use codex_app_server_protocol::FileChangeRequestApprovalParams;
use codex_app_server_protocol::FileChangeRequestApprovalResponse;
use codex_app_server_protocol::JSONRPCErrorError;
use codex_app_server_protocol::PermissionsRequestApprovalParams;
use codex_app_server_protocol::PermissionsRequestApprovalResponse;
use codex_app_server_protocol::RequestId;
use codex_app_server_protocol::ServerNotification;
use codex_app_server_protocol::ServerRequest;
use codex_app_server_protocol::ThreadStartParams;
use codex_app_server_protocol::ThreadStartResponse;
use codex_app_server_protocol::TurnEnvironmentParams;
use codex_app_server_protocol::TurnInterruptParams;
use codex_app_server_protocol::TurnInterruptResponse;
use codex_app_server_protocol::TurnStartParams;
use codex_app_server_protocol::TurnStartResponse;
use std::io;
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
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnvironmentRegistration {
    pub environment_id: String,
    pub exec_server_url: String,
    pub connect_timeout_ms: Option<u64>,
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

#[derive(Debug, Clone, PartialEq)]
pub struct PendingCommandApproval {
    request_id: RequestId,
    pub params: CommandExecutionRequestApprovalParams,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PendingFileChangeApproval {
    request_id: RequestId,
    pub params: FileChangeRequestApprovalParams,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PendingPermissionsApproval {
    request_id: RequestId,
    pub params: PermissionsRequestApprovalParams,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PendingDynamicToolCall {
    request_id: RequestId,
    pub params: DynamicToolCallParams,
}

#[derive(Debug, Clone)]
pub enum RiftxAppServerEvent {
    Notification(ServerNotification),
    CommandApproval(PendingCommandApproval),
    FileChangeApproval(PendingFileChangeApproval),
    PermissionsApproval(PendingPermissionsApproval),
    DynamicToolCall(PendingDynamicToolCall),
    UnsupportedServerRequest { method: String },
    Lagged { skipped: usize },
}

/// Embedded Codex App Server surface available to RiftX Gateway.
pub struct RiftxAppServerAdapter {
    client: InProcessAppServerClient,
    next_request_id: AtomicI64,
}

impl RiftxAppServerAdapter {
    pub async fn start(args: InProcessClientStartArgs) -> Result<Self, AdapterError> {
        Ok(Self {
            client: InProcessAppServerClient::start(args).await?,
            next_request_id: AtomicI64::new(1),
        })
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

    fn next_request_id(&self) -> RequestId {
        RequestId::Integer(self.next_request_id.fetch_add(1, Ordering::Relaxed))
    }
}

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
