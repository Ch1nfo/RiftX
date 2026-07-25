use super::tool_is_high_risk;
use codex_riftx_tools::DiscoveredTool;
use codex_riftx_tools::ToolMetadata;
use codex_riftx_tools::ToolRisk;
use pretty_assertions::assert_eq;
use std::path::PathBuf;

fn tool_with_risk(risk: Option<ToolRisk>) -> DiscoveredTool {
    DiscoveredTool {
        name: "nmap".to_string(),
        path: PathBuf::from("/tmp/nmap"),
        sha256: "abc".to_string(),
        metadata_path: None,
        metadata_sha256: None,
        metadata: Some(ToolMetadata {
            capabilities: vec!["network.discovery".to_string()],
            risk,
            help_args: Vec::new(),
            version_args: Vec::new(),
            health_check_args: Vec::new(),
            input_target_field: None,
            output_format: None,
            parser: None,
            credential: None,
        }),
        shadowed_by: None,
    }
}

#[test]
fn only_high_and_critical_tools_require_red_team_gate() {
    assert!(!tool_is_high_risk(&tool_with_risk(None)));
    assert!(!tool_is_high_risk(&tool_with_risk(Some(ToolRisk::Low))));
    assert!(!tool_is_high_risk(&tool_with_risk(Some(ToolRisk::Medium))));
    assert!(tool_is_high_risk(&tool_with_risk(Some(ToolRisk::High))));
    assert!(tool_is_high_risk(&tool_with_risk(Some(ToolRisk::Critical))));
    assert_eq!(
        tool_is_high_risk(&DiscoveredTool {
            name: "plain".to_string(),
            path: PathBuf::from("/tmp/plain"),
            sha256: "def".to_string(),
            metadata_path: None,
            metadata_sha256: None,
            metadata: None,
            shadowed_by: None,
        }),
        false
    );
}
