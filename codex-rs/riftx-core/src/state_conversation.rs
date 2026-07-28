use super::StateError;
use super::StateStore;
use crate::ConversationEntry;
use crate::ConversationEntryDraft;
use crate::MAX_CONVERSATION_ENTRY_BYTES;
use crate::MAX_CONVERSATION_PAGE_SIZE;
use sqlx::Row;

impl StateStore {
    pub async fn append_conversation_entry(
        &self,
        draft: &ConversationEntryDraft,
    ) -> Result<ConversationEntry, StateError> {
        validate_draft(draft)?;
        self.engagement(&draft.engagement_id).await?;
        let data = self
            .seal_value(
                "conversation_entries",
                &draft.engagement_id,
                &draft.id,
                draft,
            )
            .await?;
        sqlx::query(
            "INSERT OR IGNORE INTO conversation_entries(id, engagement_id, data) VALUES(?, ?, ?)",
        )
        .bind(&draft.id)
        .bind(&draft.engagement_id)
        .bind(data)
        .execute(&self.pool)
        .await?;
        let row = sqlx::query(
            "SELECT sequence, id, data FROM conversation_entries WHERE engagement_id = ? AND id = ?",
        )
        .bind(&draft.engagement_id)
        .bind(&draft.id)
        .fetch_one(&self.pool)
        .await?;
        let stored = self
            .open_value(
                "conversation_entries",
                &draft.engagement_id,
                row.try_get("id")?,
                row.try_get::<Vec<u8>, _>("data")?,
            )
            .await?;
        Ok(ConversationEntry::from_draft(
            row.try_get("sequence")?,
            stored,
        ))
    }

    pub async fn conversation_entries_before(
        &self,
        engagement_id: &str,
        before_sequence: Option<i64>,
        limit: u32,
    ) -> Result<Vec<ConversationEntry>, StateError> {
        self.engagement(engagement_id).await?;
        if limit == 0 || limit > MAX_CONVERSATION_PAGE_SIZE {
            return Err(StateError::InvalidConversationQuery(format!(
                "conversation page size must be between 1 and {MAX_CONVERSATION_PAGE_SIZE}"
            )));
        }
        let before_sequence = before_sequence.unwrap_or(i64::MAX);
        if before_sequence <= 0 {
            return Err(StateError::InvalidConversationQuery(
                "conversation cursor must be a positive sequence".to_string(),
            ));
        }
        let rows = sqlx::query(
            "SELECT sequence, id, data FROM conversation_entries WHERE engagement_id = ? AND sequence < ? ORDER BY sequence DESC LIMIT ?",
        )
        .bind(engagement_id)
        .bind(before_sequence)
        .bind(i64::from(limit))
        .fetch_all(&self.pool)
        .await?;
        let mut entries = Vec::with_capacity(rows.len());
        for row in rows {
            let draft = self
                .open_value(
                    "conversation_entries",
                    engagement_id,
                    row.try_get("id")?,
                    row.try_get::<Vec<u8>, _>("data")?,
                )
                .await?;
            entries.push(ConversationEntry::from_draft(
                row.try_get("sequence")?,
                draft,
            ));
        }
        entries.reverse();
        Ok(entries)
    }
}

fn validate_draft(draft: &ConversationEntryDraft) -> Result<(), StateError> {
    if draft.id.is_empty() || draft.id.len() > 128 {
        return Err(StateError::InvalidConversationEntry(
            "conversation entry ID must be between 1 and 128 bytes".to_string(),
        ));
    }
    if draft.text.trim().is_empty() {
        return Err(StateError::InvalidConversationEntry(
            "conversation entry text cannot be empty".to_string(),
        ));
    }
    if draft.text.len() > MAX_CONVERSATION_ENTRY_BYTES {
        return Err(StateError::InvalidConversationEntry(format!(
            "conversation entry exceeds the {MAX_CONVERSATION_ENTRY_BYTES}-byte limit"
        )));
    }
    Ok(())
}
