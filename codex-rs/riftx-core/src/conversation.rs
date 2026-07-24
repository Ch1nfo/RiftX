use serde::Deserialize;
use serde::Serialize;

pub const MAX_CONVERSATION_ENTRY_BYTES: usize = 64 * 1024;
pub const MAX_CONVERSATION_PAGE_SIZE: u32 = 200;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ConversationRole {
    Operator,
    Agent,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ConversationKind {
    Message,
    Plan,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ConversationEntryDraft {
    pub id: String,
    pub engagement_id: String,
    pub turn_id: Option<String>,
    pub role: ConversationRole,
    pub kind: ConversationKind,
    pub text: String,
    pub created_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ConversationEntry {
    pub sequence: i64,
    pub id: String,
    pub engagement_id: String,
    pub turn_id: Option<String>,
    pub role: ConversationRole,
    pub kind: ConversationKind,
    pub text: String,
    pub created_at: i64,
}

impl ConversationEntry {
    pub(crate) fn from_draft(sequence: i64, draft: ConversationEntryDraft) -> Self {
        Self {
            sequence,
            id: draft.id,
            engagement_id: draft.engagement_id,
            turn_id: draft.turn_id,
            role: draft.role,
            kind: draft.kind,
            text: draft.text,
            created_at: draft.created_at,
        }
    }
}
