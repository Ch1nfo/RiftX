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
pub use codex_riftx_domain::Execution;
pub use codex_riftx_domain::ExecutionMode;
pub use codex_riftx_domain::IdentitySelector;
pub use codex_riftx_domain::Scope;
pub use codex_riftx_domain::StructuredSuccessCriterion;
pub use codex_riftx_domain::TaskStatus;
pub use codex_riftx_execution_policy::ExecutionIntent;
pub use codex_riftx_report::EngagementReport;
pub use codex_riftx_report::ReportSkill;
pub use codex_riftx_report::ReportSkillSource;
pub use codex_riftx_report::ReportTool;
pub use codex_riftx_report::ReportToolRisk;
pub use codex_riftx_report::SkillReportSnapshot;
pub use codex_riftx_report::ToolReportSnapshot;
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
    Tool,
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
    #[serde(default)]
    pub execution_intent: Option<ExecutionIntent>,
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
    #[serde(default)]
    pub confirmation: Option<String>,
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
pub struct CredentialUseTarget {
    pub host: String,
    pub port: Option<u16>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum CredentialUseStatus {
    Reserved,
    Succeeded,
    AuthenticationFailed,
    ExecutionFailed,
    Interrupted,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CredentialGrantUse {
    pub id: String,
    pub engagement_id: String,
    pub grant_id: String,
    pub credential_id: String,
    pub identity_hash: String,
    pub target: CredentialUseTarget,
    pub capability: String,
    pub policy_revision: String,
    pub status: CredentialUseStatus,
    pub started_at: i64,
    pub completed_at: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CredentialExecutionParams {
    pub grant_id: String,
    pub tool: String,
    pub target: CredentialUseTarget,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CredentialExecutionResponse {
    pub usage: CredentialGrantUse,
    pub execution: Execution,
    pub stdout: String,
    pub stderr: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CaptureArtifactParams {
    pub path: PathBuf,
    pub media_type: Option<String>,
    pub execution_id: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ExtensionDiagnosticLevel {
    Info,
    Warning,
    Error,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ExtensionDiagnostic {
    pub level: ExtensionDiagnosticLevel,
    pub code: String,
    pub path: Option<PathBuf>,
    pub message: String,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ToolRisk {
    Low,
    Medium,
    High,
    Critical,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ToolCredentialInjection {
    Stdin,
    Environment,
    FileEnvironment,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ToolCredentialMetadata {
    pub capability: String,
    pub injection: ToolCredentialInjection,
    pub environment_variable: Option<String>,
    pub arguments: Vec<String>,
    pub authentication_failure_exit_codes: Vec<i32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ToolMetadata {
    pub capabilities: Vec<String>,
    pub risk: Option<ToolRisk>,
    pub help_args: Vec<String>,
    pub version_args: Vec<String>,
    pub health_check_args: Vec<String>,
    pub input_target_field: Option<String>,
    pub output_format: Option<String>,
    pub parser: Option<String>,
    pub credential: Option<ToolCredentialMetadata>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DiscoveredTool {
    pub name: String,
    pub path: PathBuf,
    pub sha256: String,
    pub metadata_path: Option<PathBuf>,
    pub metadata_sha256: Option<String>,
    pub metadata: Option<ToolMetadata>,
    pub shadowed_by: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ToolInventory {
    pub roots: Vec<PathBuf>,
    pub path_entries: Vec<PathBuf>,
    pub tools: Vec<DiscoveredTool>,
    pub snapshot_sha256: String,
    pub diagnostics: Vec<ExtensionDiagnostic>,
}

impl ToolInventory {
    pub fn is_healthy(&self) -> bool {
        !self
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.level == ExtensionDiagnosticLevel::Error)
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum SkillSource {
    BuiltIn,
    User,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DiscoveredSkill {
    pub name: String,
    pub description: String,
    pub path: PathBuf,
    pub source: SkillSource,
    pub enabled: bool,
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SkillCatalog {
    pub root: PathBuf,
    pub skills: Vec<DiscoveredSkill>,
    pub snapshot_sha256: String,
    pub diagnostics: Vec<ExtensionDiagnostic>,
}

impl SkillCatalog {
    pub fn is_healthy(&self) -> bool {
        !self
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.level == ExtensionDiagnosticLevel::Error)
    }
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

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum LlmCheckStatus {
    Passed,
    Failed,
    Skipped,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LlmCapabilityCheck {
    pub status: LlmCheckStatus,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LlmCapabilityMatrix {
    pub config: LlmCapabilityCheck,
    pub stream_text: LlmCapabilityCheck,
    pub function_tools: LlmCapabilityCheck,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LlmProfileState {
    Unconfigured,
    Ready,
    Invalid,
    Unreachable,
    Disabled,
    InUse,
}

impl LlmProfileState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unconfigured => "unconfigured",
            Self::Ready => "ready",
            Self::Invalid => "invalid",
            Self::Unreachable => "unreachable",
            Self::Disabled => "disabled",
            Self::InUse => "in_use",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LlmProfileSummary {
    pub name: String,
    pub protocol: String,
    pub model: String,
    pub base_url: String,
    pub is_default: bool,
    pub state: LlmProfileState,
    pub state_detail: String,
    pub configured: bool,
    pub runtime_ready: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LlmProfileList {
    pub default_profile: String,
    pub profiles: Vec<LlmProfileSummary>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LlmConnectionTestResult {
    pub profile_name: String,
    pub protocol: String,
    pub model: String,
    pub ok: bool,
    pub capabilities: LlmCapabilityMatrix,
}

#[cfg(test)]
#[path = "protocol_tests.rs"]
mod tests;
