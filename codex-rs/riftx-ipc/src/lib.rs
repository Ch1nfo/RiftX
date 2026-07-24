//! Local-only RiftX daemon transport.

mod client;
mod endpoint;
mod listener;
mod protocol;

pub use client::LocalIpcClient;
pub use client::LocalIpcError;
pub use client::LocalIpcResponse;
pub use client::LocalSseEvent;
pub use client::LocalSseStream;
pub use endpoint::LocalIpcEndpoint;
pub use listener::LocalIpcListener;
pub use protocol::ApprovalDecision;
pub use protocol::ApprovalDecisionParams;
pub use protocol::ApprovalKind;
pub use protocol::Artifact;
pub use protocol::AssessmentObjective;
pub use protocol::AuthorizationScope;
pub use protocol::AuthorizationWindow;
pub use protocol::CaptureArtifactParams;
pub use protocol::ChangeModeParams;
pub use protocol::ConversationEntry;
pub use protocol::ConversationKind;
pub use protocol::ConversationPage;
pub use protocol::ConversationRole;
pub use protocol::CreateCredentialGrantParams;
pub use protocol::CreateCredentialReferenceParams;
pub use protocol::CreateEngagementParams;
pub use protocol::CredentialGrant;
pub use protocol::CredentialKind;
pub use protocol::CredentialReference;
pub use protocol::DaemonControlStatus;
pub use protocol::DaemonInfo;
pub use protocol::DaemonPauseReason;
pub use protocol::DaemonRunState;
pub use protocol::Engagement;
pub use protocol::EngagementEvent;
pub use protocol::EngagementReport;
pub use protocol::EngagementStatus;
pub use protocol::EnvironmentClass;
pub use protocol::ExecutionMode;
pub use protocol::IPC_PROTOCOL_VERSION;
pub use protocol::IdentitySelector;
pub use protocol::PendingApproval;
pub use protocol::ReportFormat;
pub use protocol::ReportSkill;
pub use protocol::ReportSkillSource;
pub use protocol::ReportTool;
pub use protocol::ReportToolRisk;
pub use protocol::Scope;
pub use protocol::SkillReportSnapshot;
pub use protocol::StartTurnParams;
pub use protocol::StructuredSuccessCriterion;
pub use protocol::TaskStatus;
pub use protocol::ToolReportSnapshot;
pub use protocol::TurnAccepted;

#[cfg(test)]
#[path = "ipc_tests.rs"]
mod tests;
