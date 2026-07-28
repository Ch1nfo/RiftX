use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use codex_riftx_app_server_adapter::ItemCompletedNotification;
use codex_riftx_app_server_adapter::ServerNotification;
use codex_riftx_app_server_adapter::ThreadItem;
use codex_riftx_core::ConversationEntryDraft;
use codex_riftx_core::ConversationKind;
use codex_riftx_core::ConversationRole;
use codex_riftx_core::MAX_CONVERSATION_ENTRY_BYTES;
use serde_json::json;

const TRUNCATION_SUFFIX: &str = "\n[truncated]";

pub(crate) async fn process_notification(state: &GatewayState, notification: &ServerNotification) {
    let ServerNotification::ItemCompleted(payload) = notification else {
        return;
    };
    persist_completed_item(state, payload).await;
}

async fn persist_completed_item(state: &GatewayState, payload: &ItemCompletedNotification) {
    let Some(engagement_id) = state
        .thread_engagements
        .read()
        .await
        .get(&payload.thread_id)
        .cloned()
    else {
        return;
    };
    let (id, kind, text) = match &payload.item {
        ThreadItem::AgentMessage { id, text, .. } => (id, ConversationKind::Message, text.as_str()),
        ThreadItem::Plan { id, text } => (id, ConversationKind::Plan, text.as_str()),
        _ => return,
    };
    if text.trim().is_empty() {
        return;
    }
    let draft = ConversationEntryDraft {
        id: id.clone(),
        engagement_id: engagement_id.clone(),
        turn_id: Some(payload.turn_id.clone()),
        role: ConversationRole::Agent,
        kind,
        text: bounded_text(text),
        created_at: unix_timestamp(),
    };
    if state.store.append_conversation_entry(&draft).await.is_err() {
        state
            .publish(
                &engagement_id,
                "conversation/persistenceFailed",
                json!({"itemId": id, "outcome": "failure"}),
            )
            .await;
    }
}

fn bounded_text(text: &str) -> String {
    if text.len() <= MAX_CONVERSATION_ENTRY_BYTES {
        return text.to_string();
    }
    let max_body_bytes = MAX_CONVERSATION_ENTRY_BYTES.saturating_sub(TRUNCATION_SUFFIX.len());
    let mut end = max_body_bytes;
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}{TRUNCATION_SUFFIX}", &text[..end])
}

#[cfg(test)]
#[path = "conversation_tests.rs"]
mod tests;
