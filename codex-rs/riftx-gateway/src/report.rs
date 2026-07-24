pub(crate) use codex_riftx_report::EngagementReport;
pub(crate) use codex_riftx_report::ReportSkill;
pub(crate) use codex_riftx_report::ReportSkillSource;
pub(crate) use codex_riftx_report::ReportTool;
pub(crate) use codex_riftx_report::ReportToolRisk;
pub(crate) use codex_riftx_report::SkillReportSnapshot;
pub(crate) use codex_riftx_report::ToolReportSnapshot;
use codex_riftx_skills::SkillCatalog;
use codex_riftx_skills::SkillSource;
use codex_riftx_tools::ToolInventory;
use codex_riftx_tools::ToolRisk;

pub(crate) fn tool_report_snapshot(inventory: &ToolInventory) -> ToolReportSnapshot {
    ToolReportSnapshot {
        snapshot_sha256: inventory.snapshot_sha256.clone(),
        tools: inventory
            .tools
            .iter()
            .map(|tool| ReportTool {
                name: tool.name.clone(),
                sha256: tool.sha256.clone(),
                metadata_sha256: tool.metadata_sha256.clone(),
                capabilities: tool
                    .metadata
                    .as_ref()
                    .map(|metadata| metadata.capabilities.clone())
                    .unwrap_or_default(),
                risk: tool
                    .metadata
                    .as_ref()
                    .and_then(|metadata| metadata.risk)
                    .map(report_tool_risk),
                managed: tool.metadata.is_some(),
                shadowed: tool.shadowed_by.is_some(),
            })
            .collect(),
    }
}

pub(crate) fn skill_report_snapshot(catalog: &SkillCatalog) -> SkillReportSnapshot {
    SkillReportSnapshot {
        snapshot_sha256: catalog.snapshot_sha256.clone(),
        skills: catalog
            .skills
            .iter()
            .map(|skill| ReportSkill {
                name: skill.name.clone(),
                source: match skill.source {
                    SkillSource::BuiltIn => ReportSkillSource::BuiltIn,
                    SkillSource::User => ReportSkillSource::User,
                },
                enabled: skill.enabled,
                sha256: skill.sha256.clone(),
            })
            .collect(),
    }
}

fn report_tool_risk(risk: ToolRisk) -> ReportToolRisk {
    match risk {
        ToolRisk::Low => ReportToolRisk::Low,
        ToolRisk::Medium => ReportToolRisk::Medium,
        ToolRisk::High => ReportToolRisk::High,
        ToolRisk::Critical => ReportToolRisk::Critical,
    }
}

#[cfg(test)]
#[path = "report_tests.rs"]
mod tests;
