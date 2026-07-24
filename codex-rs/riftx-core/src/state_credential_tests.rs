use super::*;
use crate::AssessmentObjective;
use crate::AuthorizationScope;
use crate::AuthorizationWindow;
use crate::CredentialKind;
use crate::Engagement;
use crate::EngagementStatus;
use crate::EnvironmentClass;
use crate::ExecutionMode;
use crate::Scope;
use pretty_assertions::assert_eq;
use tempfile::TempDir;

#[tokio::test]
async fn credential_references_and_grants_are_engagement_scoped() {
    let temp = TempDir::new().expect("temp dir");
    let store = open_test_store(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");
    let engagement = engagement();
    store.put_engagement(&engagement).await.expect("engagement");
    let reference = CredentialReference {
        id: "credential-1".to_string(),
        engagement_id: engagement.id.clone(),
        label: "Authorized lab user".to_string(),
        kind: CredentialKind::Password,
        storage_key: "engagement/engagement-1/credential/credential-1".to_string(),
        username: Some("lab.user".to_string()),
        domain: Some("LAB".to_string()),
        configured: true,
        created_at: 100,
    };
    let grant = CredentialGrant {
        id: "grant-1".to_string(),
        engagement_id: engagement.id.clone(),
        credential_id: reference.id.clone(),
        allowed_targets: engagement.authorization.network.clone(),
        allowed_capabilities: vec!["credential.testing".to_string()],
        max_uses: 5,
        max_failures_per_identity: 2,
        starts_at: Some(100),
        expires_at: 200,
        created_at: 100,
        revoked_at: None,
    };

    store
        .put_credential_reference(&reference)
        .await
        .expect("reference");
    store.put_credential_grant(&grant).await.expect("grant");

    assert_eq!(
        store
            .credential_references(&engagement.id)
            .await
            .expect("references"),
        vec![reference.clone()]
    );
    assert_eq!(
        store
            .credential_grants(&engagement.id)
            .await
            .expect("grants"),
        vec![grant]
    );
    assert_eq!(
        store
            .credential_reference("another-engagement", &reference.id)
            .await
            .expect("cross-engagement lookup"),
        None
    );
    assert!(
        store
            .delete_credential_reference(&engagement.id, &reference.id)
            .await
            .expect("delete reference")
    );
    assert!(
        store
            .credential_references(&engagement.id)
            .await
            .expect("references after delete")
            .is_empty()
    );
}

fn engagement() -> Engagement {
    Engagement {
        id: "engagement-1".to_string(),
        name: "Credential state".to_string(),
        status: EngagementStatus::Draft,
        objective: AssessmentObjective {
            summary: "Validate credential state".to_string(),
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
        policy_revision: "revision-1".to_string(),
        thread_id: None,
        created_at: 100,
        updated_at: 100,
    }
}
