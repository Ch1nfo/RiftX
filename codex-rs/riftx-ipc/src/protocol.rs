pub use codex_riftx_domain::Artifact;
pub use codex_riftx_domain::AssessmentObjective;
pub use codex_riftx_domain::AuthorizationScope;
pub use codex_riftx_domain::AuthorizationWindow;
pub use codex_riftx_domain::ConversationEntry;
pub use codex_riftx_domain::ConversationKind;
pub use codex_riftx_domain::ConversationRole;
pub use codex_riftx_domain::CredentialGrant;
pub use codex_riftx_domain::CredentialKind;
pub use codex_riftx_domain::CredentialReference;
pub use codex_riftx_domain::Engagement;
pub use codex_riftx_domain::EngagementStatus;
pub use codex_riftx_domain::EnvironmentClass;
pub use codex_riftx_domain::ExecutionMode;
pub use codex_riftx_domain::IdentitySelector;
pub use codex_riftx_domain::Scope;
pub use codex_riftx_domain::StructuredSuccessCriterion;
pub use codex_riftx_domain::TaskStatus;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use std::path::PathBuf;

pub const IPC_PROTOCOL_VERSION: u32 = 4;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DaemonInfo {
    pub protocol_version: u32,
    pub daemon_version: String,
}

impl DaemonInfo {
    pub fn current() -> Self {
        Self {
            protocol_version: IPC_PROTOCOL_VERSION,
            daemon_version: env!("CARGO_PKG_VERSION").to_string(),
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum DaemonRunState {
    Running,
    Paused,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum DaemonPauseReason {
    OperatorPause,
    KillSwitch,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DaemonControlStatus {
    pub state: DaemonRunState,
    pub reason: Option<DaemonPauseReason>,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EngagementEvent {
    pub engagement_id: String,
    pub kind: String,
    pub timestamp: i64,
    pub data: Value,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ApprovalKind {
    Command,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct PendingApproval {
    pub id: String,
    pub engagement_id: String,
    pub policy_revision: String,
    pub kind: ApprovalKind,
    pub requested_at: i64,
    pub command: Option<String>,
    pub cwd: Option<String>,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ApprovalDecision {
    Approve,
    Deny,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CreateEngagementParams {
    pub name: String,
    pub objective: AssessmentObjective,
    #[serde(default)]
    pub entry_points: Vec<String>,
    pub mode: ExecutionMode,
    #[serde(default)]
    pub llm_profile: Option<String>,
    pub authorization: AuthorizationScope,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CreateCredentialReferenceParams {
    pub label: String,
    pub kind: CredentialKind,
    pub username: Option<String>,
    pub domain: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CreateCredentialGrantParams {
    pub credential_id: String,
    pub allowed_targets: Scope,
    pub allowed_capabilities: Vec<String>,
    pub max_uses: u32,
    pub max_failures_per_identity: u32,
    pub starts_at: Option<i64>,
    pub expires_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CaptureArtifactParams {
    pub path: PathBuf,
    pub media_type: Option<String>,
    pub execution_id: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ReportFormat {
    Markdown,
    Json,
}

impl ReportFormat {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Markdown => "markdown",
            Self::Json => "json",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct StartTurnParams {
    #[serde(default)]
    pub input: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ChangeModeParams {
    pub mode: ExecutionMode,
    pub confirmation: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ApprovalDecisionParams {
    pub decision: ApprovalDecision,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TurnAccepted {
    pub task_id: String,
    pub status: TaskStatus,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ConversationPage {
    pub data: Vec<ConversationEntry>,
    pub next_cursor: Option<String>,
}
