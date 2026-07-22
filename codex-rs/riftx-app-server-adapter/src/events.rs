use crate::AdapterError;
use codex_app_server_protocol::CommandExecutionRequestApprovalParams;
use codex_app_server_protocol::DynamicToolCallParams;
use codex_app_server_protocol::FileChangeRequestApprovalParams;
use codex_app_server_protocol::PermissionsRequestApprovalParams;
use codex_app_server_protocol::RequestId;
use codex_app_server_protocol::ServerNotification;

#[derive(Debug, Clone, PartialEq)]
pub struct PendingCommandApproval {
    pub(crate) request_id: RequestId,
    pub params: CommandExecutionRequestApprovalParams,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PendingFileChangeApproval {
    pub(crate) request_id: RequestId,
    pub params: FileChangeRequestApprovalParams,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PendingPermissionsApproval {
    pub(crate) request_id: RequestId,
    pub params: PermissionsRequestApprovalParams,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PendingDynamicToolCall {
    pub(crate) request_id: RequestId,
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

#[derive(Debug, Clone, PartialEq)]
pub struct RiftxEventEnvelope {
    pub kind: String,
    pub thread_id: Option<String>,
    pub turn_id: Option<String>,
    pub request_id: Option<String>,
    pub data: serde_json::Value,
}

impl RiftxAppServerEvent {
    pub fn envelope(&self) -> Result<RiftxEventEnvelope, AdapterError> {
        let (kind, request_id, data) = match self {
            Self::Notification(notification) => {
                let serialized = serde_json::to_value(notification)?;
                let kind = serialized
                    .get("method")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("appServer/notification")
                    .to_string();
                let data = serialized
                    .get("params")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null);
                (kind, None, data)
            }
            Self::CommandApproval(pending) => (
                "approval/command".to_string(),
                Some(pending.request_id.to_string()),
                serde_json::to_value(&pending.params)?,
            ),
            Self::FileChangeApproval(pending) => (
                "approval/fileChange".to_string(),
                Some(pending.request_id.to_string()),
                serde_json::to_value(&pending.params)?,
            ),
            Self::PermissionsApproval(pending) => (
                "approval/permissions".to_string(),
                Some(pending.request_id.to_string()),
                serde_json::to_value(&pending.params)?,
            ),
            Self::DynamicToolCall(pending) => (
                "tool/dynamic".to_string(),
                Some(pending.request_id.to_string()),
                serde_json::to_value(&pending.params)?,
            ),
            Self::UnsupportedServerRequest { method } => (
                "appServer/unsupportedRequest".to_string(),
                None,
                serde_json::json!({"method": method}),
            ),
            Self::Lagged { skipped } => (
                "appServer/lagged".to_string(),
                None,
                serde_json::json!({"skipped": skipped}),
            ),
        };
        let thread_id = string_field(&data, "threadId");
        let turn_id = string_field(&data, "turnId")
            .or_else(|| data.get("turn").and_then(|turn| string_field(turn, "id")));
        Ok(RiftxEventEnvelope {
            kind,
            thread_id,
            turn_id,
            request_id,
            data,
        })
    }
}

fn string_field(value: &serde_json::Value, field: &str) -> Option<String> {
    value
        .get(field)
        .and_then(serde_json::Value::as_str)
        .map(str::to_string)
}
