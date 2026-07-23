use super::*;
use crate::AssessmentObjective;
use crate::AttackPathHop;
use crate::AuthorizationScope;
use crate::AuthorizationWindow;
use crate::Engagement;
use crate::EngagementStatus;
use crate::EnvironmentClass;
use crate::Evidence;
use crate::ExecutionMode;
use crate::ExecutionStatus;
use crate::Finding;
use crate::FindingSeverity;
use crate::HypothesisStatus;
use crate::Scope;
use ipnet::IpNet;
use pretty_assertions::assert_eq;
use tempfile::TempDir;

#[tokio::test]
async fn complete_target_state_graph_is_persisted_and_validated() {
    let (_temp, store) = store().await;
    let asset = asset();
    let identity = identity();
    let observation = observation();
    let hypothesis = hypothesis();
    let test_case = test_case();
    let execution = execution();
    let evidence = evidence();
    let finding = finding();
    let attack_path = attack_path();
    let coverage = coverage();

    store.put_asset(&asset).await.expect("store asset");
    store.put_identity(&identity).await.expect("store identity");
    store
        .put_observation(&observation)
        .await
        .expect("store observation");
    store
        .put_hypothesis(&hypothesis)
        .await
        .expect("store hypothesis");
    store
        .put_test_case(&test_case)
        .await
        .expect("store test case");
    store
        .put_execution(&execution)
        .await
        .expect("store execution");
    store.put_evidence(&evidence).await.expect("store evidence");
    store.put_finding(&finding).await.expect("store finding");
    store
        .put_attack_path(&attack_path)
        .await
        .expect("store attack path");
    store.put_coverage(&coverage).await.expect("store coverage");

    store
        .validate_evidence_chain("eng-1")
        .await
        .expect("evidence chain should be complete");
    assert_eq!(
        (
            store.identities("eng-1").await.expect("identities"),
            store.observations("eng-1").await.expect("observations"),
            store.hypotheses("eng-1").await.expect("hypotheses"),
            store.test_cases("eng-1").await.expect("test cases"),
            store.executions("eng-1").await.expect("executions"),
            store.attack_paths("eng-1").await.expect("attack paths"),
            store.coverage("eng-1").await.expect("coverage"),
        ),
        (
            vec![identity],
            vec![observation],
            vec![hypothesis],
            vec![test_case],
            vec![execution],
            vec![attack_path],
            vec![coverage],
        )
    );
}

#[tokio::test]
async fn evidence_chain_rejects_an_unknown_subject() {
    let (_temp, store) = store().await;
    let observation = Observation {
        subject: StateSubject::Asset {
            asset_id: "missing-asset".to_string(),
        },
        ..observation()
    };
    store
        .put_observation(&observation)
        .await
        .expect("store observation");

    let error = store
        .validate_evidence_chain("eng-1")
        .await
        .expect_err("unknown subject should fail validation");
    assert!(matches!(
        error,
        StateError::BrokenChainReference {
            entity_kind: "observation",
            reference_kind: "asset",
            ..
        }
    ));
}

#[tokio::test]
async fn invalid_target_state_is_rejected_before_persistence() {
    let (_temp, store) = store().await;
    let invalid = Coverage {
        total_items: 0,
        ..coverage()
    };

    let error = store
        .put_coverage(&invalid)
        .await
        .expect_err("invalid coverage should be rejected");
    assert!(matches!(
        error,
        StateError::InvalidTargetState(TargetStateError::InvalidCoverage)
    ));
    assert_eq!(
        store.coverage("eng-1").await.expect("coverage"),
        Vec::<Coverage>::new()
    );
}

async fn store() -> (TempDir, StateStore) {
    let temp = TempDir::new().expect("temp dir");
    let store = StateStore::open(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");
    store
        .put_engagement(&engagement())
        .await
        .expect("store engagement");
    (temp, store)
}

fn engagement() -> Engagement {
    Engagement {
        id: "eng-1".to_string(),
        name: "Authorized lab".to_string(),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Validate authorized identity paths".to_string(),
            success_criteria: vec!["Preserve reproducible evidence".to_string()],
            structured_criteria: Vec::new(),
        },
        entry_points: vec!["10.10.20.10".to_string()],
        mode: ExecutionMode::Native,
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: vec!["10.10.20.0/24".parse::<IpNet>().expect("CIDR")],
                domains: Vec::new(),
                ports: vec![445],
            },
            identities: Vec::new(),
            capabilities: vec!["attack_path.analysis".to_string()],
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

fn asset() -> crate::Asset {
    crate::Asset {
        id: "dc-1".to_string(),
        engagement_id: "eng-1".to_string(),
        kind: "host".to_string(),
        value: "10.10.20.10".to_string(),
        discovered_at: 10,
    }
}

fn identity() -> Identity {
    Identity {
        id: "identity-1".to_string(),
        engagement_id: "eng-1".to_string(),
        asset_id: Some("dc-1".to_string()),
        kind: "domainAccount".to_string(),
        principal: "test.user".to_string(),
        domain: Some("LAB".to_string()),
        tenant: None,
        discovered_at: 20,
    }
}

fn observation() -> Observation {
    Observation {
        id: "observation-1".to_string(),
        engagement_id: "eng-1".to_string(),
        subject: StateSubject::Identity {
            identity_id: "identity-1".to_string(),
        },
        execution_id: None,
        source: "operator".to_string(),
        kind: "credentialAvailable".to_string(),
        summary: "Authorized test credential is available".to_string(),
        confidence_basis_points: 10_000,
        observed_at: 30,
    }
}

fn hypothesis() -> Hypothesis {
    Hypothesis {
        id: "hypothesis-1".to_string(),
        engagement_id: "eng-1".to_string(),
        observation_ids: vec!["observation-1".to_string()],
        statement: "Credential reuse may reach the domain controller".to_string(),
        status: HypothesisStatus::Validated,
        confidence_basis_points: 9_000,
        created_at: 40,
    }
}

fn test_case() -> TestCase {
    TestCase {
        id: "test-1".to_string(),
        engagement_id: "eng-1".to_string(),
        hypothesis_id: "hypothesis-1".to_string(),
        target: StateSubject::Asset {
            asset_id: "dc-1".to_string(),
        },
        capability: "credentialValidation".to_string(),
        expected_evidence: "Authenticated domain controller response".to_string(),
        created_at: 50,
    }
}

fn execution() -> Execution {
    Execution {
        id: "execution-1".to_string(),
        engagement_id: "eng-1".to_string(),
        test_case_id: "test-1".to_string(),
        task_id: None,
        runner: "native-tool".to_string(),
        status: ExecutionStatus::Completed,
        started_at: 60,
        completed_at: Some(70),
        exit_code: Some(0),
    }
}

fn evidence() -> Evidence {
    Evidence {
        id: "evidence-1".to_string(),
        engagement_id: "eng-1".to_string(),
        finding_id: Some("finding-1".to_string()),
        execution_id: Some("execution-1".to_string()),
        artifact_id: None,
        summary: "Authenticated access was reproduced".to_string(),
        captured_at: 80,
    }
}

fn finding() -> Finding {
    Finding {
        id: "finding-1".to_string(),
        engagement_id: "eng-1".to_string(),
        asset_id: Some("dc-1".to_string()),
        evidence_ids: vec!["evidence-1".to_string()],
        title: "Credential reuse reaches domain controller".to_string(),
        severity: FindingSeverity::Critical,
        description: "The authorized credential provides administrative access".to_string(),
        remediation: Some("Rotate the credential and restrict administrative logon".to_string()),
    }
}

fn attack_path() -> AttackPath {
    AttackPath {
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
        validated_at: 90,
    }
}

fn coverage() -> Coverage {
    Coverage {
        id: "coverage-1".to_string(),
        engagement_id: "eng-1".to_string(),
        dimension: "authorizedAssets".to_string(),
        covered_items: 1,
        total_items: 1,
        measured_at: 100,
    }
}
