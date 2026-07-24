use serde::Deserialize;
use serde::Serialize;
use thiserror::Error;

const MAX_BASIS_POINTS: u16 = 10_000;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "type", rename_all = "camelCase", deny_unknown_fields)]
pub enum StateSubject {
    Engagement,
    Asset { asset_id: String },
    Service { service_id: String },
    Identity { identity_id: String },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Identity {
    pub id: String,
    pub engagement_id: String,
    pub asset_id: Option<String>,
    pub kind: String,
    pub principal: String,
    pub domain: Option<String>,
    pub tenant: Option<String>,
    pub discovered_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Observation {
    pub id: String,
    pub engagement_id: String,
    pub subject: StateSubject,
    pub execution_id: Option<String>,
    pub source: String,
    pub kind: String,
    pub summary: String,
    pub confidence_basis_points: u16,
    pub observed_at: i64,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum HypothesisStatus {
    Proposed,
    Validated,
    Rejected,
    Inconclusive,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Hypothesis {
    pub id: String,
    pub engagement_id: String,
    pub observation_ids: Vec<String>,
    pub statement: String,
    pub status: HypothesisStatus,
    pub confidence_basis_points: u16,
    pub created_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TestCase {
    pub id: String,
    pub engagement_id: String,
    pub hypothesis_id: String,
    pub target: StateSubject,
    pub capability: String,
    pub expected_evidence: String,
    pub created_at: i64,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ExecutionStatus {
    Pending,
    Running,
    Completed,
    Failed,
    Interrupted,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ExecutionTool {
    pub requested_name: String,
    pub resolved_path: Option<String>,
    pub sha256: Option<String>,
    pub metadata_sha256: Option<String>,
    pub version: Option<String>,
    pub managed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Execution {
    pub id: String,
    pub engagement_id: String,
    pub test_case_id: Option<String>,
    pub task_id: Option<String>,
    pub turn_id: String,
    pub runner: String,
    pub status: ExecutionStatus,
    pub started_at: i64,
    pub completed_at: Option<i64>,
    pub exit_code: Option<i32>,
    pub duration_ms: Option<i64>,
    /// Redacted argument vector. Secret values must never be persisted here.
    pub argv: Vec<String>,
    pub command_sha256: String,
    pub cwd: String,
    pub process_id: Option<String>,
    pub tool: Option<ExecutionTool>,
    pub tool_inventory_sha256: String,
    pub stdout_sha256: Option<String>,
    pub stderr_sha256: Option<String>,
    pub stdin_sha256: Option<String>,
    pub stdout_bytes: u64,
    pub stderr_bytes: u64,
    pub stdin_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AttackPathHop {
    pub source: StateSubject,
    pub destination: StateSubject,
    pub capability: String,
    pub evidence_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AttackPath {
    pub id: String,
    pub engagement_id: String,
    pub hops: Vec<AttackPathHop>,
    pub destination_role: String,
    pub access_level: String,
    pub confidence_basis_points: u16,
    pub reproducible: bool,
    pub validated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Coverage {
    pub id: String,
    pub engagement_id: String,
    pub dimension: String,
    pub covered_items: u64,
    pub total_items: u64,
    pub measured_at: i64,
}

impl Identity {
    pub fn validate(&self) -> Result<(), TargetStateError> {
        require_fields([
            ("id", self.id.as_str()),
            ("engagementId", self.engagement_id.as_str()),
            ("kind", self.kind.as_str()),
            ("principal", self.principal.as_str()),
        ])
    }
}

impl Observation {
    pub fn validate(&self) -> Result<(), TargetStateError> {
        require_fields([
            ("id", self.id.as_str()),
            ("engagementId", self.engagement_id.as_str()),
            ("source", self.source.as_str()),
            ("kind", self.kind.as_str()),
            ("summary", self.summary.as_str()),
        ])?;
        validate_subject(&self.subject)?;
        validate_basis_points(self.confidence_basis_points)
    }
}

impl Hypothesis {
    pub fn validate(&self) -> Result<(), TargetStateError> {
        require_fields([
            ("id", self.id.as_str()),
            ("engagementId", self.engagement_id.as_str()),
            ("statement", self.statement.as_str()),
        ])?;
        require_references("observationIds", &self.observation_ids)?;
        validate_basis_points(self.confidence_basis_points)
    }
}

impl TestCase {
    pub fn validate(&self) -> Result<(), TargetStateError> {
        require_fields([
            ("id", self.id.as_str()),
            ("engagementId", self.engagement_id.as_str()),
            ("hypothesisId", self.hypothesis_id.as_str()),
            ("capability", self.capability.as_str()),
            ("expectedEvidence", self.expected_evidence.as_str()),
        ])?;
        validate_subject(&self.target)
    }
}

impl Execution {
    pub fn validate(&self) -> Result<(), TargetStateError> {
        require_fields([
            ("id", self.id.as_str()),
            ("engagementId", self.engagement_id.as_str()),
            ("turnId", self.turn_id.as_str()),
            ("runner", self.runner.as_str()),
            ("commandSha256", self.command_sha256.as_str()),
            ("toolInventorySha256", self.tool_inventory_sha256.as_str()),
        ])?;
        if self.argv.is_empty() || self.cwd.trim().is_empty() {
            return Err(TargetStateError::InvalidExecutionCommand);
        }
        let terminal = matches!(
            self.status,
            ExecutionStatus::Completed | ExecutionStatus::Failed | ExecutionStatus::Interrupted
        );
        if terminal != self.completed_at.is_some()
            || self
                .completed_at
                .is_some_and(|completed_at| completed_at < self.started_at)
        {
            return Err(TargetStateError::InvalidExecutionWindow);
        }
        Ok(())
    }
}

impl AttackPath {
    pub fn validate(&self) -> Result<(), TargetStateError> {
        require_fields([
            ("id", self.id.as_str()),
            ("engagementId", self.engagement_id.as_str()),
            ("destinationRole", self.destination_role.as_str()),
            ("accessLevel", self.access_level.as_str()),
        ])?;
        if self.hops.is_empty() {
            return Err(TargetStateError::MissingReferences("hops"));
        }
        for hop in &self.hops {
            validate_subject(&hop.source)?;
            validate_subject(&hop.destination)?;
            require_fields([("capability", hop.capability.as_str())])?;
            require_references("evidenceIds", &hop.evidence_ids)?;
        }
        validate_basis_points(self.confidence_basis_points)
    }
}

impl Coverage {
    pub fn validate(&self) -> Result<(), TargetStateError> {
        require_fields([
            ("id", self.id.as_str()),
            ("engagementId", self.engagement_id.as_str()),
            ("dimension", self.dimension.as_str()),
        ])?;
        if self.total_items == 0 || self.covered_items > self.total_items {
            return Err(TargetStateError::InvalidCoverage);
        }
        Ok(())
    }
}

fn validate_subject(subject: &StateSubject) -> Result<(), TargetStateError> {
    match subject {
        StateSubject::Engagement => Ok(()),
        StateSubject::Asset { asset_id } => require_fields([("assetId", asset_id.as_str())]),
        StateSubject::Service { service_id } => {
            require_fields([("serviceId", service_id.as_str())])
        }
        StateSubject::Identity { identity_id } => {
            require_fields([("identityId", identity_id.as_str())])
        }
    }
}

fn validate_basis_points(value: u16) -> Result<(), TargetStateError> {
    if value > MAX_BASIS_POINTS {
        return Err(TargetStateError::InvalidBasisPoints);
    }
    Ok(())
}

fn require_fields<'a>(
    fields: impl IntoIterator<Item = (&'static str, &'a str)>,
) -> Result<(), TargetStateError> {
    for (name, value) in fields {
        if value.trim().is_empty() {
            return Err(TargetStateError::MissingField(name));
        }
    }
    Ok(())
}

fn require_references(name: &'static str, references: &[String]) -> Result<(), TargetStateError> {
    if references.is_empty() || references.iter().any(|value| value.trim().is_empty()) {
        return Err(TargetStateError::MissingReferences(name));
    }
    Ok(())
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum TargetStateError {
    #[error("target state field {0} must not be empty")]
    MissingField(&'static str),
    #[error("target state reference list {0} must not be empty")]
    MissingReferences(&'static str),
    #[error("confidence must be between 0 and 10000 basis points")]
    InvalidBasisPoints,
    #[error("terminal executions require a valid completion time")]
    InvalidExecutionWindow,
    #[error("execution command metadata is incomplete")]
    InvalidExecutionCommand,
    #[error("coverage requires 0 < total items and covered items <= total items")]
    InvalidCoverage,
}

#[cfg(test)]
#[path = "target_state_tests.rs"]
mod tests;
