use codex_riftx_core::Artifact;
use codex_riftx_core::Asset;
use codex_riftx_core::Engagement;
use codex_riftx_core::Evidence;
use codex_riftx_core::Finding;
use codex_riftx_core::Service;
use codex_riftx_core::Task;
use serde::Serialize;

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EngagementReport {
    pub engagement: Engagement,
    pub assets: Vec<Asset>,
    pub services: Vec<Service>,
    pub findings: Vec<Finding>,
    pub evidence: Vec<Evidence>,
    pub tasks: Vec<Task>,
    pub artifacts: Vec<Artifact>,
}

impl EngagementReport {
    pub fn markdown(&self) -> String {
        let mut output = format!(
            "# RiftX Report: {}\n\n- Engagement: `{}`\n- Policy revision: `{}`\n- Status: `{:?}`\n\n## Assets and Services\n\n",
            self.engagement.name,
            self.engagement.id,
            self.engagement.policy_revision,
            self.engagement.status
        );
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
