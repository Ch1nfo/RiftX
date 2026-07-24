use crate::AuditConfig;
use base64::Engine;
use base64::engine::general_purpose::STANDARD_NO_PAD;
use codex_riftx_crypto::CryptoError;
use codex_riftx_crypto::EngagementRecordCipher;
use serde::Deserialize;
use serde::Serialize;
use std::path::PathBuf;
use std::sync::Arc;
use thiserror::Error;
use tokio::io::AsyncBufReadExt;
use tokio::io::AsyncWriteExt;
use tokio::sync::Semaphore;
use uuid::Uuid;
use zeroize::Zeroizing;

const AUDIT_RECORD_KIND: &str = "audit_records";
const SYSTEM_AUDIT_ID: &str = "system";
const ENCRYPTED_AUDIT_FORMAT: &str = "riftxAuditEncryptedV1";
const MAX_AUDIT_LINE_BYTES: usize = 24 * 1024 * 1024;
const MAX_AUDIT_READ_RECORDS: u32 = 10_000;

#[derive(Debug, Error)]
pub enum AuditError {
    #[error("failed to create audit directory: {0}")]
    CreateDirectory(#[source] std::io::Error),
    #[error("failed to append audit record: {0}")]
    Append(#[source] std::io::Error),
    #[error("failed to read audit records: {0}")]
    Read(#[source] std::io::Error),
    #[error("failed to serialize audit record: {0}")]
    Serialize(#[from] serde_json::Error),
    #[error("failed to decode encrypted audit record: {0}")]
    Decode(#[from] base64::DecodeError),
    #[error(transparent)]
    Crypto(#[from] CryptoError),
    #[error("audit encryption task failed: {0}")]
    CryptoTask(String),
    #[error("audit record uses an unsupported encrypted format")]
    UnsupportedFormat,
    #[error("engagement audit record is not encrypted")]
    UnencryptedEngagementRecord,
    #[error("audit line exceeds the {MAX_AUDIT_LINE_BYTES}-byte limit")]
    LineTooLarge,
    #[error("audit read limit must be between 1 and {MAX_AUDIT_READ_RECORDS}")]
    InvalidReadLimit,
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
    pub details: Option<serde_json::Value>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct EncryptedAuditLine {
    format: String,
    engagement_id: String,
    record_id: String,
    envelope: String,
}

#[derive(Clone)]
pub struct AuditWriter {
    path: PathBuf,
    fsync: bool,
    cipher: Arc<dyn EngagementRecordCipher>,
    append_slot: Arc<Semaphore>,
}

impl AuditWriter {
    pub(crate) fn new(config: &AuditConfig, cipher: Arc<dyn EngagementRecordCipher>) -> Self {
        Self {
            path: config.jsonl_path.clone(),
            fsync: config.fsync,
            cipher,
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
        let mut encoded = self.encode_record(record).await?;
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

    /// Reads and authenticates up to `limit` records from the start of the audit log.
    ///
    /// Engagement records must use the encrypted format. The reserved `system` records contain
    /// only privacy-safe daemon control metadata and remain plain JSON for recovery diagnostics.
    pub async fn read_records(&self, limit: u32) -> Result<Vec<AuditRecord>, AuditError> {
        if limit == 0 || limit > MAX_AUDIT_READ_RECORDS {
            return Err(AuditError::InvalidReadLimit);
        }
        let file = match tokio::fs::File::open(&self.path).await {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(AuditError::Read(error)),
        };
        let mut reader = tokio::io::BufReader::new(file);
        let mut line = Vec::new();
        let mut records = Vec::new();
        while records.len() < limit as usize {
            line.clear();
            let count = reader
                .read_until(b'\n', &mut line)
                .await
                .map_err(AuditError::Read)?;
            if count == 0 {
                break;
            }
            if line.len() > MAX_AUDIT_LINE_BYTES {
                return Err(AuditError::LineTooLarge);
            }
            while matches!(line.last(), Some(b'\n' | b'\r')) {
                line.pop();
            }
            if line.is_empty() {
                continue;
            }
            records.push(self.decode_record(&line).await?);
        }
        Ok(records)
    }

    async fn encode_record(&self, record: &AuditRecord) -> Result<Vec<u8>, AuditError> {
        if record.engagement_id == SYSTEM_AUDIT_ID {
            return Ok(serde_json::to_vec(record)?);
        }
        let record_id = Uuid::new_v4().to_string();
        let plaintext = Zeroizing::new(serde_json::to_vec(record)?);
        let cipher = self.cipher.clone();
        let engagement_id = record.engagement_id.clone();
        let encryption_id = record_id.clone();
        let envelope = tokio::task::spawn_blocking(move || {
            cipher.seal_record(
                &engagement_id,
                AUDIT_RECORD_KIND,
                &encryption_id,
                &plaintext,
            )
        })
        .await
        .map_err(|error| AuditError::CryptoTask(error.to_string()))??;
        Ok(serde_json::to_vec(&EncryptedAuditLine {
            format: ENCRYPTED_AUDIT_FORMAT.to_string(),
            engagement_id: record.engagement_id.clone(),
            record_id,
            envelope: STANDARD_NO_PAD.encode(envelope),
        })?)
    }

    async fn decode_record(&self, line: &[u8]) -> Result<AuditRecord, AuditError> {
        if let Ok(record) = serde_json::from_slice::<AuditRecord>(line) {
            return if record.engagement_id == SYSTEM_AUDIT_ID {
                Ok(record)
            } else {
                Err(AuditError::UnencryptedEngagementRecord)
            };
        }
        let stored: EncryptedAuditLine = serde_json::from_slice(line)?;
        if stored.format != ENCRYPTED_AUDIT_FORMAT {
            return Err(AuditError::UnsupportedFormat);
        }
        let envelope = STANDARD_NO_PAD.decode(stored.envelope)?;
        let cipher = self.cipher.clone();
        let plaintext = tokio::task::spawn_blocking(move || {
            cipher.open_record(
                &stored.engagement_id,
                AUDIT_RECORD_KIND,
                &stored.record_id,
                &envelope,
            )
        })
        .await
        .map_err(|error| AuditError::CryptoTask(error.to_string()))??;
        Ok(serde_json::from_slice(&plaintext)?)
    }
}

#[cfg(test)]
#[path = "audit_tests.rs"]
mod tests;
