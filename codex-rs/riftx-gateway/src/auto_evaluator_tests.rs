use super::*;
use crate::api::tests::test_state;
use codex_riftx_core::AssessmentObjective;
use codex_riftx_core::AttackPath;
use codex_riftx_core::AttackPathHop;
use codex_riftx_core::AuthorizationScope;
use codex_riftx_core::AuthorizationWindow;
use codex_riftx_core::AutoLlmProfileSnapshot;
use codex_riftx_core::AutoRunConfig;
use codex_riftx_core::AutoRunLimits;
use codex_riftx_core::AutoRunState;
use codex_riftx_core::AutoStopReason;
use codex_riftx_core::Coverage;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::EnvironmentClass;
use codex_riftx_core::Evidence;
use codex_riftx_core::Execution;
use codex_riftx_core::ExecutionMode;
use codex_riftx_core::ExecutionStatus;
use codex_riftx_core::Finding;
use codex_riftx_core::FindingSeverity;
use codex_riftx_core::Scope;
use codex_riftx_core::StateSubject;
use codex_riftx_core::StructuredSuccessCriterion;
use pretty_assertions::assert_eq;
use serde_json::json;
use tempfile::TempDir;

#[tokio::test]
async fn evaluator_requires_valid_evidence_for_every_structured_predicate() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = engagement(structured_criteria());
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    put_valid_evidence_graph(&state, &engagement.id).await;
    let run = auto_run(&engagement, AutoRunState::Evaluating);

    let assessment = evaluate(&state, &run, 200).await.expect("evaluate goal");

    assert!(assessment.succeeded);
    assert_eq!(assessment.evaluator_version, AUTO_GOAL_EVALUATOR_VERSION);
    assert_eq!(assessment.evaluated_at, 200);
    assert_eq!(assessment.criteria.len(), 4);
    assert!(
        assessment
            .criteria
            .iter()
            .all(|criterion| criterion.satisfied)
    );
    assert_eq!(assessment.evidence_ids, vec!["evidence-1".to_string()]);
}

#[tokio::test]
async fn evaluator_never_accepts_a_claim_without_structured_evidence() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = engagement(vec![StructuredSuccessCriterion {
        id: "evidence".to_string(),
        description: "Capture one evidence item".to_string(),
        predicate: SuccessPredicate::Evidence {
            minimum_items: 1,
            reproduction_required: false,
        },
    }]);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");

    let assessment = evaluate(
        &state,
        &auto_run(&engagement, AutoRunState::Evaluating),
        200,
    )
    .await
    .expect("evaluate goal");

    assert!(!assessment.succeeded);
    assert_eq!(assessment.evidence_ids, Vec::<String>::new());
    assert_eq!(assessment.criteria[0].satisfied, false);
}

#[tokio::test]
async fn reproduction_predicate_requires_reproducible_artifact_backed_evidence() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = engagement(vec![StructuredSuccessCriterion {
        id: "reproduce".to_string(),
        description: "Capture reproducible evidence".to_string(),
        predicate: SuccessPredicate::Evidence {
            minimum_items: 1,
            reproduction_required: true,
        },
    }]);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    put_execution_and_evidence(&state, &engagement.id).await;

    let assessment = evaluate(
        &state,
        &auto_run(&engagement, AutoRunState::Evaluating),
        200,
    )
    .await
    .expect("evaluate goal");

    assert!(!assessment.succeeded);
    assert_eq!(assessment.criteria[0].satisfied, false);
}

#[tokio::test]
async fn completed_turn_persists_success_and_completes_the_engagement() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = engagement(vec![StructuredSuccessCriterion {
        id: "evidence".to_string(),
        description: "Capture one evidence item".to_string(),
        predicate: SuccessPredicate::Evidence {
            minimum_items: 1,
            reproduction_required: false,
        },
    }]);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    put_execution_and_evidence(&state, &engagement.id).await;
    state
        .store
        .put_auto_run(&auto_run(&engagement, AutoRunState::Running))
        .await
        .expect("store run");

    crate::auto_run::on_turn_completed(
        &state,
        &engagement.id,
        &json!({"turn": {"status": "completed"}}),
    )
    .await;

    let completed = state
        .store
        .auto_run(&engagement.id)
        .await
        .expect("load run")
        .expect("run");
    assert_eq!(completed.state, AutoRunState::Succeeded);
    assert_eq!(
        completed.stop_reason,
        Some(AutoStopReason::SuccessCriteriaMet)
    );
    assert!(
        completed
            .last_goal_assessment
            .as_ref()
            .is_some_and(|assessment| assessment.succeeded)
    );
    assert_eq!(
        state
            .store
            .engagement(&engagement.id)
            .await
            .expect("load engagement")
            .status,
        EngagementStatus::Completed
    );
}

fn structured_criteria() -> Vec<StructuredSuccessCriterion> {
    vec![
        StructuredSuccessCriterion {
            id: "evidence".to_string(),
            description: "Capture evidence".to_string(),
            predicate: SuccessPredicate::Evidence {
                minimum_items: 1,
                reproduction_required: false,
            },
        },
        StructuredSuccessCriterion {
            id: "coverage".to_string(),
            description: "Cover the declared target set".to_string(),
            predicate: SuccessPredicate::Coverage {
                minimum_basis_points: 8_000,
            },
        },
        StructuredSuccessCriterion {
            id: "finding".to_string(),
            description: "Validate a high-confidence finding".to_string(),
            predicate: SuccessPredicate::Finding {
                minimum_count: 1,
                minimum_severity: FindingSeverity::High,
                minimum_confidence_basis_points: 9_000,
            },
        },
        StructuredSuccessCriterion {
            id: "path".to_string(),
            description: "Validate the requested path".to_string(),
            predicate: SuccessPredicate::AttackPath {
                destination_role: "targetRole".to_string(),
                access_level: "administrator".to_string(),
                minimum_confidence_basis_points: 9_000,
                reproducible_evidence: false,
            },
        },
    ]
}

async fn put_valid_evidence_graph(state: &GatewayState, engagement_id: &str) {
    put_execution_and_evidence(state, engagement_id).await;
    state
        .store
        .put_finding(&Finding {
            id: "finding-1".to_string(),
            engagement_id: engagement_id.to_string(),
            asset_id: None,
            evidence_ids: vec!["evidence-1".to_string()],
            title: "Validated finding".to_string(),
            severity: FindingSeverity::High,
            confidence_basis_points: 9_500,
            description: "Tool-derived finding".to_string(),
            remediation: None,
        })
        .await
        .expect("store finding");
    state
        .store
        .put_coverage(&Coverage {
            id: "coverage-1".to_string(),
            engagement_id: engagement_id.to_string(),
            dimension: "authorizedAssets".to_string(),
            covered_items: 4,
            total_items: 5,
            evidence_ids: vec!["evidence-1".to_string()],
            measured_at: 150,
        })
        .await
        .expect("store coverage");
    state
        .store
        .put_attack_path(&AttackPath {
            id: "path-1".to_string(),
            engagement_id: engagement_id.to_string(),
            hops: vec![AttackPathHop {
                source: StateSubject::Engagement,
                destination: StateSubject::Engagement,
                capability: "validatedAccess".to_string(),
                evidence_ids: vec!["evidence-1".to_string()],
            }],
            destination_role: "targetRole".to_string(),
            access_level: "administrator".to_string(),
            confidence_basis_points: 9_500,
            reproducible: false,
            validated_at: 160,
        })
        .await
        .expect("store attack path");
}

async fn put_execution_and_evidence(state: &GatewayState, engagement_id: &str) {
    state
        .store
        .put_execution(&Execution {
            id: "execution-1".to_string(),
            engagement_id: engagement_id.to_string(),
            test_case_id: None,
            task_id: None,
            turn_id: "turn-1".to_string(),
            runner: "local:test".to_string(),
            status: ExecutionStatus::Completed,
            started_at: 100,
            completed_at: Some(110),
            exit_code: Some(0),
            duration_ms: Some(10_000),
            argv: vec!["test-tool".to_string()],
            command_sha256: "command-sha256".to_string(),
            cwd: "/tmp".to_string(),
            process_id: None,
            tool: None,
            tool_inventory_sha256: "inventory-sha256".to_string(),
            stdout_sha256: Some("stdout-sha256".to_string()),
            stderr_sha256: None,
            stdin_sha256: None,
            stdout_bytes: 10,
            stderr_bytes: 0,
            stdin_bytes: 0,
        })
        .await
        .expect("store execution");
    state
        .store
        .put_evidence(&Evidence {
            id: "evidence-1".to_string(),
            engagement_id: engagement_id.to_string(),
            finding_id: None,
            execution_id: Some("execution-1".to_string()),
            artifact_id: None,
            summary: "Tool-derived evidence".to_string(),
            reproducible: false,
            captured_at: 120,
        })
        .await
        .expect("store evidence");
}

fn engagement(structured_criteria: Vec<StructuredSuccessCriterion>) -> Engagement {
    Engagement {
        id: "engagement-auto-evaluator".to_string(),
        name: "Auto evaluator".to_string(),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Reach the deterministic success criteria".to_string(),
            success_criteria: vec!["The model may claim success only with evidence".to_string()],
            structured_criteria,
        },
        entry_points: vec!["10.10.0.10".to_string()],
        mode: ExecutionMode::Auto,
        llm_profile: "default".to_string(),
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: Vec::new(),
                domains: Vec::new(),
                ports: Vec::new(),
            },
            identities: Vec::new(),
            capabilities: vec!["network.discovery".to_string()],
            environment: EnvironmentClass::Lab,
            window: AuthorizationWindow {
                starts_at: None,
                expires_at: Some(4_000_000_000),
            },
        },
        policy_revision: "policy-v1".to_string(),
        thread_id: None,
        created_at: 1,
        updated_at: 1,
    }
}

fn auto_run(engagement: &Engagement, state: AutoRunState) -> AutoRun {
    AutoRun {
        engagement_id: engagement.id.clone(),
        config: AutoRunConfig {
            objective: engagement.objective.clone(),
            authorization: engagement.authorization.clone(),
            llm_profile: AutoLlmProfileSnapshot {
                name: "default".to_string(),
                model: "model".to_string(),
                base_url: "https://example.invalid/v1".to_string(),
                protocol: "responses".to_string(),
                timeout_seconds: 30,
                reasoning_level: "medium".to_string(),
                context_budget: 32_000,
                config_sha256: "a".repeat(64),
            },
            tools_snapshot_sha256: "b".repeat(64),
            policy_revision: engagement.policy_revision.clone(),
            expires_at: 4_000_000_000,
            limits: AutoRunLimits::default(),
        },
        state,
        stop_reason: None,
        current_subgoal: Some("Collect deterministic evidence".to_string()),
        turns_started: 1,
        turns_completed: 0,
        tool_calls: 1,
        consecutive_failures: 0,
        no_progress_turns: 0,
        last_goal_assessment: None,
        started_at: Some(100),
        updated_at: 100,
    }
}
