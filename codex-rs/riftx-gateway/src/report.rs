use codex_riftx_core::Artifact;
use codex_riftx_core::Asset;
use codex_riftx_core::AssetRelation;
use codex_riftx_core::AttackPath;
use codex_riftx_core::Coverage;
use codex_riftx_core::Engagement;
use codex_riftx_core::Evidence;
use codex_riftx_core::Execution;
use codex_riftx_core::Finding;
use codex_riftx_core::Hypothesis;
use codex_riftx_core::Identity;
use codex_riftx_core::Observation;
use codex_riftx_core::Service;
use codex_riftx_core::StateSubject;
use codex_riftx_core::Task;
use codex_riftx_core::TestCase;
use serde::Serialize;

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EngagementReport {
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
}

impl EngagementReport {
    pub fn markdown(&self) -> String {
        let mut output = format!(
            "# RiftX Report: {}\n\n- Engagement: `{}`\n- Objective: {}\n- Mode: `{:?}`\n- Environment: `{:?}`\n- Authorization expires: `{}`\n- Policy revision: `{}`\n- Status: `{:?}`\n\n## Success Criteria\n\n",
            self.engagement.name,
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
            self.engagement.status
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
        output.push_str("\n## Authorization\n\n");
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
