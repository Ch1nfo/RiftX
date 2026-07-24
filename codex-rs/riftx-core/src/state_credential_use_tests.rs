use super::*;
use crate::AssessmentObjective;
use crate::AuthorizationScope;
use crate::AuthorizationWindow;
use crate::CredentialKind;
use crate::CredentialUseTarget;
use crate::EngagementStatus;
use crate::EnvironmentClass;
use crate::ExecutionMode;
use crate::Scope;
use pretty_assertions::assert_eq;
use tempfile::TempDir;

#[tokio::test]
async fn reservation_enforces_concurrency_use_and_failure_limits() {
    let (_temp, store, grant) = fixture(3, 1).await;
    let first = request("use-1", "10.10.0.10");
    let second = request("use-2", "10.10.0.11");

    let reserved = store
        .reserve_credential_use(&first)
        .await
        .expect("first reservation");
    assert_eq!(reserved.status, CredentialUseStatus::Reserved);
    let payload: Vec<u8> =
        sqlx::query_scalar("SELECT data FROM credential_grant_uses WHERE id = ?")
            .bind(&reserved.id)
            .fetch_one(&store.pool)
            .await
            .expect("raw credential use");
    assert!(payload.starts_with(b"RXE1"));
    assert!(!String::from_utf8_lossy(&payload).contains(&first.target.host));
    assert!(matches!(
        store.reserve_credential_use(&second).await,
        Err(StateError::CredentialUseInProgress)
    ));
    store
        .complete_credential_use(
            &grant.engagement_id,
            &reserved.id,
            CredentialUseOutcome::AuthenticationFailed,
            151,
        )
        .await
        .expect("failed completion");
    assert!(matches!(
        store.reserve_credential_use(&second).await,
        Err(StateError::CredentialFailureLimitExceeded)
    ));
}

#[tokio::test]
async fn execution_failures_do_not_consume_the_authentication_failure_budget() {
    let (_temp, store, grant) = fixture(2, 1).await;
    let first = request("use-1", "10.10.0.10");
    let second = request("use-2", "10.10.0.11");

    store
        .reserve_credential_use(&first)
        .await
        .expect("first reservation");
    store
        .complete_credential_use(
            &grant.engagement_id,
            &first.id,
            CredentialUseOutcome::ExecutionFailed,
            151,
        )
        .await
        .expect("execution failure");
    store
        .reserve_credential_use(&second)
        .await
        .expect("second reservation");
    assert_eq!(
        store
            .credential_uses(&grant.engagement_id)
            .await
            .expect("credential uses")
            .len(),
        2
    );
    assert!(matches!(
        store
            .reserve_credential_use(&request("use-3", "10.10.0.12"))
            .await,
        Err(StateError::CredentialUseLimitExceeded)
    ));
}

#[tokio::test]
async fn reservation_rejects_stale_policy_scope_and_capability() {
    let (_temp, store, _grant) = fixture(3, 2).await;
    let mut stale = request("use-stale", "10.10.0.10");
    stale.policy_revision = "b".repeat(64);
    assert!(matches!(
        store.reserve_credential_use(&stale).await,
        Err(StateError::CredentialPolicyRevisionMismatch)
    ));
    let outside = request("use-outside", "10.20.0.10");
    assert!(matches!(
        store.reserve_credential_use(&outside).await,
        Err(StateError::CredentialTargetDenied(_))
    ));
    let mut capability = request("use-capability", "10.10.0.10");
    capability.capability = "lateral_movement".to_string();
    assert!(matches!(
        store.reserve_credential_use(&capability).await,
        Err(StateError::CredentialCapabilityDenied(_))
    ));
}

#[tokio::test]
async fn reservation_rejects_a_reference_without_a_secret() {
    let (_temp, store, grant) = fixture(1, 1).await;
    let mut reference = store
        .credential_reference(&grant.engagement_id, &grant.credential_id)
        .await
        .expect("credential query")
        .expect("credential reference");
    reference.configured = false;
    store
        .put_credential_reference(&reference)
        .await
        .expect("unconfigured reference");

    assert!(matches!(
        store
            .reserve_credential_use(&request("use-unconfigured", "10.10.0.10"))
            .await,
        Err(StateError::CredentialSecretUnavailable(id)) if id == reference.id
    ));
}

#[tokio::test]
async fn concurrent_reservations_allow_only_one_use_per_identity() {
    let (_temp, store, _grant) = fixture(2, 2).await;
    let attempts = (0..8)
        .map(|index| {
            let store = store.clone();
            tokio::spawn(async move {
                store
                    .reserve_credential_use(&request(
                        &format!("use-{index}"),
                        &format!("10.10.0.{}", index + 10),
                    ))
                    .await
            })
        })
        .collect::<Vec<_>>();
    let mut successes = 0;
    for attempt in attempts {
        if attempt.await.expect("reservation task").is_ok() {
            successes += 1;
        }
    }

    assert_eq!(successes, 1);
}

async fn fixture(max_uses: u32, max_failures: u32) -> (TempDir, StateStore, CredentialGrant) {
    let temp = TempDir::new().expect("temp dir");
    let store = open_test_store(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");
    let engagement = Engagement {
        id: "engagement-1".to_string(),
        name: "Credential use".to_string(),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Validate credential use".to_string(),
            success_criteria: Vec::new(),
            structured_criteria: Vec::new(),
        },
        entry_points: vec!["10.10.0.10".to_string()],
        mode: ExecutionMode::Native,
        llm_profile: "default".to_string(),
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: vec!["10.10.0.0/24".parse().expect("CIDR")],
                domains: Vec::new(),
                ports: vec![445],
            },
            identities: Vec::new(),
            capabilities: vec!["credential.testing".to_string()],
            environment: EnvironmentClass::Lab,
            window: AuthorizationWindow {
                starts_at: None,
                expires_at: Some(300),
            },
        },
        policy_revision: "a".repeat(64),
        thread_id: None,
        created_at: 100,
        updated_at: 100,
    };
    store.put_engagement(&engagement).await.expect("engagement");
    let reference = CredentialReference {
        id: "credential-1".to_string(),
        engagement_id: engagement.id.clone(),
        label: "Lab user".to_string(),
        kind: CredentialKind::Password,
        storage_key: "engagement/engagement-1/credential/credential-1".to_string(),
        username: Some("administrator".to_string()),
        domain: Some("lab.example".to_string()),
        configured: true,
        created_at: 100,
    };
    store
        .put_credential_reference(&reference)
        .await
        .expect("reference");
    let grant = CredentialGrant {
        id: "grant-1".to_string(),
        engagement_id: engagement.id,
        credential_id: reference.id,
        allowed_targets: engagement.authorization.network,
        allowed_capabilities: vec!["credential.testing".to_string()],
        max_uses,
        max_failures_per_identity: max_failures,
        starts_at: Some(100),
        expires_at: 200,
        created_at: 100,
        revoked_at: None,
    };
    store.put_credential_grant(&grant).await.expect("grant");
    (temp, store, grant)
}

fn request(id: &str, host: &str) -> CredentialUseRequest {
    CredentialUseRequest {
        id: id.to_string(),
        engagement_id: "engagement-1".to_string(),
        grant_id: "grant-1".to_string(),
        target: CredentialUseTarget {
            host: host.to_string(),
            port: Some(445),
        },
        capability: "credential.testing".to_string(),
        policy_revision: "a".repeat(64),
        requested_at: 150,
    }
}
