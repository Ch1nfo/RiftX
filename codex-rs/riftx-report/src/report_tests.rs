use super::*;
use codex_riftx_domain::ApprovalActor;
use codex_riftx_domain::ApprovalDecisionReason;
use codex_riftx_domain::ApprovalOutcome;
use codex_riftx_domain::ApprovalRecord;
use codex_riftx_domain::ApprovalRequestKind;
use codex_riftx_domain::AssessmentObjective;
use codex_riftx_domain::AttackPathHop;
use codex_riftx_domain::AuthorizationScope;
use codex_riftx_domain::AuthorizationWindow;
use codex_riftx_domain::AutoCriterionAssessment;
use codex_riftx_domain::AutoGoalAssessment;
use codex_riftx_domain::AutoLlmProfileSnapshot;
use codex_riftx_domain::AutoRunConfig;
use codex_riftx_domain::AutoRunLimits;
use codex_riftx_domain::AutoRunState;
use codex_riftx_domain::AutoStopReason;
use codex_riftx_domain::EngagementStatus;
use codex_riftx_domain::EnvironmentClass;
use codex_riftx_domain::EvidencePurpose;
use codex_riftx_domain::ExecutionMode;
use codex_riftx_domain::ExecutionStatus;
use codex_riftx_domain::FindingSeverity;
use codex_riftx_domain::HypothesisStatus;
use codex_riftx_domain::Scope;
use codex_riftx_domain::StructuredSuccessCriterion;
use codex_riftx_domain::SuccessPredicate;
use pretty_assertions::assert_eq;
use std::collections::BTreeSet;

#[test]
fn report_preserves_traceability_redaction_and_schema() {
    let secrets: serde_json::Value =
        serde_json::from_str(include_str!("../fixtures/redaction-cases.json"))
            .expect("redaction fixture");
    let api_key = secrets["apiKey"].as_str().expect("fixture API key");
    let password = secrets["password"].as_str().expect("fixture password");
    let token = secrets["token"].as_str().expect("fixture token");
    let private_key = secrets["privateKey"].as_str().expect("fixture private key");
    let credential_url = secrets["credentialUrl"]
        .as_str()
        .expect("fixture credential URL");
    let engagement = Engagement {
        id: "eng-1".to_string(),
        name: "Authorized lab".to_string(),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Validate an authorized attack path".to_string(),
            success_criteria: vec!["Preserve reproducible evidence".to_string()],
            structured_criteria: vec![StructuredSuccessCriterion {
                id: "artifact-evidence".to_string(),
                description: "Preserve one artifact-backed evidence item".to_string(),
                predicate: SuccessPredicate::Evidence {
                    minimum_items: 1,
                    reproduction_required: true,
                },
            }],
        },
        entry_points: vec!["10.10.20.10".to_string()],
        mode: ExecutionMode::Auto,
        llm_profile: "default".to_string(),
        auto_limits: None,
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
    };
    let report = EngagementReport {
        schema: REPORT_SCHEMA_VERSION.to_string(),
        generated_at: 80,
        llm_profile: Some(ReportLlmProfile {
            name: "default".to_string(),
            protocol: Some(ReportLlmProtocol::ChatCompletions),
        }),
        auto_run: Some(ReportAutoRun::from(&AutoRun {
            engagement_id: engagement.id.clone(),
            config: AutoRunConfig {
                objective: engagement.objective.clone(),
                authorization: engagement.authorization.clone(),
                llm_profile: AutoLlmProfileSnapshot {
                    name: "default".to_string(),
                    model: "test-model".to_string(),
                    base_url: "https://llm.example.test".to_string(),
                    protocol: "chatCompletions".to_string(),
                    timeout_seconds: 30,
                    reasoning_level: "medium".to_string(),
                    context_budget: 100_000,
                    config_sha256: "profile-sha256".to_string(),
                },
                tools_snapshot_sha256: "tool-inventory-sha256".to_string(),
                policy_revision: engagement.policy_revision.clone(),
                expires_at: 2_000_000_000,
                limits: AutoRunLimits::default(),
            },
            state: AutoRunState::Succeeded,
            stop_reason: Some(AutoStopReason::SuccessCriteriaMet),
            current_subgoal: Some("Validate the attack path".to_string()),
            turns_started: 3,
            turns_completed: 3,
            tool_calls: 100,
            consecutive_failures: 0,
            no_progress_turns: 0,
            unavailable_tools: Vec::new(),
            last_goal_assessment: Some(AutoGoalAssessment {
                evaluator_version: "riftx.goal/v1".to_string(),
                evaluated_at: 70,
                succeeded: true,
                criteria: vec![AutoCriterionAssessment {
                    criterion_id: "artifact-evidence".to_string(),
                    satisfied: true,
                    evidence_ids: vec!["evidence-1".to_string()],
                }],
                evidence_ids: vec!["evidence-1".to_string()],
            }),
            progress_baseline: None,
            last_progress_assessment: None,
            started_at: Some(10),
            updated_at: 70,
        })),
        limitations: standard_report_limitations(),
        engagement,
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
            summary: format!("Potential issue requires validation token: {token}"),
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
            argv: vec![
                "nmap".to_string(),
                "--api-key".to_string(),
                api_key.to_string(),
                format!("--password={password}"),
                "--token-validation".to_string(),
                "strict".to_string(),
                credential_url.to_string(),
            ],
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
        findings: vec![Finding {
            id: "finding-1".to_string(),
            engagement_id: "eng-1".to_string(),
            asset_id: None,
            evidence_ids: vec!["evidence-1".to_string()],
            title: "Validated credential reuse".to_string(),
            severity: FindingSeverity::High,
            confidence_basis_points: 9_000,
            description: "The authorized test reproduced credential reuse".to_string(),
            remediation: Some("Rotate the exposed credential".to_string()),
        }],
        evidence: vec![Evidence {
            id: "evidence-1".to_string(),
            engagement_id: "eng-1".to_string(),
            finding_id: Some("finding-1".to_string()),
            execution_id: Some("execution-1".to_string()),
            artifact_id: Some("artifact-1".to_string()),
            summary: "Tool output reproduced the authorized finding".to_string(),
            purpose: EvidencePurpose::Objective,
            reproducible: true,
            captured_at: 55,
        }],
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
            evidence_ids: vec!["evidence-1".to_string()],
            measured_at: 70,
        }],
        tasks: Vec::new(),
        artifacts: vec![Artifact {
            id: "artifact-1".to_string(),
            engagement_id: "eng-1".to_string(),
            execution_id: Some("execution-1".to_string()),
            path: "artifacts/reproduction.txt".to_string(),
            media_type: "text/plain".to_string(),
            sha256: "d".repeat(64),
            size_bytes: 42,
            created_at: 55,
        }],
        approvals: vec![ApprovalRecord {
            id: "approval-1".to_string(),
            engagement_id: "eng-1".to_string(),
            kind: ApprovalRequestKind::Command,
            requested_at: 30,
            decided_at: Some(31),
            requested_decision: None,
            outcome: ApprovalOutcome::Cancelled,
            actor: Some(ApprovalActor::System),
            decision_reason: Some(ApprovalDecisionReason::EngagementStopped),
            policy_revision: "revision-1".to_string(),
            execution_binding_sha256: "binding-sha256".to_string(),
            command_sha256: "command-sha256".to_string(),
            argument_sha256: "argument-sha256".to_string(),
            display_argv: vec![
                "nmap".to_string(),
                "--private-key".to_string(),
                private_key.to_string(),
            ],
            cwd: Some("/workspace".to_string()),
            executable_names: vec!["nmap".to_string()],
        }],
        tool_snapshot: ToolReportSnapshot {
            snapshot_sha256: "tool-inventory-sha256".to_string(),
            tools: vec![ReportTool {
                name: "nmap".to_string(),
                sha256: "nmap-sha256".to_string(),
                metadata_sha256: Some("nmap-metadata-sha256".to_string()),
                metadata_schema_version: Some(1),
                capabilities: vec!["network.discovery".to_string()],
                risk: Some(ReportToolRisk::Low),
                managed: true,
                shadowed: false,
            }],
        },
        skill_snapshot: SkillReportSnapshot {
            snapshot_sha256: "skill-catalog-sha256".to_string(),
            skills: vec![ReportSkill {
                name: "authorized-recon".to_string(),
                source: ReportSkillSource::User,
                enabled: true,
                sha256: "skill-sha256".to_string(),
            }],
        },
    }
    .redacted();

    let markdown = report.markdown();
    assert_eq!(markdown, include_str!("../fixtures/traceable-report-v1.md"));
    assert!(markdown.contains("## Approvals"));
    assert!(markdown.contains("approval-1"));
    for expected in [
        "Potential issue requires validation",
        "Credential reuse may reach domain control",
        "credentialValidation",
        "native-tool",
        "Mode: `Auto`",
        "Schema: `riftx.report/v1`",
        "Generated at: `80`",
        "LLM Profile: `default`",
        "LLM Protocol: `ChatCompletions`",
        "Stop reason: `SuccessCriteriaMet`",
        "Goal assessment: succeeded=true, evaluated=70, evidence=`evidence-1`",
        "Criterion `artifact-evidence`: satisfied=true, evidence=`evidence-1`",
        "Finding ID: `finding-1`",
        "Evidence: `evidence-1`",
        "artifact=id=`artifact-1`, sha256=`dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd`, bytes=42",
        "Tool calls: 100/100",
        "Known Limitations",
        LOCAL_EXECUTION_LIMITATION,
        NON_ENFORCED_SCOPE_LIMITATION,
        "Operator-declared Authorized Scope",
        "not an OS-enforced network isolation boundary",
        "attack_path.analysis",
        "domainController",
        "authorizedAssets`: 3/4",
        "Inventory SHA-256: `tool-inventory-sha256`",
        "`nmap`: `nmap-sha256`",
        "metadataSchema=1",
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
        format!(
            "{}\n",
            serde_json::to_string_pretty(&report).expect("pretty report JSON")
        ),
        include_str!("../fixtures/traceable-report-v1.json")
    );
    let decoded = serde_json::from_value(json.clone()).expect("report should decode");
    assert_eq!(report, decoded);
    let encoded = json.to_string();
    for secret in [api_key, password, token, private_key, credential_url] {
        assert!(
            !markdown.contains(secret),
            "Markdown leaked fixture: {secret}"
        );
        assert!(!encoded.contains(secret), "JSON leaked fixture: {secret}");
    }
    assert!(markdown.contains("token: [REDACTED]"));
    assert_eq!(
        json.pointer("/executions/0/argv"),
        Some(&serde_json::json!([
            "nmap",
            "--api-key",
            "[REDACTED]",
            "--password=[REDACTED]",
            "--token-validation",
            "strict",
            "[REDACTED_URL]"
        ]))
    );
    assert_eq!(
        json.pointer("/approvals/0/displayArgv"),
        Some(&serde_json::json!(["nmap", "--private-key", "[REDACTED]"]))
    );
    assert!(!encoded.contains("llm.example.test"));
    assert!(!encoded.contains("test-model"));

    let schema: serde_json::Value =
        serde_json::from_str(include_str!("../fixtures/riftx.report-v1.schema.json"))
            .expect("report schema fixture");
    assert_eq!(
        schema.pointer("/properties/schema/const"),
        Some(&serde_json::json!(REPORT_SCHEMA_VERSION))
    );
    let report_fields = json
        .as_object()
        .expect("report object")
        .keys()
        .cloned()
        .collect::<BTreeSet<_>>();
    let schema_fields = schema["properties"]
        .as_object()
        .expect("schema properties")
        .keys()
        .cloned()
        .collect::<BTreeSet<_>>();
    let required_fields = schema["required"]
        .as_array()
        .expect("schema required fields")
        .iter()
        .map(|field| field.as_str().expect("required field").to_string())
        .collect::<BTreeSet<_>>();
    assert_eq!(schema_fields, report_fields);
    assert_eq!(required_fields, report_fields);
    assert_eq!(
        json.pointer("/findings/0/evidenceIds/0"),
        Some(&serde_json::json!("evidence-1"))
    );
    assert_eq!(
        json.pointer("/evidence/0/artifactId"),
        Some(&serde_json::json!("artifact-1"))
    );
    assert_eq!(
        json.pointer("/artifacts/0/sha256"),
        Some(&serde_json::json!("d".repeat(64)))
    );
    assert_eq!(
        json.pointer("/autoRun/lastGoalAssessment/criteria/0/evidenceIds/0"),
        Some(&serde_json::json!("evidence-1"))
    );
    assert_eq!(
        json.pointer("/approvals/0/outcome"),
        Some(&serde_json::json!("cancelled"))
    );
    assert_eq!(
        (
            json.pointer("/schema"),
            json.pointer("/generatedAt"),
            json.pointer("/llmProfile/protocol"),
            json.pointer("/autoRun/stopReason"),
            json.pointer("/observations/0/kind"),
            json.pointer("/hypotheses/0/status"),
            json.pointer("/testCases/0/capability"),
            json.pointer("/attackPaths/0/destinationRole"),
            json.pointer("/coverage/0/totalItems"),
            json.pointer("/toolSnapshot/tools/0/name"),
            json.pointer("/skillSnapshot/skills/0/name"),
        ),
        (
            Some(&serde_json::json!("riftx.report/v1")),
            Some(&serde_json::json!(80)),
            Some(&serde_json::json!("chatCompletions")),
            Some(&serde_json::json!("successCriteriaMet")),
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
