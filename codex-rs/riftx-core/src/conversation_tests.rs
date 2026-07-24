use crate::AssessmentObjective;
use crate::AuthorizationScope;
use crate::AuthorizationWindow;
use crate::ConversationEntryDraft;
use crate::ConversationKind;
use crate::ConversationRole;
use crate::Engagement;
use crate::EngagementStatus;
use crate::EnvironmentClass;
use crate::ExecutionMode;
use crate::Scope;
use crate::StateStore;
use pretty_assertions::assert_eq;
use tempfile::TempDir;

#[tokio::test]
async fn conversation_history_is_ordered_deduplicated_and_paginated() {
    let temp = TempDir::new().expect("temp dir");
    let store = StateStore::open(&temp.path().join("state.sqlite"))
        .await
        .expect("state store");
    store
        .put_engagement(&engagement())
        .await
        .expect("insert engagement");
    let operator = draft(
        "operator-1",
        ConversationRole::Operator,
        ConversationKind::Message,
        "Inspect the authorized target.",
        10,
    );
    let agent = draft(
        "agent-1",
        ConversationRole::Agent,
        ConversationKind::Message,
        "I will begin with local reconnaissance.",
        11,
    );
    let stored_operator = store
        .append_conversation_entry(&operator)
        .await
        .expect("append operator message");
    let stored_agent = store
        .append_conversation_entry(&agent)
        .await
        .expect("append agent message");
    assert_eq!(
        store
            .append_conversation_entry(&operator)
            .await
            .expect("deduplicate operator message"),
        stored_operator
    );

    assert_eq!(
        store
            .conversation_entries_before("eng-1", None, 1)
            .await
            .expect("latest page"),
        vec![stored_agent.clone()]
    );
    assert_eq!(
        store
            .conversation_entries_before("eng-1", Some(stored_agent.sequence), 1)
            .await
            .expect("older page"),
        vec![stored_operator.clone()]
    );
    assert_eq!(
        store
            .conversation_entries_before("eng-1", None, 2)
            .await
            .expect("full page"),
        vec![stored_operator, stored_agent]
    );
}

fn draft(
    id: &str,
    role: ConversationRole,
    kind: ConversationKind,
    text: &str,
    created_at: i64,
) -> ConversationEntryDraft {
    ConversationEntryDraft {
        id: id.to_string(),
        engagement_id: "eng-1".to_string(),
        turn_id: Some("turn-1".to_string()),
        role,
        kind,
        text: text.to_string(),
        created_at,
    }
}

fn engagement() -> Engagement {
    Engagement {
        id: "eng-1".to_string(),
        name: "Conversation lab".to_string(),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Preserve operator and agent messages".to_string(),
            success_criteria: Vec::new(),
            structured_criteria: Vec::new(),
        },
        entry_points: vec!["127.0.0.1".to_string()],
        mode: ExecutionMode::Native,
        llm_profile: "default".to_string(),
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: vec!["127.0.0.0/8".parse().expect("CIDR")],
                domains: Vec::new(),
                ports: Vec::new(),
            },
            identities: Vec::new(),
            capabilities: Vec::new(),
            environment: EnvironmentClass::Lab,
            window: AuthorizationWindow {
                starts_at: None,
                expires_at: None,
            },
        },
        policy_revision: "revision-1".to_string(),
        thread_id: None,
        created_at: 1,
        updated_at: 1,
    }
}
