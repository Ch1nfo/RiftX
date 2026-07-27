use serde::Deserialize;
use serde::Serialize;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ApprovalRequestKind {
    Command,
    Tool,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ApprovalActor {
    LocalOperator,
    System,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum RecordedApprovalDecision {
    Approve,
    Deny,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ApprovalOutcome {
    Pending,
    Approved,
    Denied,
    Invalidated,
    Cancelled,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ApprovalDecisionReason {
    Approved,
    OperatorDenied,
    PolicyOrBindingChanged,
    DaemonPaused,
    AuditUnavailable,
    EngagementStopped,
    TurnCompleted,
    DaemonRestart,
    RuntimeClosed,
}

/// Secret-free, engagement-owned history for one operator approval request.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ApprovalRecord {
    pub id: String,
    pub engagement_id: String,
    pub kind: ApprovalRequestKind,
    pub requested_at: i64,
    pub decided_at: Option<i64>,
    pub requested_decision: Option<RecordedApprovalDecision>,
    pub outcome: ApprovalOutcome,
    pub actor: Option<ApprovalActor>,
    pub decision_reason: Option<ApprovalDecisionReason>,
    pub policy_revision: String,
    pub execution_binding_sha256: String,
    pub command_sha256: String,
    pub argument_sha256: String,
    pub display_argv: Vec<String>,
    pub cwd: Option<String>,
    pub executable_names: Vec<String>,
}
