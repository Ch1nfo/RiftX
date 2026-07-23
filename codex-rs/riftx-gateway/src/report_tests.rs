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
use pretty_assertions::assert_eq;

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
            test_case_id: "test-1".to_string(),
            task_id: None,
            runner: "native-tool".to_string(),
            status: ExecutionStatus::Completed,
            started_at: 40,
            completed_at: Some(50),
            exit_code: Some(0),
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
        ),
        (
            Some(&serde_json::json!("potentialFinding")),
            Some(&serde_json::json!("proposed")),
            Some(&serde_json::json!("credentialValidation")),
            Some(&serde_json::json!("domainController")),
            Some(&serde_json::json!(4)),
        )
    );
}
