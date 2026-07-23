use crate::AuditConfig;
use serde::Deserialize;
use serde::Serialize;
use std::path::PathBuf;
use std::sync::Arc;
use thiserror::Error;
use tokio::io::AsyncWriteExt;
use tokio::sync::Semaphore;

#[derive(Debug, Error)]
pub enum AuditError {
    #[error("failed to create audit directory: {0}")]
    CreateDirectory(#[source] std::io::Error),
    #[error("failed to append audit record: {0}")]
    Append(#[source] std::io::Error),
    #[error("failed to serialize audit record: {0}")]
    Serialize(#[from] serde_json::Error),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AuditRecord {
    pub timestamp: i64,
    pub event: String,
    pub engagement_id: String,
    pub thread_id: Option<String>,
    pub turn_id: Option<String>,
    pub tool_call_id: Option<String>,
    pub mode: Option<crate::ExecutionMode>,
    pub policy_revision: Option<String>,
    pub outcome: Option<String>,
}

#[derive(Clone)]
pub struct AuditWriter {
    path: PathBuf,
    fsync: bool,
    append_slot: Arc<Semaphore>,
}

impl AuditWriter {
    pub fn new(config: &AuditConfig) -> Self {
        Self {
            path: config.jsonl_path.clone(),
            fsync: config.fsync,
            append_slot: Arc::new(Semaphore::new(1)),
        }
    }

    pub async fn append(&self, record: &AuditRecord) -> Result<(), AuditError> {
        let _permit = self
            .append_slot
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| AuditError::Append(std::io::Error::other("audit writer is closed")))?;
        if let Some(parent) = self.path.parent()
            && !parent.as_os_str().is_empty()
        {
            tokio::fs::create_dir_all(parent)
                .await
                .map_err(AuditError::CreateDirectory)?;
        }
        let mut encoded = serde_json::to_vec(record)?;
        encoded.push(b'\n');
        let mut file = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .await
            .map_err(AuditError::Append)?;
        file.write_all(&encoded).await.map_err(AuditError::Append)?;
        if self.fsync {
            file.sync_data().await.map_err(AuditError::Append)?;
        }
        Ok(())
    }
}

#[cfg(test)]
#[path = "audit_tests.rs"]
mod tests;
