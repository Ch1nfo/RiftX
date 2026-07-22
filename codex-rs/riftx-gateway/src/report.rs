use codex_riftx_core::Artifact;
use codex_riftx_core::Engagement;
use codex_riftx_core::Finding;
use serde::Serialize;

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EngagementReport {
    pub engagement: Engagement,
    pub findings: Vec<Finding>,
    pub artifacts: Vec<Artifact>,
}

impl EngagementReport {
    pub fn markdown(&self) -> String {
        let mut output = format!(
            "# RiftX Report: {}\n\n- Engagement: `{}`\n- Policy revision: `{}`\n- Status: `{:?}`\n\n## Findings\n\n",
            self.engagement.name,
            self.engagement.id,
            self.engagement.policy_revision,
            self.engagement.status
        );
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
        output
    }
}
