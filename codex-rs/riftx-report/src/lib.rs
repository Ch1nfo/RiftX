//! Typed RiftX report snapshots and deterministic Markdown rendering.

use codex_riftx_domain::Artifact;
use codex_riftx_domain::Asset;
use codex_riftx_domain::AssetRelation;
use codex_riftx_domain::AttackPath;
use codex_riftx_domain::AutoGoalAssessment;
use codex_riftx_domain::AutoProgressAssessment;
use codex_riftx_domain::AutoRun;
use codex_riftx_domain::AutoRunLimits;
use codex_riftx_domain::AutoRunState;
use codex_riftx_domain::AutoStopReason;
use codex_riftx_domain::Coverage;
use codex_riftx_domain::Engagement;
use codex_riftx_domain::Evidence;
use codex_riftx_domain::Execution;
use codex_riftx_domain::Finding;
use codex_riftx_domain::Hypothesis;
use codex_riftx_domain::Identity;
use codex_riftx_domain::Observation;
use codex_riftx_domain::Service;
use codex_riftx_domain::StateSubject;
use codex_riftx_domain::Task;
use codex_riftx_domain::TestCase;
use serde::Deserialize;
use serde::Serialize;

pub const REPORT_SCHEMA_VERSION: &str = "riftx.report/v1";
pub const LOCAL_EXECUTION_LIMITATION: &str = "RiftX executes tools on the local machine; review local tool and Artifact handling accordingly.";
pub const NON_ENFORCED_SCOPE_LIMITATION: &str = "The declared target Scope is checked by RiftX policy but is not an OS-enforced network isolation boundary.";
pub const ARTIFACT_SENSITIVITY_LIMITATION: &str = "User-provided tools may create Artifacts containing sensitive data; review them before export.";

fn default_report_schema() -> String {
    REPORT_SCHEMA_VERSION.to_string()
}

pub fn standard_report_limitations() -> Vec<String> {
    vec![
        LOCAL_EXECUTION_LIMITATION.to_string(),
        NON_ENFORCED_SCOPE_LIMITATION.to_string(),
        ARTIFACT_SENSITIVITY_LIMITATION.to_string(),
    ]
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ReportLlmProtocol {
    Responses,
    ChatCompletions,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ReportLlmProfile {
    pub name: String,
    pub protocol: Option<ReportLlmProtocol>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ReportAutoRun {
    pub state: AutoRunState,
    pub stop_reason: Option<AutoStopReason>,
    pub current_subgoal: Option<String>,
    pub turns_started: u32,
    pub turns_completed: u32,
    pub tool_calls: u32,
    pub consecutive_failures: u32,
    pub no_progress_turns: u32,
    pub unavailable_tools: Vec<String>,
    pub limits: AutoRunLimits,
    pub expires_at: i64,
    pub last_goal_assessment: Option<AutoGoalAssessment>,
    pub last_progress_assessment: Option<AutoProgressAssessment>,
    pub started_at: Option<i64>,
    pub updated_at: i64,
}

impl From<&AutoRun> for ReportAutoRun {
    fn from(run: &AutoRun) -> Self {
        Self {
            state: run.state,
            stop_reason: run.stop_reason,
            current_subgoal: run.current_subgoal.clone(),
            turns_started: run.turns_started,
            turns_completed: run.turns_completed,
            tool_calls: run.tool_calls,
            consecutive_failures: run.consecutive_failures,
            no_progress_turns: run.no_progress_turns,
            unavailable_tools: run.unavailable_tools.clone(),
            limits: run.config.limits.clone(),
            expires_at: run.config.expires_at,
            last_goal_assessment: run.last_goal_assessment.clone(),
            last_progress_assessment: run.last_progress_assessment.clone(),
            started_at: run.started_at,
            updated_at: run.updated_at,
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ReportToolRisk {
    Low,
    Medium,
    High,
    Critical,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ReportSkillSource {
    BuiltIn,
    User,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ToolReportSnapshot {
    pub snapshot_sha256: String,
    pub tools: Vec<ReportTool>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ReportTool {
    pub name: String,
    pub sha256: String,
    pub metadata_sha256: Option<String>,
    #[serde(default)]
    pub metadata_schema_version: Option<u32>,
    pub capabilities: Vec<String>,
    pub risk: Option<ReportToolRisk>,
    pub managed: bool,
    pub shadowed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SkillReportSnapshot {
    pub snapshot_sha256: String,
    pub skills: Vec<ReportSkill>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ReportSkill {
    pub name: String,
    pub source: ReportSkillSource,
    pub enabled: bool,
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EngagementReport {
    #[serde(default = "default_report_schema")]
    pub schema: String,
    #[serde(default)]
    pub generated_at: i64,
    #[serde(default)]
    pub llm_profile: Option<ReportLlmProfile>,
    #[serde(default)]
    pub auto_run: Option<ReportAutoRun>,
    #[serde(default)]
    pub limitations: Vec<String>,
    pub engagement: Engagement,
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
    pub tool_snapshot: ToolReportSnapshot,
    pub skill_snapshot: SkillReportSnapshot,
}

impl EngagementReport {
    pub fn markdown(&self) -> String {
        let mut output = format!(
            "# RiftX Report: {}\n\n- Schema: `{}`\n- Generated at: `{}`\n- Engagement: `{}`\n- Objective: {}\n- Mode: `{:?}`\n- Environment: `{:?}`\n- Authorization expires: `{}`\n- Policy revision: `{}`\n- Status: `{:?}`\n- LLM Profile: `{}`\n- LLM Protocol: `{}`\n\n## Success Criteria\n\n",
            self.engagement.name,
            self.schema,
            self.generated_at,
            self.engagement.id,
            self.engagement.objective.summary,
            self.engagement.mode,
            self.engagement.authorization.environment,
            self.engagement
                .authorization
                .window
                .expires_at
                .map_or_else(|| "none".to_string(), |value| value.to_string()),
            self.engagement.policy_revision,
            self.engagement.status,
            self.llm_profile
                .as_ref()
                .map_or(self.engagement.llm_profile.as_str(), |profile| profile
                    .name
                    .as_str()),
            self.llm_profile
                .as_ref()
                .and_then(|profile| profile.protocol)
                .map_or_else(
                    || "unavailable".to_string(),
                    |protocol| format!("{protocol:?}")
                ),
        );
        if self.engagement.objective.success_criteria.is_empty() {
            output.push_str("No explicit success criteria recorded.\n");
        } else {
            for criterion in &self.engagement.objective.success_criteria {
                output.push_str(&format!("- {criterion}\n"));
            }
        }
        for criterion in &self.engagement.objective.structured_criteria {
            output.push_str(&format!(
                "- `{}`: {}\n",
                criterion.id, criterion.description
            ));
        }
        output.push_str("\n## Operator-declared Authorized Scope\n\n");
        output.push_str(
            "This scope is an application-level operator declaration. Local shell execution is not an OS-enforced network isolation boundary.\n\n",
        );
        output.push_str(&format!(
            "- CIDRs: {}\n- Domains: {}\n- Ports: {}\n- Capabilities: {}\n",
            joined(
                self.engagement
                    .authorization
                    .network
                    .cidrs
                    .iter()
                    .map(ToString::to_string)
            ),
            joined(
                self.engagement
                    .authorization
                    .network
                    .domains
                    .iter()
                    .cloned()
            ),
            joined(
                self.engagement
                    .authorization
                    .network
                    .ports
                    .iter()
                    .map(ToString::to_string)
            ),
            joined(self.engagement.authorization.capabilities.iter().cloned()),
        ));
        output.push_str("\n## Assets and Services\n\n");
        if self.assets.is_empty() {
            output.push_str("No assets recorded.\n");
        } else {
            for asset in &self.assets {
                output.push_str(&format!("- `{}` ({})\n", asset.value, asset.kind));
                for service in self
                    .services
                    .iter()
                    .filter(|service| service.asset_id == asset.id)
                {
                    output.push_str(&format!(
                        "  - {}/{} {}\n",
                        service.port,
                        service.transport,
                        service.name.as_deref().unwrap_or("unknown")
                    ));
                }
            }
        }
        output.push_str("\n## Asset Relationships\n\n");
        if self.asset_relations.is_empty() {
            output.push_str("No asset relationships recorded.\n");
        } else {
            for relation in &self.asset_relations {
                output.push_str(&format!(
                    "- `{}` --{}--> `{}`\n",
                    relation.source_asset_id, relation.kind, relation.target_asset_id
                ));
            }
        }
        output.push_str("\n## Identities\n\n");
        if self.identities.is_empty() {
            output.push_str("No identities recorded.\n");
        } else {
            for identity in &self.identities {
                output.push_str(&format!(
                    "- `{}` ({}) domain={} tenant={}\n",
                    identity.principal,
                    identity.kind,
                    identity.domain.as_deref().unwrap_or("none"),
                    identity.tenant.as_deref().unwrap_or("none")
                ));
            }
        }
        output.push_str("\n## Observations\n\n");
        if self.observations.is_empty() {
            output.push_str("No observations recorded.\n");
        } else {
            for observation in &self.observations {
                output.push_str(&format!(
                    "- [{} / {}] {} on `{}` (confidence {}/10000)\n",
                    observation.source,
                    observation.kind,
                    observation.summary,
                    subject_label(&observation.subject),
                    observation.confidence_basis_points
                ));
            }
        }
        output.push_str("\n## Hypotheses and Test Cases\n\n");
        if self.hypotheses.is_empty() {
            output.push_str("No hypotheses recorded.\n");
        } else {
            for hypothesis in &self.hypotheses {
                output.push_str(&format!(
                    "- `{:?}` {} (confidence {}/10000)\n",
                    hypothesis.status, hypothesis.statement, hypothesis.confidence_basis_points
                ));
                for test_case in self
                    .test_cases
                    .iter()
                    .filter(|test_case| test_case.hypothesis_id == hypothesis.id)
                {
                    output.push_str(&format!(
                        "  - Test `{}` using `{}` against `{}`\n",
                        test_case.id,
                        test_case.capability,
                        subject_label(&test_case.target)
                    ));
                }
            }
        }
        output.push_str("\n## Executions\n\n");
        if self.executions.is_empty() {
            output.push_str("No executions recorded.\n");
        } else {
            for execution in &self.executions {
                output.push_str(&format!(
                    "- `{}` via `{}`: `{:?}`\n",
                    execution.id, execution.runner, execution.status
                ));
            }
        }
        output.push_str("\n## Findings\n\n");
        if self.findings.is_empty() {
            output.push_str("No findings recorded.\n");
        } else {
            for finding in &self.findings {
                output.push_str(&format!(
                    "### {} ({:?})\n\n{}\n\n",
                    finding.title, finding.severity, finding.description
                ));
            }
        }
        output.push_str("\n## Attack Paths\n\n");
        if self.attack_paths.is_empty() {
            output.push_str("No validated attack paths recorded.\n");
        } else {
            for path in &self.attack_paths {
                output.push_str(&format!(
                    "- `{}` -> `{}` (confidence {}/10000, reproducible={})\n",
                    path.destination_role,
                    path.access_level,
                    path.confidence_basis_points,
                    path.reproducible
                ));
                for hop in &path.hops {
                    output.push_str(&format!(
                        "  - `{}` --{}--> `{}`\n",
                        subject_label(&hop.source),
                        hop.capability,
                        subject_label(&hop.destination)
                    ));
                }
            }
        }
        output.push_str("\n## Coverage\n\n");
        if self.coverage.is_empty() {
            output.push_str("No coverage measurements recorded.\n");
        } else {
            for coverage in &self.coverage {
                output.push_str(&format!(
                    "- `{}`: {}/{}\n",
                    coverage.dimension, coverage.covered_items, coverage.total_items
                ));
            }
        }
        output.push_str("\n## Evidence\n\n");
        if self.evidence.is_empty() {
            output.push_str("No evidence recorded.\n");
        } else {
            for evidence in &self.evidence {
                output.push_str(&format!("- {}\n", evidence.summary));
            }
        }
        output.push_str("\n## Auto Run\n\n");
        if let Some(run) = &self.auto_run {
            output.push_str(&format!(
                "- State: `{:?}`\n- Stop reason: `{}`\n- Turns: {}/{}\n- Tool calls: {}/{}\n- Wall-clock budget: {} seconds\n- Single-command budget: {} seconds\n",
                run.state,
                run.stop_reason
                    .map_or_else(|| "none".to_string(), |reason| format!("{reason:?}")),
                run.turns_started,
                run.limits.max_turns,
                run.tool_calls,
                run.limits.max_tool_calls,
                run.limits.max_wall_clock_seconds,
                run.limits.max_single_command_seconds,
            ));
        } else {
            output.push_str("This engagement has no Auto run.\n");
        }
        output.push_str("\n## Known Limitations\n\n");
        if self.limitations.is_empty() {
            output.push_str("No report limitations were recorded.\n");
        } else {
            for limitation in &self.limitations {
                output.push_str(&format!("- {limitation}\n"));
            }
        }
        output.push_str("\n## Tool Snapshot\n\n");
        output.push_str(&format!(
            "- Inventory SHA-256: `{}`\n",
            self.tool_snapshot.snapshot_sha256
        ));
        if self.tool_snapshot.tools.is_empty() {
            output.push_str("- No Tools Directory entries recorded.\n");
        } else {
            for tool in &self.tool_snapshot.tools {
                output.push_str(&format!(
                    "- `{}`: `{}` (metadataSchema={}, managed={}, shadowed={})\n",
                    tool.name,
                    tool.sha256,
                    tool.metadata_schema_version
                        .map_or_else(|| "none".to_string(), |version| version.to_string()),
                    tool.managed,
                    tool.shadowed
                ));
            }
        }
        output.push_str("\n## Skill Snapshot\n\n");
        output.push_str(&format!(
            "- Catalog SHA-256: `{}`\n",
            self.skill_snapshot.snapshot_sha256
        ));
        if self.skill_snapshot.skills.is_empty() {
            output.push_str("- No Skills Directory entries recorded.\n");
        } else {
            for skill in &self.skill_snapshot.skills {
                output.push_str(&format!(
                    "- `{}`: `{}` ({:?}, enabled={})\n",
                    skill.name, skill.sha256, skill.source, skill.enabled
                ));
            }
        }
        output.push_str("\n## Artifacts\n\n");
        if self.artifacts.is_empty() {
            output.push_str("No artifacts recorded.\n");
        } else {
            for artifact in &self.artifacts {
                output.push_str(&format!(
                    "- `{}`: `{}` ({} bytes)\n",
                    artifact.path, artifact.sha256, artifact.size_bytes
                ));
            }
        }
        output.push_str("\n## Artifact Hash Manifest\n\n");
        for artifact in &self.artifacts {
            output.push_str(&format!("{}  {}\n", artifact.sha256, artifact.path));
        }
        output
    }
}

fn joined(values: impl Iterator<Item = String>) -> String {
    let values = values.collect::<Vec<_>>();
    if values.is_empty() {
        "none".to_string()
    } else {
        values.join(", ")
    }
}

fn subject_label(subject: &StateSubject) -> &str {
    match subject {
        StateSubject::Engagement => "engagement",
        StateSubject::Asset { asset_id } => asset_id,
        StateSubject::Service { service_id } => service_id,
        StateSubject::Identity { identity_id } => identity_id,
    }
}

#[cfg(test)]
#[path = "report_tests.rs"]
mod tests;
