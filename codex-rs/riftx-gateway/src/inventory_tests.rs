use super::*;
use codex_riftx_skills::DiscoveredSkill as RuntimeDiscoveredSkill;
use codex_riftx_skills::SkillDiagnostic;
use codex_riftx_tools::DiscoveredTool as RuntimeDiscoveredTool;
use codex_riftx_tools::ToolCredentialMetadata as RuntimeToolCredentialMetadata;
use codex_riftx_tools::ToolDiagnostic;
use codex_riftx_tools::ToolMetadata as RuntimeToolMetadata;
use pretty_assertions::assert_eq;
use std::path::PathBuf;

#[test]
fn runtime_extension_inventories_map_to_complete_ipc_values() {
    let tool_inventory = RuntimeToolInventory {
        roots: vec![PathBuf::from("/opt/riftx/tools")],
        path_entries: vec![PathBuf::from("/opt/riftx/tools/bin")],
        tools: vec![RuntimeDiscoveredTool {
            name: "scanner".to_string(),
            path: PathBuf::from("/opt/riftx/tools/bin/scanner"),
            sha256: "tool-sha".to_string(),
            metadata_path: Some(PathBuf::from("/opt/riftx/tools/bin/scanner.riftx.toml")),
            metadata_sha256: Some("metadata-sha".to_string()),
            metadata: Some(RuntimeToolMetadata {
                capabilities: vec![
                    "network.discovery".to_string(),
                    "credential.testing".to_string(),
                ],
                risk: Some(RuntimeToolRisk::Medium),
                help_args: vec!["--help".to_string()],
                version_args: vec!["--version".to_string()],
                health_check_args: Vec::new(),
                input_target_field: Some("target".to_string()),
                output_format: Some("json".to_string()),
                parser: Some("json".to_string()),
                credential: Some(RuntimeToolCredentialMetadata {
                    capability: "credential.testing".to_string(),
                    injection: RuntimeToolCredentialInjection::FileEnvironment,
                    environment_variable: Some("RIFTX_CREDENTIAL_FILE".to_string()),
                    arguments: vec!["--target".to_string(), "{target}".to_string()],
                    authentication_failure_exit_codes: vec![10],
                }),
            }),
            shadowed_by: None,
        }],
        snapshot_sha256: "tools-snapshot".to_string(),
        diagnostics: vec![ToolDiagnostic {
            level: DiagnosticLevel::Warning,
            code: "tool_shadowed".to_string(),
            path: Some(PathBuf::from("/opt/riftx/tools/bin/scanner")),
            message: "another tool shadows this entry".to_string(),
        }],
    };
    let skill_catalog = RuntimeSkillCatalog {
        root: PathBuf::from("/opt/riftx/skills"),
        skills: vec![RuntimeDiscoveredSkill {
            name: "web-recon".to_string(),
            description: "Enumerate an authorized web target.".to_string(),
            path: PathBuf::from("/opt/riftx/skills/web-recon/SKILL.md"),
            source: RuntimeSkillSource::User,
            enabled: true,
            sha256: "skill-sha".to_string(),
        }],
        snapshot_sha256: "skills-snapshot".to_string(),
        diagnostics: vec![SkillDiagnostic {
            level: SkillDiagnosticLevel::Info,
            code: "skill_loaded".to_string(),
            path: None,
            message: "skill loaded".to_string(),
        }],
    };

    assert_eq!(
        ipc_tool_inventory(&tool_inventory),
        ToolInventory {
            roots: vec![PathBuf::from("/opt/riftx/tools")],
            path_entries: vec![PathBuf::from("/opt/riftx/tools/bin")],
            tools: vec![DiscoveredTool {
                name: "scanner".to_string(),
                path: PathBuf::from("/opt/riftx/tools/bin/scanner"),
                sha256: "tool-sha".to_string(),
                metadata_path: Some(PathBuf::from("/opt/riftx/tools/bin/scanner.riftx.toml",)),
                metadata_sha256: Some("metadata-sha".to_string()),
                metadata: Some(ToolMetadata {
                    capabilities: vec![
                        "network.discovery".to_string(),
                        "credential.testing".to_string(),
                    ],
                    risk: Some(ToolRisk::Medium),
                    help_args: vec!["--help".to_string()],
                    version_args: vec!["--version".to_string()],
                    health_check_args: Vec::new(),
                    input_target_field: Some("target".to_string()),
                    output_format: Some("json".to_string()),
                    parser: Some("json".to_string()),
                    credential: Some(ToolCredentialMetadata {
                        capability: "credential.testing".to_string(),
                        injection: ToolCredentialInjection::FileEnvironment,
                        environment_variable: Some("RIFTX_CREDENTIAL_FILE".to_string()),
                        arguments: vec!["--target".to_string(), "{target}".to_string()],
                        authentication_failure_exit_codes: vec![10],
                    }),
                }),
                shadowed_by: None,
            }],
            snapshot_sha256: "tools-snapshot".to_string(),
            diagnostics: vec![ExtensionDiagnostic {
                level: ExtensionDiagnosticLevel::Warning,
                code: "tool_shadowed".to_string(),
                path: Some(PathBuf::from("/opt/riftx/tools/bin/scanner")),
                message: "another tool shadows this entry".to_string(),
            }],
        }
    );
    assert_eq!(
        ipc_skill_catalog(&skill_catalog),
        SkillCatalog {
            root: PathBuf::from("/opt/riftx/skills"),
            skills: vec![DiscoveredSkill {
                name: "web-recon".to_string(),
                description: "Enumerate an authorized web target.".to_string(),
                path: PathBuf::from("/opt/riftx/skills/web-recon/SKILL.md"),
                source: SkillSource::User,
                enabled: true,
                sha256: "skill-sha".to_string(),
            }],
            snapshot_sha256: "skills-snapshot".to_string(),
            diagnostics: vec![ExtensionDiagnostic {
                level: ExtensionDiagnosticLevel::Info,
                code: "skill_loaded".to_string(),
                path: None,
                message: "skill loaded".to_string(),
            }],
        }
    );
}
