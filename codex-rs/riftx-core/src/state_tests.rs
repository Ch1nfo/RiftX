use super::*;
use crate::AssessmentObjective;
use crate::AuthorizationScope;
use crate::AuthorizationWindow;
use crate::AutoLlmProfileSnapshot;
use crate::AutoRun;
use crate::AutoRunConfig;
use crate::AutoRunLimits;
use crate::AutoRunState;
use crate::ConversationEntryDraft;
use crate::ConversationKind;
use crate::ConversationRole;
use crate::EnvironmentClass;
use crate::ExecutionMode;
use crate::Scope;
use crate::TaskStatus;
use ipnet::IpNet;
use pretty_assertions::assert_eq;
use tempfile::TempDir;

fn engagement() -> Engagement {
    Engagement {
        id: "eng-1".to_string(),
        name: "Juice Shop".to_string(),
        status: EngagementStatus::Draft,
        objective: AssessmentObjective {
            summary: "Identify exploitable web risks".to_string(),
            success_criteria: vec!["Record evidence for validated findings".to_string()],
            structured_criteria: Vec::new(),
        },
        entry_points: vec!["juice.local".to_string()],
        mode: ExecutionMode::Pentest,
        llm_profile: "default".to_string(),
        auto_limits: None,
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: vec!["10.10.0.0/24".parse::<IpNet>().expect("CIDR")],
                domains: vec!["juice.local".to_string()],
                ports: vec![80, 443],
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

#[tokio::test]
async fn engagement_lifecycle_is_persisted() {
    let temp = TempDir::new().expect("temp dir");
    let store = open_test_store(&temp.path().join("state.sqlite"))
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
    assert_eq!(
        store.engagements().await.expect("list engagements"),
        vec![active]
    );

    let interrupted = store
        .transition_engagement("eng-1", EngagementStatus::Interrupted, 3)
        .await
        .expect("interrupt");
    let reactivated = store
        .transition_engagement("eng-1", EngagementStatus::Active, 4)
        .await
        .expect("reactivate");
    assert_eq!(reactivated.status, EngagementStatus::Active);
    assert_eq!(reactivated.id, interrupted.id);
}

#[tokio::test]
async fn auto_run_checkpoint_round_trips_with_the_engagement_key() {
    let temp = TempDir::new().expect("temp dir");
    let store = open_test_store(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");
    let engagement = engagement();
    store
        .put_engagement(&engagement)
        .await
        .expect("insert engagement");
    let run = AutoRun {
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
        updated_at: 1,
    };
    store.put_auto_run(&run).await.expect("put Auto run");
    assert_eq!(
        store.auto_run(&engagement.id).await.expect("read Auto run"),
        Some(run)
    );
}

#[tokio::test]
async fn authorization_expiry_is_a_terminal_persisted_transition() {
    let temp = TempDir::new().expect("temp dir");
    let store = open_test_store(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");
    store
        .put_engagement(&engagement())
        .await
        .expect("insert draft");
    let expired = store
        .transition_engagement("eng-1", EngagementStatus::Expired, 2)
        .await
        .expect("expire");

    assert_eq!(expired.status, EngagementStatus::Expired);
    assert!(
        store
            .transition_engagement("eng-1", EngagementStatus::Active, 3)
            .await
            .is_err()
    );
}

#[tokio::test]
async fn task_is_resolved_by_turn_id() {
    let temp = TempDir::new().expect("temp dir");
    let store = open_test_store(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");
    store
        .put_engagement(&engagement())
        .await
        .expect("insert draft");
    let task = Task {
        id: "task-1".to_string(),
        engagement_id: "eng-1".to_string(),
        kind: "agent_turn".to_string(),
        status: TaskStatus::Running,
        turn_id: Some("turn-1".to_string()),
        error: None,
    };
    store.put_task(&task).await.expect("insert task");
    assert_eq!(
        store
            .task_for_turn("eng-1", "turn-1")
            .await
            .expect("lookup task"),
        Some(task)
    );
}

#[tokio::test]
async fn multi_asset_relationship_is_persisted() {
    let temp = TempDir::new().expect("temp dir");
    let store = open_test_store(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");
    store
        .put_engagement(&engagement())
        .await
        .expect("insert draft");
    let relation = AssetRelation {
        id: "relation-1".to_string(),
        engagement_id: "eng-1".to_string(),
        source_asset_id: "workstation-1".to_string(),
        target_asset_id: "domain-controller-1".to_string(),
        kind: "domainMemberOf".to_string(),
        evidence_id: Some("evidence-1".to_string()),
        discovered_at: 2,
    };
    store
        .put_asset_relation(&relation)
        .await
        .expect("store relation");

    assert_eq!(
        store
            .asset_relations("eng-1")
            .await
            .expect("list relations"),
        vec![relation]
    );
}

#[tokio::test]
async fn invalid_engagement_transition_is_rejected() {
    let temp = TempDir::new().expect("temp dir");
    let store = open_test_store(&temp.path().join("state.sqlite"))
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

#[tokio::test]
async fn system_state_survives_store_reopen() {
    let temp = TempDir::new().expect("temp dir");
    let path = temp.path().join("state.sqlite");
    let cipher = test_record_cipher();
    let store = StateStore::open_with_cipher(&path, cipher.clone())
        .await
        .expect("state store");
    let expected = serde_json::json!({
        "state": "paused",
        "reason": "killSwitch",
        "updatedAt": 42,
    });
    store
        .put_system_state("daemonControl", &expected)
        .await
        .expect("put system state");
    drop(store);

    let reopened = StateStore::open_with_cipher(&path, cipher)
        .await
        .expect("reopen state store");
    assert_eq!(
        reopened
            .system_state::<serde_json::Value>("daemonControl")
            .await
            .expect("read system state"),
        Some(expected)
    );
}

#[tokio::test]
async fn encrypted_engagement_state_survives_store_reopen() {
    let temp = TempDir::new().expect("temp dir");
    let path = temp.path().join("state.sqlite");
    let keyring = codex_keyring_store::tests::MockKeyringStore::default();
    let store = StateStore::open_with_cipher(
        &path,
        Arc::new(KeyringEngagementCipher::new(keyring.clone())),
    )
    .await
    .expect("state store");
    let expected = engagement();
    store
        .put_engagement(&expected)
        .await
        .expect("store engagement");
    drop(store);

    let reopened =
        StateStore::open_with_cipher(&path, Arc::new(KeyringEngagementCipher::new(keyring)))
            .await
            .expect("reopen state store");

    assert_eq!(
        reopened
            .engagement(&expected.id)
            .await
            .expect("read engagement"),
        expected
    );
}

#[tokio::test]
async fn engagement_owned_payloads_are_encrypted_and_tampering_is_rejected() {
    let temp = TempDir::new().expect("temp dir");
    let store = open_test_store(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");
    let mut engagement = engagement();
    engagement.name = "plaintext-engagement-marker".to_string();
    store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let asset = Asset {
        id: "asset-1".to_string(),
        engagement_id: engagement.id.clone(),
        kind: "host".to_string(),
        value: "plaintext-asset-marker".to_string(),
        discovered_at: 2,
    };
    store.put_asset(&asset).await.expect("store asset");
    let conversation = ConversationEntryDraft {
        id: "message-1".to_string(),
        engagement_id: engagement.id.clone(),
        turn_id: Some("turn-1".to_string()),
        role: ConversationRole::Operator,
        kind: ConversationKind::Message,
        text: "plaintext-conversation-marker".to_string(),
        created_at: 3,
    };
    store
        .append_conversation_entry(&conversation)
        .await
        .expect("store conversation");

    for (query, marker) in [
        (
            "SELECT data FROM engagements LIMIT 1",
            "plaintext-engagement-marker",
        ),
        ("SELECT data FROM assets LIMIT 1", "plaintext-asset-marker"),
        (
            "SELECT data FROM conversation_entries LIMIT 1",
            "plaintext-conversation-marker",
        ),
    ] {
        let payload: Vec<u8> = sqlx::query_scalar(query)
            .fetch_one(&store.pool)
            .await
            .expect("raw encrypted payload");
        assert!(payload.starts_with(b"RXE1"));
        assert!(!String::from_utf8_lossy(&payload).contains(marker));
    }

    let mut tampered: Vec<u8> = sqlx::query_scalar("SELECT data FROM assets WHERE id = ?")
        .bind(&asset.id)
        .fetch_one(&store.pool)
        .await
        .expect("asset envelope");
    let last = tampered.len() - 1;
    tampered[last] ^= 1;
    sqlx::query("UPDATE assets SET data = ? WHERE id = ?")
        .bind(tampered)
        .bind(&asset.id)
        .execute(&store.pool)
        .await
        .expect("tamper asset envelope");

    assert!(matches!(
        store.assets(&engagement.id).await,
        Err(StateError::Crypto(CryptoError::AuthenticationFailed))
    ));
}
