use super::*;
use crate::ApprovalActor;
use crate::ApprovalDecisionReason;
use crate::ApprovalOutcome;
use crate::ApprovalRecord;
use crate::ApprovalRequestKind;
use crate::Artifact;
use crate::AssessmentObjective;
use crate::Asset;
use crate::AssetRelation;
use crate::AuthorizationScope;
use crate::AuthorizationWindow;
use crate::AutoLlmProfileSnapshot;
use crate::AutoRun;
use crate::AutoRunConfig;
use crate::AutoRunLimits;
use crate::AutoRunState;
use crate::EngagementStatus;
use crate::EnvironmentClass;
use crate::ExecutionMode;
use crate::RecordedApprovalDecision;
use crate::Scope;
use crate::Service;
use crate::Task;
use crate::TaskStatus;
use ipnet::IpNet;
use pretty_assertions::assert_eq;
use tempfile::TempDir;

#[tokio::test]
async fn snapshot_returns_one_engagements_state_from_one_read_model() {
    let temp = TempDir::new().expect("temp dir");
    let store = super::super::open_test_store(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");
    let engagement = engagement_fixture("eng-1");
    let other_engagement = engagement_fixture("eng-2");
    store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    store
        .put_engagement(&other_engagement)
        .await
        .expect("store other engagement");

    let assets = vec![asset("asset-1", "eng-1"), asset("asset-2", "eng-1")];
    for asset in &assets {
        store.put_asset(asset).await.expect("store asset");
    }
    store
        .put_asset(&asset("other-asset", "eng-2"))
        .await
        .expect("store other asset");
    let asset_relations = vec![AssetRelation {
        id: "relation-1".to_string(),
        engagement_id: "eng-1".to_string(),
        source_asset_id: "asset-1".to_string(),
        target_asset_id: "asset-2".to_string(),
        kind: "routesTo".to_string(),
        evidence_id: None,
        discovered_at: 4,
    }];
    store
        .put_asset_relation(&asset_relations[0])
        .await
        .expect("store asset relation");
    let services = vec![Service {
        id: "service-1".to_string(),
        engagement_id: "eng-1".to_string(),
        asset_id: "asset-1".to_string(),
        transport: "tcp".to_string(),
        port: 443,
        name: Some("https".to_string()),
        version: None,
    }];
    store
        .put_service(&services[0])
        .await
        .expect("store service");
    let tasks = vec![Task {
        id: "task-1".to_string(),
        engagement_id: "eng-1".to_string(),
        kind: "agentTurn".to_string(),
        status: TaskStatus::Completed,
        turn_id: Some("turn-1".to_string()),
        error: None,
    }];
    store.put_task(&tasks[0]).await.expect("store task");
    let artifacts = vec![Artifact {
        id: "artifact-1".to_string(),
        engagement_id: "eng-1".to_string(),
        execution_id: None,
        path: "artifacts/result.json".to_string(),
        media_type: "application/json".to_string(),
        sha256: "a".repeat(64),
        size_bytes: 12,
        created_at: 5,
    }];
    store
        .put_artifact(&artifacts[0])
        .await
        .expect("store artifact");
    let auto_run = auto_run(&engagement);
    store.put_auto_run(&auto_run).await.expect("store Auto run");
    let approvals = vec![approval("approval-1", "eng-1")];
    store
        .put_approval(&approvals[0])
        .await
        .expect("store approval");
    store
        .put_approval(&approval("other-approval", "eng-2"))
        .await
        .expect("store other approval");

    assert_eq!(
        store
            .engagement_state_snapshot("eng-1")
            .await
            .expect("engagement snapshot"),
        EngagementStateSnapshot {
            engagement,
            auto_run: Some(auto_run),
            assets,
            asset_relations,
            services,
            identities: Vec::new(),
            observations: Vec::new(),
            hypotheses: Vec::new(),
            test_cases: Vec::new(),
            executions: Vec::new(),
            findings: Vec::new(),
            evidence: Vec::new(),
            attack_paths: Vec::new(),
            coverage: Vec::new(),
            tasks,
            artifacts,
            approvals,
        }
    );
}

#[tokio::test]
async fn snapshot_requires_an_existing_engagement() {
    let temp = TempDir::new().expect("temp dir");
    let store = super::super::open_test_store(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");

    let error = store
        .engagement_state_snapshot("missing")
        .await
        .expect_err("missing engagement should fail");
    assert!(matches!(error, StateError::EngagementNotFound(id) if id == "missing"));
}

fn engagement_fixture(id: &str) -> Engagement {
    Engagement {
        id: id.to_string(),
        name: format!("Authorized lab {id}"),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Preserve a consistent report snapshot".to_string(),
            success_criteria: vec!["Export one coherent state".to_string()],
            structured_criteria: Vec::new(),
        },
        entry_points: vec!["10.10.0.10".to_string()],
        mode: ExecutionMode::Pentest,
        llm_profile: "default".to_string(),
        auto_limits: None,
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: vec!["10.10.0.0/24".parse::<IpNet>().expect("CIDR")],
                domains: Vec::new(),
                ports: vec![443],
            },
            identities: Vec::new(),
            capabilities: vec!["web.discovery".to_string()],
            environment: EnvironmentClass::Lab,
            window: AuthorizationWindow {
                starts_at: None,
                expires_at: Some(200),
            },
        },
        policy_revision: "revision-1".to_string(),
        thread_id: None,
        created_at: 1,
        updated_at: 1,
    }
}

fn asset(id: &str, engagement_id: &str) -> Asset {
    Asset {
        id: id.to_string(),
        engagement_id: engagement_id.to_string(),
        kind: "host".to_string(),
        value: format!("{id}.invalid"),
        discovered_at: 3,
    }
}

fn auto_run(engagement: &Engagement) -> AutoRun {
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
                config_sha256: "b".repeat(64),
            },
            tools_snapshot_sha256: "c".repeat(64),
            policy_revision: engagement.policy_revision.clone(),
            expires_at: 200,
            limits: AutoRunLimits::default(),
        },
        state: AutoRunState::Ready,
        stop_reason: None,
        current_subgoal: None,
        turns_started: 0,
        turns_completed: 0,
        tool_calls: 0,
        consecutive_failures: 0,
        no_progress_turns: 0,
        unavailable_tools: Vec::new(),
        last_goal_assessment: None,
        progress_baseline: None,
        last_progress_assessment: None,
        started_at: None,
        updated_at: 2,
    }
}

fn approval(id: &str, engagement_id: &str) -> ApprovalRecord {
    ApprovalRecord {
        id: id.to_string(),
        engagement_id: engagement_id.to_string(),
        kind: ApprovalRequestKind::Command,
        requested_at: 6,
        decided_at: Some(7),
        requested_decision: Some(RecordedApprovalDecision::Approve),
        outcome: ApprovalOutcome::Approved,
        actor: Some(ApprovalActor::LocalOperator),
        decision_reason: Some(ApprovalDecisionReason::Approved),
        policy_revision: "revision-1".to_string(),
        execution_binding_sha256: "d".repeat(64),
        command_sha256: "e".repeat(64),
        argument_sha256: "f".repeat(64),
        display_argv: vec!["nmap".to_string(), "10.10.0.10".to_string()],
        cwd: Some("/workspace".to_string()),
        executable_names: vec!["nmap".to_string()],
    }
}
