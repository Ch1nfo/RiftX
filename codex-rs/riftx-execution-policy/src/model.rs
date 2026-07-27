use codex_riftx_domain::ExecutionMode;
use codex_riftx_tools::ToolInventory;
use serde::Deserialize;
use serde::Serialize;
use std::path::Path;
use std::path::PathBuf;

/// Original command representation supplied by a local execution path.
#[derive(Debug, Clone, Copy)]
pub enum CommandSpec<'a> {
    CommandLine(&'a str),
    Argv(&'a [String]),
}

/// Inputs required to construct an immutable pre-spawn execution intent.
pub struct CommandIntentInput<'a> {
    pub engagement_id: &'a str,
    pub thread_id: &'a str,
    pub turn_id: &'a str,
    pub tool_call_id: &'a str,
    pub mode: ExecutionMode,
    pub command: CommandSpec<'a>,
    pub cwd: &'a Path,
    pub search_path: &'a [PathBuf],
    pub inventory: &'a ToolInventory,
    pub requested_capabilities: &'a [String],
    pub authorization_deadline: Option<i64>,
    pub policy_revision: &'a str,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ExecutionParseStatus {
    Parsed,
    Complex,
    Empty,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ExecutionRisk {
    Low,
    Medium,
    High,
    Critical,
    Unknown,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum RiskSource {
    Declared,
    MissingRisk,
    MissingMetadata,
    Unmanaged,
    Unresolved,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ExecutionExecutable {
    pub requested_name: String,
    pub display_args: Vec<String>,
    pub resolved_path: Option<PathBuf>,
    pub sha256: Option<String>,
    pub inventory_sha256: Option<String>,
    pub inventory_hash_matches: Option<bool>,
    pub risk: ExecutionRisk,
    pub risk_source: RiskSource,
    pub capabilities: Vec<String>,
    pub managed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ExecutionIntent {
    pub engagement_id: String,
    pub thread_id: String,
    pub turn_id: String,
    pub tool_call_id: String,
    pub mode: ExecutionMode,
    pub display_argv: Vec<String>,
    pub command_sha256: String,
    pub argument_sha256: String,
    pub cwd: PathBuf,
    pub executables: Vec<ExecutionExecutable>,
    pub tool_inventory_sha256: String,
    pub risk: ExecutionRisk,
    pub requested_capabilities: Vec<String>,
    pub authorization_deadline: Option<i64>,
    pub policy_revision: String,
    pub parse_status: ExecutionParseStatus,
    pub binding_sha256: String,
}

pub struct DecisionContext<'a> {
    pub now: i64,
    pub authorized_capabilities: &'a [String],
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ExecutionDisposition {
    Allow,
    RequireApproval,
    Deny,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", tag = "type")]
pub enum DecisionReason {
    AuthorizationExpired,
    CapabilityDenied { capability: String },
    ExecutableHashChanged { path: PathBuf },
    EmptyCommand,
    ModeRequiresApproval,
    ComplexCommand,
    RiskRequiresApproval { risk: ExecutionRisk },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ExecutionDecision {
    pub disposition: ExecutionDisposition,
    pub reasons: Vec<DecisionReason>,
}
