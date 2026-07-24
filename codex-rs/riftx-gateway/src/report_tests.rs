use super::*;
use codex_riftx_core::AssessmentObjective;
use codex_riftx_core::AttackPathHop;
use codex_riftx_core::AuthorizationScope;
use codex_riftx_core::AuthorizationWindow;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::EnvironmentClass;
use codex_riftx_core::ExecutionMode;
use codex_riftx_core::ExecutionStatus;
use codex_riftx_core::HypothesisStatus;
use codex_riftx_core::Scope;
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
fn report_contains_unverified_state_attack_paths_and_coverage() {
    let report = EngagementReport {
        engagement: Engagement {
            id: "eng-1".to_string(),
            name: "Authorized lab".to_string(),
            status: EngagementStatus::Active,
            objective: AssessmentObjective {
                summary: "Validate an authorized attack path".to_string(),
                success_criteria: vec!["Preserve reproducible evidence".to_string()],
                structured_criteria: Vec::new(),
            },
            entry_points: vec!["10.10.20.10".to_string()],
            mode: ExecutionMode::Native,
            llm_profile: "default".to_string(),
            authorization: AuthorizationScope {
                network: Scope {
                    cidrs: vec!["10.10.20.0/24".parse().expect("CIDR")],
                    domains: Vec::new(),
                    ports: vec![445],
                },
                identities: Vec::new(),
                capabilities: vec!["attack_path.analysis".to_string()],
                environment: EnvironmentClass::Lab,
                window: AuthorizationWindow {
                    starts_at: None,
                    expires_at: Some(2_000_000_000),
                },
            },
            policy_revision: "revision-1".to_string(),
            thread_id: None,
            created_at: 1,
            updated_at: 1,
        },
        assets: Vec::new(),
        asset_relations: Vec::new(),
        services: Vec::new(),
        identities: Vec::new(),
        observations: vec![Observation {
            id: "observation-1".to_string(),
            engagement_id: "eng-1".to_string(),
            subject: StateSubject::Asset {
                asset_id: "dc-1".to_string(),
            },
            execution_id: None,
            source: "local:nuclei".to_string(),
            kind: "potentialFinding".to_string(),
            summary: "Potential issue requires validation".to_string(),
            confidence_basis_points: 7_000,
            observed_at: 10,
        }],
        hypotheses: vec![Hypothesis {
            id: "hypothesis-1".to_string(),
            engagement_id: "eng-1".to_string(),
            observation_ids: vec!["observation-1".to_string()],
            statement: "Credential reuse may reach domain control".to_string(),
            status: HypothesisStatus::Proposed,
            confidence_basis_points: 6_000,
            created_at: 20,
        }],
        test_cases: vec![TestCase {
            id: "test-1".to_string(),
            engagement_id: "eng-1".to_string(),
            hypothesis_id: "hypothesis-1".to_string(),
            target: StateSubject::Asset {
                asset_id: "dc-1".to_string(),
            },
            capability: "credentialValidation".to_string(),
            expected_evidence: "Authenticated response".to_string(),
            created_at: 30,
        }],
        executions: vec![Execution {
            id: "execution-1".to_string(),
            engagement_id: "eng-1".to_string(),
            test_case_id: Some("test-1".to_string()),
            task_id: None,
            turn_id: "turn-1".to_string(),
            runner: "native-tool".to_string(),
            status: ExecutionStatus::Completed,
            started_at: 40,
            completed_at: Some(50),
            exit_code: Some(0),
            duration_ms: Some(10_000),
            argv: vec!["nmap".to_string(), "10.10.20.10".to_string()],
            command_sha256: "command-sha256".to_string(),
            cwd: "/tmp".to_string(),
            process_id: None,
            tool: None,
            tool_inventory_sha256: "inventory-sha256".to_string(),
            stdout_sha256: Some("stdout-sha256".to_string()),
            stderr_sha256: Some("stderr-sha256".to_string()),
            stdin_sha256: None,
            stdout_bytes: 10,
            stderr_bytes: 5,
            stdin_bytes: 0,
        }],
        findings: Vec::new(),
        evidence: Vec::new(),
        attack_paths: vec![AttackPath {
            id: "path-1".to_string(),
            engagement_id: "eng-1".to_string(),
            hops: vec![AttackPathHop {
                source: StateSubject::Identity {
                    identity_id: "identity-1".to_string(),
                },
                destination: StateSubject::Asset {
                    asset_id: "dc-1".to_string(),
                },
                capability: "credentialReuse".to_string(),
                evidence_ids: vec!["evidence-1".to_string()],
            }],
            destination_role: "domainController".to_string(),
            access_level: "domainAdminEquivalent".to_string(),
            confidence_basis_points: 9_000,
            reproducible: true,
            validated_at: 60,
        }],
        coverage: vec![Coverage {
            id: "coverage-1".to_string(),
            engagement_id: "eng-1".to_string(),
            dimension: "authorizedAssets".to_string(),
            covered_items: 3,
            total_items: 4,
            measured_at: 70,
        }],
        tasks: Vec::new(),
        artifacts: Vec::new(),
        tool_snapshot: ToolReportSnapshot {
            snapshot_sha256: "tool-inventory-sha256".to_string(),
            tools: vec![ReportTool {
                name: "nmap".to_string(),
                sha256: "nmap-sha256".to_string(),
                metadata_sha256: Some("nmap-metadata-sha256".to_string()),
                capabilities: vec!["network.discovery".to_string()],
                risk: Some(ToolRisk::Low),
                managed: true,
                shadowed: false,
            }],
        },
        skill_snapshot: SkillReportSnapshot {
            snapshot_sha256: "skill-catalog-sha256".to_string(),
            skills: vec![ReportSkill {
                name: "authorized-recon".to_string(),
                source: SkillSource::User,
                enabled: true,
                sha256: "skill-sha256".to_string(),
            }],
        },
    };

    let markdown = report.markdown();
    for expected in [
        "Potential issue requires validation",
        "Credential reuse may reach domain control",
        "credentialValidation",
        "native-tool",
        "Mode: `Native`",
        "attack_path.analysis",
        "domainController",
        "authorizedAssets`: 3/4",
        "Inventory SHA-256: `tool-inventory-sha256`",
        "`nmap`: `nmap-sha256`",
        "Catalog SHA-256: `skill-catalog-sha256`",
        "`authorized-recon`: `skill-sha256`",
    ] {
        assert!(
            markdown.contains(expected),
            "missing report text: {expected}"
        );
    }

    let json = serde_json::to_value(&report).expect("report should encode");
    assert_eq!(
        (
            json.pointer("/observations/0/kind"),
            json.pointer("/hypotheses/0/status"),
            json.pointer("/testCases/0/capability"),
            json.pointer("/attackPaths/0/destinationRole"),
            json.pointer("/coverage/0/totalItems"),
            json.pointer("/toolSnapshot/tools/0/name"),
            json.pointer("/skillSnapshot/skills/0/name"),
        ),
        (
            Some(&serde_json::json!("potentialFinding")),
            Some(&serde_json::json!("proposed")),
            Some(&serde_json::json!("credentialValidation")),
            Some(&serde_json::json!("domainController")),
            Some(&serde_json::json!(4)),
            Some(&serde_json::json!("nmap")),
            Some(&serde_json::json!("authorized-recon")),
        )
    );
}

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
                capabilities: vec!["network.discovery".to_string()],
                risk: Some(ToolRisk::Medium),
                help_args: Vec::new(),
                version_args: Vec::new(),
                health_check_args: Vec::new(),
                input_target_field: None,
                output_format: None,
                parser: None,
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

    let tool_snapshot = ToolReportSnapshot::from_inventory(&inventory);
    let skill_snapshot = SkillReportSnapshot::from_catalog(&catalog);

    assert_eq!(
        tool_snapshot,
        ToolReportSnapshot {
            snapshot_sha256: "inventory-sha256".to_string(),
            tools: vec![ReportTool {
                name: "scanner".to_string(),
                sha256: "tool-sha256".to_string(),
                metadata_sha256: Some("metadata-sha256".to_string()),
                capabilities: vec!["network.discovery".to_string()],
                risk: Some(ToolRisk::Medium),
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
                source: SkillSource::User,
                enabled: true,
                sha256: "skill-sha256".to_string(),
            }],
        }
    );
    let encoded = serde_json::to_string(&(tool_snapshot, skill_snapshot)).expect("snapshot JSON");
    assert!(!encoded.contains("/private/operator"));
}
