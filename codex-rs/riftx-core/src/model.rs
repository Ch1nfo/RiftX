use ipnet::IpNet;
use serde::Deserialize;
use serde::Serialize;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Scope {
    pub cidrs: Vec<IpNet>,
    #[serde(default)]
    pub domains: Vec<String>,
    #[serde(default)]
    pub ports: Vec<u16>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AssessmentObjective {
    pub summary: String,
    pub success_criteria: Vec<String>,
    pub structured_criteria: Vec<crate::StructuredSuccessCriterion>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum EngagementStatus {
    Draft,
    Active,
    Interrupted,
    Completed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Engagement {
    pub id: String,
    pub name: String,
    pub status: EngagementStatus,
    pub objective: AssessmentObjective,
    #[serde(default)]
    pub entry_points: Vec<String>,
    pub mode: crate::ExecutionMode,
    pub authorization: crate::AuthorizationScope,
    pub policy_revision: String,
    pub thread_id: Option<String>,
    pub created_at: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Asset {
    pub id: String,
    pub engagement_id: String,
    pub kind: String,
    pub value: String,
    pub discovered_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AssetRelation {
    pub id: String,
    pub engagement_id: String,
    pub source_asset_id: String,
    pub target_asset_id: String,
    pub kind: String,
    pub evidence_id: Option<String>,
    pub discovered_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Service {
    pub id: String,
    pub engagement_id: String,
    pub asset_id: String,
    pub transport: String,
    pub port: u16,
    pub name: Option<String>,
    pub version: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum FindingSeverity {
    Info,
    Low,
    Medium,
    High,
    Critical,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Finding {
    pub id: String,
    pub engagement_id: String,
    pub asset_id: Option<String>,
    pub evidence_ids: Vec<String>,
    pub title: String,
    pub severity: FindingSeverity,
    pub description: String,
    pub remediation: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Evidence {
    pub id: String,
    pub engagement_id: String,
    pub finding_id: Option<String>,
    pub execution_id: Option<String>,
    pub artifact_id: Option<String>,
    pub summary: String,
    pub captured_at: i64,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum TaskStatus {
    Pending,
    Running,
    Completed,
    Failed,
    Interrupted,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Task {
    pub id: String,
    pub engagement_id: String,
    pub kind: String,
    pub status: TaskStatus,
    pub turn_id: Option<String>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Artifact {
    pub id: String,
    pub engagement_id: String,
    pub execution_id: Option<String>,
    pub path: String,
    pub media_type: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub created_at: i64,
}
