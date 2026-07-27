use codex_riftx_ipc::DiscoveredSkill;
use codex_riftx_ipc::DiscoveredTool;
use codex_riftx_ipc::ExtensionDiagnostic;
use codex_riftx_ipc::ExtensionDiagnosticLevel;
use codex_riftx_ipc::SkillCatalog;
use codex_riftx_ipc::SkillSource;
use codex_riftx_ipc::ToolCredentialInjection;
use codex_riftx_ipc::ToolCredentialMetadata;
use codex_riftx_ipc::ToolInventory;
use codex_riftx_ipc::ToolMetadata;
use codex_riftx_ipc::ToolRisk;
use codex_riftx_skills::SkillCatalog as RuntimeSkillCatalog;
use codex_riftx_skills::SkillDiagnosticLevel;
use codex_riftx_skills::SkillSource as RuntimeSkillSource;
use codex_riftx_tools::DiagnosticLevel;
use codex_riftx_tools::ToolCredentialInjection as RuntimeToolCredentialInjection;
use codex_riftx_tools::ToolInventory as RuntimeToolInventory;
use codex_riftx_tools::ToolRisk as RuntimeToolRisk;

pub(crate) fn ipc_tool_inventory(inventory: &RuntimeToolInventory) -> ToolInventory {
    ToolInventory {
        roots: inventory.roots.clone(),
        path_entries: inventory.path_entries.clone(),
        tools: inventory
            .tools
            .iter()
            .map(|tool| DiscoveredTool {
                name: tool.name.clone(),
                path: tool.path.clone(),
                sha256: tool.sha256.clone(),
                metadata_path: tool.metadata_path.clone(),
                metadata_sha256: tool.metadata_sha256.clone(),
                metadata: tool.metadata.as_ref().map(|metadata| ToolMetadata {
                    schema_version: metadata.schema_version,
                    capabilities: metadata.capabilities.clone(),
                    risk: metadata.risk.map(|risk| match risk {
                        RuntimeToolRisk::Low => ToolRisk::Low,
                        RuntimeToolRisk::Medium => ToolRisk::Medium,
                        RuntimeToolRisk::High => ToolRisk::High,
                        RuntimeToolRisk::Critical => ToolRisk::Critical,
                    }),
                    help_args: metadata.help_args.clone(),
                    version_args: metadata.version_args.clone(),
                    health_check_args: metadata.health_check_args.clone(),
                    input_target_field: metadata.input_target_field.clone(),
                    output_format: metadata.output_format.clone(),
                    parser: metadata.parser.clone(),
                    credential: metadata.credential.as_ref().map(|credential| {
                        ToolCredentialMetadata {
                            capability: credential.capability.clone(),
                            injection: match credential.injection {
                                RuntimeToolCredentialInjection::Stdin => {
                                    ToolCredentialInjection::Stdin
                                }
                                RuntimeToolCredentialInjection::Environment => {
                                    ToolCredentialInjection::Environment
                                }
                                RuntimeToolCredentialInjection::FileEnvironment => {
                                    ToolCredentialInjection::FileEnvironment
                                }
                            },
                            environment_variable: credential.environment_variable.clone(),
                            arguments: credential.arguments.clone(),
                            authentication_failure_exit_codes: credential
                                .authentication_failure_exit_codes
                                .clone(),
                        }
                    }),
                }),
                shadowed_by: tool.shadowed_by.clone(),
            })
            .collect(),
        snapshot_sha256: inventory.snapshot_sha256.clone(),
        diagnostics: inventory
            .diagnostics
            .iter()
            .map(|diagnostic| ExtensionDiagnostic {
                level: match diagnostic.level {
                    DiagnosticLevel::Info => ExtensionDiagnosticLevel::Info,
                    DiagnosticLevel::Warning => ExtensionDiagnosticLevel::Warning,
                    DiagnosticLevel::Error => ExtensionDiagnosticLevel::Error,
                },
                code: diagnostic.code.clone(),
                path: diagnostic.path.clone(),
                message: diagnostic.message.clone(),
            })
            .collect(),
    }
}

pub(crate) fn ipc_skill_catalog(catalog: &RuntimeSkillCatalog) -> SkillCatalog {
    SkillCatalog {
        root: catalog.root.clone(),
        skills: catalog
            .skills
            .iter()
            .map(|skill| DiscoveredSkill {
                name: skill.name.clone(),
                description: skill.description.clone(),
                path: skill.path.clone(),
                source: match skill.source {
                    RuntimeSkillSource::BuiltIn => SkillSource::BuiltIn,
                    RuntimeSkillSource::User => SkillSource::User,
                },
                enabled: skill.enabled,
                sha256: skill.sha256.clone(),
            })
            .collect(),
        snapshot_sha256: catalog.snapshot_sha256.clone(),
        diagnostics: catalog
            .diagnostics
            .iter()
            .map(|diagnostic| ExtensionDiagnostic {
                level: match diagnostic.level {
                    SkillDiagnosticLevel::Info => ExtensionDiagnosticLevel::Info,
                    SkillDiagnosticLevel::Warning => ExtensionDiagnosticLevel::Warning,
                    SkillDiagnosticLevel::Error => ExtensionDiagnosticLevel::Error,
                },
                code: diagnostic.code.clone(),
                path: diagnostic.path.clone(),
                message: diagnostic.message.clone(),
            })
            .collect(),
    }
}

#[cfg(test)]
#[path = "inventory_tests.rs"]
mod tests;
