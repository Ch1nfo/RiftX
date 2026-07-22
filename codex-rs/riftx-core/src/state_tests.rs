use super::*;
use crate::Scope;
use ipnet::IpNet;
use pretty_assertions::assert_eq;
use tempfile::TempDir;

fn engagement() -> Engagement {
    Engagement {
        id: "eng-1".to_string(),
        name: "Juice Shop".to_string(),
        status: EngagementStatus::Draft,
        scope: Scope {
            cidrs: vec!["10.10.0.0/24".parse::<IpNet>().expect("CIDR")],
            domains: vec!["juice.local".to_string()],
            ports: vec![80, 443],
        },
        tool_profile: "recon".to_string(),
        policy_revision: "revision-1".to_string(),
        sandbox_id: None,
        thread_id: None,
        created_at: 1,
        updated_at: 1,
    }
}

#[tokio::test]
async fn engagement_lifecycle_is_persisted() {
    let temp = TempDir::new().expect("temp dir");
    let store = StateStore::open(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");
    let draft = engagement();
    store.put_engagement(&draft).await.expect("insert draft");
    assert_eq!(
        store.engagement(&draft.id).await.expect("read draft"),
        draft
    );

    let active = store
        .transition_engagement("eng-1", EngagementStatus::Active, 2)
        .await
        .expect("activate");
    assert_eq!(active.status, EngagementStatus::Active);
    assert_eq!(active.updated_at, 2);
}

#[tokio::test]
async fn invalid_engagement_transition_is_rejected() {
    let temp = TempDir::new().expect("temp dir");
    let store = StateStore::open(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");
    store
        .put_engagement(&engagement())
        .await
        .expect("insert draft");
    let error = store
        .transition_engagement("eng-1", EngagementStatus::Completed, 2)
        .await
        .expect_err("draft cannot complete");
    assert!(matches!(error, StateError::InvalidTransition { .. }));
}
