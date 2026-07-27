use super::*;
use codex_riftx_skills::DiscoveredSkill;
use codex_riftx_skills::SkillCatalog;
use codex_riftx_skills::SkillSource;
use codex_riftx_tools::DiscoveredTool;
use codex_riftx_tools::ToolInventory;
use codex_riftx_tools::ToolMetadata;
use codex_riftx_tools::ToolRisk;
use pretty_assertions::assert_eq;
use std::path::PathBuf;

#[test]
fn report_snapshots_exclude_local_extension_paths() {
    let inventory = ToolInventory {
        roots: vec![PathBuf::from("/private/operator/tools")],
        path_entries: vec![PathBuf::from("/private/operator/tools/bin")],
        tools: vec![DiscoveredTool {
            name: "scanner".to_string(),
            path: PathBuf::from("/private/operator/tools/bin/scanner"),
            sha256: "tool-sha256".to_string(),
            metadata_path: Some(PathBuf::from(
                "/private/operator/tools/bin/scanner.riftx.toml",
            )),
            metadata_sha256: Some("metadata-sha256".to_string()),
            metadata: Some(ToolMetadata {
                schema_version: 1,
                capabilities: vec!["network.discovery".to_string()],
                risk: Some(ToolRisk::Medium),
                help_args: Vec::new(),
                version_args: Vec::new(),
                health_check_args: Vec::new(),
                input_target_field: None,
                output_format: None,
                parser: None,
                credential: None,
            }),
            shadowed_by: Some(PathBuf::from("/usr/local/bin/scanner")),
        }],
        snapshot_sha256: "inventory-sha256".to_string(),
        diagnostics: Vec::new(),
    };
    let catalog = SkillCatalog {
        root: PathBuf::from("/private/operator/skills"),
        skills: vec![DiscoveredSkill {
            name: "recon".to_string(),
            description: "Authorized discovery".to_string(),
            path: PathBuf::from("/private/operator/skills/recon/SKILL.md"),
            source: SkillSource::User,
            enabled: true,
            sha256: "skill-sha256".to_string(),
        }],
        snapshot_sha256: "catalog-sha256".to_string(),
        diagnostics: Vec::new(),
    };

    let tool_snapshot = tool_report_snapshot(&inventory);
    let skill_snapshot = skill_report_snapshot(&catalog);

    assert_eq!(
        tool_snapshot,
        ToolReportSnapshot {
            snapshot_sha256: "inventory-sha256".to_string(),
            tools: vec![ReportTool {
                name: "scanner".to_string(),
                sha256: "tool-sha256".to_string(),
                metadata_sha256: Some("metadata-sha256".to_string()),
                metadata_schema_version: Some(1),
                capabilities: vec!["network.discovery".to_string()],
                risk: Some(ReportToolRisk::Medium),
                managed: true,
                shadowed: true,
            }],
        }
    );
    assert_eq!(
        skill_snapshot,
        SkillReportSnapshot {
            snapshot_sha256: "catalog-sha256".to_string(),
            skills: vec![ReportSkill {
                name: "recon".to_string(),
                source: ReportSkillSource::User,
                enabled: true,
                sha256: "skill-sha256".to_string(),
            }],
        }
    );
    let encoded = serde_json::to_string(&(tool_snapshot, skill_snapshot)).expect("snapshot JSON");
    assert!(!encoded.contains("/private/operator"));
}
