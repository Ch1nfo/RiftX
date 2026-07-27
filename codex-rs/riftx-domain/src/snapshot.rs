use crate::ApprovalRecord;
use crate::Artifact;
use crate::Asset;
use crate::AssetRelation;
use crate::AttackPath;
use crate::AutoRun;
use crate::Coverage;
use crate::Engagement;
use crate::Evidence;
use crate::Execution;
use crate::Finding;
use crate::Hypothesis;
use crate::Identity;
use crate::Observation;
use crate::Service;
use crate::Task;
use crate::TestCase;
use serde::Deserialize;
use serde::Serialize;

/// Point-in-time engagement state loaded from one storage transaction.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EngagementStateSnapshot {
    pub engagement: Engagement,
    pub auto_run: Option<AutoRun>,
    pub assets: Vec<Asset>,
    pub asset_relations: Vec<AssetRelation>,
    pub services: Vec<Service>,
    pub identities: Vec<Identity>,
    pub observations: Vec<Observation>,
    pub hypotheses: Vec<Hypothesis>,
    pub test_cases: Vec<TestCase>,
    pub executions: Vec<Execution>,
    pub findings: Vec<Finding>,
    pub evidence: Vec<Evidence>,
    pub attack_paths: Vec<AttackPath>,
    pub coverage: Vec<Coverage>,
    pub tasks: Vec<Task>,
    pub artifacts: Vec<Artifact>,
    pub approvals: Vec<ApprovalRecord>,
}
