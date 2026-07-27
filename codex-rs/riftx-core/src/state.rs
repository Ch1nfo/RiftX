use crate::ApprovalRecord;
use crate::Artifact;
use crate::Asset;
use crate::AssetRelation;
use crate::AuditConfig;
use crate::AuditWriter;
use crate::AutoRun;
use crate::CredentialError;
use crate::CredentialGrant;
use crate::CredentialReference;
use crate::CredentialUseError;
use crate::Engagement;
use crate::EngagementStatus;
use crate::Evidence;
use crate::Finding;
use crate::Service;
use crate::TargetStateError;
use crate::Task;
use codex_riftx_crypto::CryptoError;
use codex_riftx_crypto::EngagementRecordCipher;
use codex_riftx_crypto::KeyringEngagementCipher;
use serde::Serialize;
use serde::de::DeserializeOwned;
use sqlx::Row;
use sqlx::SqlitePool;
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::sqlite::SqlitePoolOptions;
use std::path::Path;
use std::sync::Arc;
use thiserror::Error;
use zeroize::Zeroizing;

#[derive(Debug, Error)]
pub enum StateError {
    #[error(transparent)]
    Database(#[from] sqlx::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Crypto(#[from] CryptoError),
    #[error("engagement encryption task failed: {0}")]
    CryptoTask(String),
    #[error("engagement {0} was not found")]
    EngagementNotFound(String),
    #[error("engagement {id} cannot transition from {from:?} to {to:?}")]
    InvalidTransition {
        id: String,
        from: EngagementStatus,
        to: EngagementStatus,
    },
    #[error(transparent)]
    InvalidTargetState(#[from] TargetStateError),
    #[error(transparent)]
    InvalidCredential(#[from] CredentialError),
    #[error(transparent)]
    InvalidCredentialUse(#[from] CredentialUseError),
    #[error("credential grant {0} was not found")]
    CredentialGrantNotFound(String),
    #[error("credential reference {0} was not found")]
    CredentialReferenceNotFound(String),
    #[error("credential reference {0} has no configured secret")]
    CredentialSecretUnavailable(String),
    #[error("credential grant policy revision is stale")]
    CredentialPolicyRevisionMismatch,
    #[error("credential grant is not active")]
    CredentialGrantInactive,
    #[error("credential grant does not allow capability {0}")]
    CredentialCapabilityDenied(String),
    #[error("credential grant does not allow target {0}")]
    CredentialTargetDenied(String),
    #[error("credential grant use limit has been reached")]
    CredentialUseLimitExceeded,
    #[error("credential failure limit has been reached for this identity")]
    CredentialFailureLimitExceeded,
    #[error("another credential use is already active for this identity")]
    CredentialUseInProgress,
    #[error("credential use {0} was not found")]
    CredentialUseNotFound(String),
    #[error("credential use {id} cannot transition from {from:?} to {to:?}")]
    InvalidCredentialUseTransition {
        id: String,
        from: crate::CredentialUseStatus,
        to: crate::CredentialUseStatus,
    },
    #[error("invalid conversation entry: {0}")]
    InvalidConversationEntry(String),
    #[error("invalid conversation query: {0}")]
    InvalidConversationQuery(String),
    #[error("system state coordinator is unavailable")]
    SystemStateUnavailable,
    #[error("security audit is unavailable")]
    AuditUnavailable,
    #[error("{entity_kind} {entity_id} is missing required {reference_kind} reference")]
    MissingChainReference {
        entity_kind: &'static str,
        entity_id: String,
        reference_kind: &'static str,
    },
    #[error("{entity_kind} {entity_id} references unknown {reference_kind} {reference_id}")]
    BrokenChainReference {
        entity_kind: &'static str,
        entity_id: String,
        reference_kind: &'static str,
        reference_id: String,
    },
}

#[derive(Clone)]
pub struct StateStore {
    pool: SqlitePool,
    cipher: Arc<dyn EngagementRecordCipher>,
}

macro_rules! entity_tables {
    ($($variant:ident => $table:literal),+ $(,)?) => {
        #[derive(Clone, Copy)]
        enum EntityTable {
            $($variant),+
        }

        impl EntityTable {
            fn name(self) -> &'static str {
                match self {
                    $(Self::$variant => $table),+
                }
            }

            fn create_sql(self) -> &'static str {
                match self {
                    $(Self::$variant => concat!(
                        "CREATE TABLE IF NOT EXISTS ",
                        $table,
                        " (id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL, data BLOB NOT NULL, FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE)"
                    )),+
                }
            }

            fn upsert_sql(self) -> &'static str {
                match self {
                    $(Self::$variant => concat!(
                        "INSERT INTO ",
                        $table,
                        "(id, engagement_id, data) VALUES(?, ?, ?) ON CONFLICT(id) DO UPDATE SET engagement_id=excluded.engagement_id, data=excluded.data"
                    )),+
                }
            }

            fn list_sql(self) -> &'static str {
                match self {
                    $(Self::$variant => concat!(
                        "SELECT id, data FROM ",
                        $table,
                        " WHERE engagement_id = ? ORDER BY id"
                    )),+
                }
            }

            fn get_sql(self) -> &'static str {
                match self {
                    $(Self::$variant => concat!(
                        "SELECT data FROM ",
                        $table,
                        " WHERE engagement_id = ? AND id = ?"
                    )),+
                }
            }

            fn delete_sql(self) -> &'static str {
                match self {
                    $(Self::$variant => concat!(
                        "DELETE FROM ",
                        $table,
                        " WHERE engagement_id = ? AND id = ?"
                    )),+
                }
            }
        }
    };
}

entity_tables!(
    Assets => "assets",
    AssetRelations => "asset_relations",
    Services => "services",
    Identities => "identities",
    Observations => "observations",
    Hypotheses => "hypotheses",
    TestCases => "test_cases",
    Executions => "executions",
    Findings => "findings",
    Evidence => "evidence",
    AttackPaths => "attack_paths",
    Coverage => "coverage",
    CredentialReferences => "credential_references",
    CredentialGrants => "credential_grants",
    Tasks => "tasks",
    Artifacts => "artifacts",
    AutoRuns => "auto_runs",
    Approvals => "approvals",
);

impl StateStore {
    pub async fn open(path: &Path) -> Result<Self, StateError> {
        Self::open_with_cipher(path, Arc::new(KeyringEngagementCipher::default())).await
    }

    pub async fn open_with_cipher(
        path: &Path,
        cipher: Arc<dyn EngagementRecordCipher>,
    ) -> Result<Self, StateError> {
        let options = SqliteConnectOptions::new()
            .filename(path)
            .create_if_missing(true)
            .foreign_keys(true);
        let pool = SqlitePoolOptions::new()
            .max_connections(5)
            .connect_with(options)
            .await?;
        let store = Self { pool, cipher };
        store.initialize().await?;
        store.prepare_existing_engagements().await?;
        Ok(store)
    }

    /// Creates an audit writer that shares this store's prepared engagement data keys.
    pub fn audit_writer(&self, config: &AuditConfig) -> AuditWriter {
        AuditWriter::new(config, self.cipher.clone())
    }

    /// Returns an opaque cipher handle for other engagement-owned encrypted stores.
    pub fn record_cipher(&self) -> Arc<dyn EngagementRecordCipher> {
        self.cipher.clone()
    }

    async fn initialize(&self) -> Result<(), StateError> {
        sqlx::query(
            "CREATE TABLE IF NOT EXISTS engagements (id TEXT PRIMARY KEY, data BLOB NOT NULL)",
        )
        .execute(&self.pool)
        .await?;
        sqlx::query(
            "CREATE TABLE IF NOT EXISTS conversation_entries (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL,
                engagement_id TEXT NOT NULL,
                data BLOB NOT NULL,
                UNIQUE(engagement_id, id),
                FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE
            )",
        )
        .execute(&self.pool)
        .await?;
        sqlx::query(
            "CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                data TEXT NOT NULL
            )",
        )
        .execute(&self.pool)
        .await?;
        sqlx::query(
            "CREATE INDEX IF NOT EXISTS conversation_entries_engagement_sequence
             ON conversation_entries(engagement_id, sequence)",
        )
        .execute(&self.pool)
        .await?;
        sqlx::query(
            "CREATE TABLE IF NOT EXISTS credential_grant_uses (
                id TEXT PRIMARY KEY,
                engagement_id TEXT NOT NULL,
                grant_id TEXT NOT NULL,
                credential_id TEXT NOT NULL,
                identity_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                completed_at INTEGER,
                data BLOB NOT NULL,
                FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE
            )",
        )
        .execute(&self.pool)
        .await?;
        sqlx::query(
            "CREATE INDEX IF NOT EXISTS credential_grant_uses_limits
             ON credential_grant_uses(grant_id, identity_hash, status)",
        )
        .execute(&self.pool)
        .await?;
        for table in [
            EntityTable::Assets,
            EntityTable::AssetRelations,
            EntityTable::Services,
            EntityTable::Identities,
            EntityTable::Observations,
            EntityTable::Hypotheses,
            EntityTable::TestCases,
            EntityTable::Executions,
            EntityTable::Findings,
            EntityTable::Evidence,
            EntityTable::AttackPaths,
            EntityTable::Coverage,
            EntityTable::CredentialReferences,
            EntityTable::CredentialGrants,
            EntityTable::Tasks,
            EntityTable::Artifacts,
            EntityTable::AutoRuns,
            EntityTable::Approvals,
        ] {
            sqlx::query(table.create_sql()).execute(&self.pool).await?;
        }
        Ok(())
    }

    pub async fn put_engagement(&self, engagement: &Engagement) -> Result<(), StateError> {
        let exists: bool =
            sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM engagements WHERE id = ?)")
                .bind(&engagement.id)
                .fetch_one(&self.pool)
                .await?;
        let created_key = !exists;
        if created_key {
            self.create_engagement_key(&engagement.id).await?;
        }
        let data = match self
            .seal_value("engagements", &engagement.id, &engagement.id, engagement)
            .await
        {
            Ok(data) => data,
            Err(error) => {
                self.cleanup_new_key(created_key, &engagement.id).await;
                return Err(error);
            }
        };
        let result = sqlx::query(
            "INSERT INTO engagements(id, data) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
        )
        .bind(&engagement.id)
        .bind(data)
        .execute(&self.pool)
        .await;
        if let Err(error) = result {
            self.cleanup_new_key(created_key, &engagement.id).await;
            return Err(error.into());
        }
        Ok(())
    }

    pub async fn engagement(&self, id: &str) -> Result<Engagement, StateError> {
        let row = sqlx::query("SELECT data FROM engagements WHERE id = ?")
            .bind(id)
            .fetch_optional(&self.pool)
            .await?
            .ok_or_else(|| StateError::EngagementNotFound(id.to_string()))?;
        self.open_value("engagements", id, id, row.try_get::<Vec<u8>, _>("data")?)
            .await
    }

    pub async fn engagements(&self) -> Result<Vec<Engagement>, StateError> {
        let rows = sqlx::query("SELECT id, data FROM engagements ORDER BY id")
            .fetch_all(&self.pool)
            .await?;
        let mut engagements = Vec::with_capacity(rows.len());
        for row in rows {
            let id: String = row.try_get("id")?;
            engagements.push(
                self.open_value("engagements", &id, &id, row.try_get::<Vec<u8>, _>("data")?)
                    .await?,
            );
        }
        Ok(engagements)
    }

    pub async fn system_state<T: DeserializeOwned>(
        &self,
        key: &str,
    ) -> Result<Option<T>, StateError> {
        let row = sqlx::query("SELECT data FROM system_state WHERE key = ?")
            .bind(key)
            .fetch_optional(&self.pool)
            .await?;
        row.map(|row| serde_json::from_str(row.get("data")))
            .transpose()
            .map_err(StateError::from)
    }

    pub async fn put_system_state<T: Serialize>(
        &self,
        key: &str,
        value: &T,
    ) -> Result<(), StateError> {
        sqlx::query(
            "INSERT INTO system_state(key, data) VALUES(?, ?)
             ON CONFLICT(key) DO UPDATE SET data=excluded.data",
        )
        .bind(key)
        .bind(serde_json::to_string(value)?)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn transition_engagement(
        &self,
        id: &str,
        to: EngagementStatus,
        updated_at: i64,
    ) -> Result<Engagement, StateError> {
        let mut engagement = self.engagement(id).await?;
        let valid = matches!(
            (engagement.status, to),
            (EngagementStatus::Draft, EngagementStatus::Active)
                | (EngagementStatus::Interrupted, EngagementStatus::Active)
                | (EngagementStatus::Active, EngagementStatus::Interrupted)
                | (EngagementStatus::Draft, EngagementStatus::Expired)
                | (EngagementStatus::Interrupted, EngagementStatus::Expired)
                | (EngagementStatus::Active, EngagementStatus::Expired)
                | (EngagementStatus::Active, EngagementStatus::Completed)
        );
        if !valid {
            return Err(StateError::InvalidTransition {
                id: id.to_string(),
                from: engagement.status,
                to,
            });
        }
        engagement.status = to;
        engagement.updated_at = updated_at;
        self.put_engagement(&engagement).await?;
        Ok(engagement)
    }

    pub async fn put_asset(&self, value: &Asset) -> Result<(), StateError> {
        self.put_entity(EntityTable::Assets, &value.id, &value.engagement_id, value)
            .await
    }

    pub async fn put_asset_relation(&self, value: &AssetRelation) -> Result<(), StateError> {
        self.put_entity(
            EntityTable::AssetRelations,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn put_service(&self, value: &Service) -> Result<(), StateError> {
        self.put_entity(
            EntityTable::Services,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn put_finding(&self, value: &Finding) -> Result<(), StateError> {
        self.put_entity(
            EntityTable::Findings,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn put_evidence(&self, value: &Evidence) -> Result<(), StateError> {
        self.put_entity(
            EntityTable::Evidence,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn put_task(&self, value: &Task) -> Result<(), StateError> {
        self.put_entity(EntityTable::Tasks, &value.id, &value.engagement_id, value)
            .await
    }

    pub async fn task_for_turn(
        &self,
        engagement_id: &str,
        turn_id: &str,
    ) -> Result<Option<Task>, StateError> {
        let tasks: Vec<Task> = self.entities(EntityTable::Tasks, engagement_id).await?;
        Ok(tasks
            .into_iter()
            .find(|task| task.turn_id.as_deref() == Some(turn_id)))
    }

    pub async fn put_artifact(&self, value: &Artifact) -> Result<(), StateError> {
        self.put_entity(
            EntityTable::Artifacts,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn put_auto_run(&self, value: &AutoRun) -> Result<(), StateError> {
        self.put_entity(
            EntityTable::AutoRuns,
            &value.engagement_id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn auto_run(&self, engagement_id: &str) -> Result<Option<AutoRun>, StateError> {
        self.entity(EntityTable::AutoRuns, engagement_id, engagement_id)
            .await
    }

    pub async fn put_approval(&self, value: &ApprovalRecord) -> Result<(), StateError> {
        self.put_entity(
            EntityTable::Approvals,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn approval(
        &self,
        engagement_id: &str,
        approval_id: &str,
    ) -> Result<Option<ApprovalRecord>, StateError> {
        self.entity(EntityTable::Approvals, engagement_id, approval_id)
            .await
    }

    pub async fn approvals(&self, engagement_id: &str) -> Result<Vec<ApprovalRecord>, StateError> {
        self.entities(EntityTable::Approvals, engagement_id).await
    }

    pub async fn findings(&self, engagement_id: &str) -> Result<Vec<Finding>, StateError> {
        self.entities(EntityTable::Findings, engagement_id).await
    }

    pub async fn assets(&self, engagement_id: &str) -> Result<Vec<Asset>, StateError> {
        self.entities(EntityTable::Assets, engagement_id).await
    }

    pub async fn asset_relations(
        &self,
        engagement_id: &str,
    ) -> Result<Vec<AssetRelation>, StateError> {
        self.entities(EntityTable::AssetRelations, engagement_id)
            .await
    }

    pub async fn services(&self, engagement_id: &str) -> Result<Vec<Service>, StateError> {
        self.entities(EntityTable::Services, engagement_id).await
    }

    pub async fn evidence(&self, engagement_id: &str) -> Result<Vec<Evidence>, StateError> {
        self.entities(EntityTable::Evidence, engagement_id).await
    }

    pub async fn tasks(&self, engagement_id: &str) -> Result<Vec<Task>, StateError> {
        self.entities(EntityTable::Tasks, engagement_id).await
    }

    pub async fn artifacts(&self, engagement_id: &str) -> Result<Vec<Artifact>, StateError> {
        self.entities(EntityTable::Artifacts, engagement_id).await
    }

    async fn put_entity<T: Serialize>(
        &self,
        table: EntityTable,
        id: &str,
        engagement_id: &str,
        value: &T,
    ) -> Result<(), StateError> {
        let data = self
            .seal_value(table.name(), engagement_id, id, value)
            .await?;
        sqlx::query(table.upsert_sql())
            .bind(id)
            .bind(engagement_id)
            .bind(data)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    async fn entities<T: DeserializeOwned>(
        &self,
        table: EntityTable,
        engagement_id: &str,
    ) -> Result<Vec<T>, StateError> {
        let rows = sqlx::query(table.list_sql())
            .bind(engagement_id)
            .fetch_all(&self.pool)
            .await?;
        let mut values = Vec::with_capacity(rows.len());
        for row in rows {
            let id: String = row.try_get("id")?;
            values.push(
                self.open_value(
                    table.name(),
                    engagement_id,
                    &id,
                    row.try_get::<Vec<u8>, _>("data")?,
                )
                .await?,
            );
        }
        Ok(values)
    }

    async fn entity<T: DeserializeOwned>(
        &self,
        table: EntityTable,
        engagement_id: &str,
        id: &str,
    ) -> Result<Option<T>, StateError> {
        let row = sqlx::query(table.get_sql())
            .bind(engagement_id)
            .bind(id)
            .fetch_optional(&self.pool)
            .await?;
        match row {
            Some(row) => self
                .open_value(
                    table.name(),
                    engagement_id,
                    id,
                    row.try_get::<Vec<u8>, _>("data")?,
                )
                .await
                .map(Some),
            None => Ok(None),
        }
    }

    async fn delete_entity(
        &self,
        table: EntityTable,
        engagement_id: &str,
        id: &str,
    ) -> Result<bool, StateError> {
        Ok(sqlx::query(table.delete_sql())
            .bind(engagement_id)
            .bind(id)
            .execute(&self.pool)
            .await?
            .rows_affected()
            > 0)
    }

    async fn prepare_existing_engagements(&self) -> Result<(), StateError> {
        let ids = sqlx::query_scalar::<_, String>("SELECT id FROM engagements ORDER BY id")
            .fetch_all(&self.pool)
            .await?;
        for id in ids {
            let cipher = self.cipher.clone();
            tokio::task::spawn_blocking(move || cipher.prepare_engagement(&id))
                .await
                .map_err(|error| StateError::CryptoTask(error.to_string()))??;
        }
        Ok(())
    }

    async fn create_engagement_key(&self, engagement_id: &str) -> Result<(), StateError> {
        let cipher = self.cipher.clone();
        let engagement_id = engagement_id.to_string();
        tokio::task::spawn_blocking(move || cipher.create_engagement(&engagement_id))
            .await
            .map_err(|error| StateError::CryptoTask(error.to_string()))??;
        Ok(())
    }

    async fn cleanup_new_key(&self, created: bool, engagement_id: &str) {
        if !created {
            return;
        }
        let cipher = self.cipher.clone();
        let engagement_id = engagement_id.to_string();
        let _ = tokio::task::spawn_blocking(move || cipher.delete_engagement(&engagement_id)).await;
    }

    async fn seal_value<T: Serialize>(
        &self,
        record_kind: &str,
        engagement_id: &str,
        record_id: &str,
        value: &T,
    ) -> Result<Vec<u8>, StateError> {
        let plaintext = Zeroizing::new(serde_json::to_vec(value)?);
        let cipher = self.cipher.clone();
        let engagement_id = engagement_id.to_string();
        let record_kind = record_kind.to_string();
        let record_id = record_id.to_string();
        Ok(tokio::task::spawn_blocking(move || {
            cipher.seal_record(&engagement_id, &record_kind, &record_id, &plaintext)
        })
        .await
        .map_err(|error| StateError::CryptoTask(error.to_string()))??)
    }

    async fn open_value<T: DeserializeOwned>(
        &self,
        record_kind: &str,
        engagement_id: &str,
        record_id: &str,
        envelope: Vec<u8>,
    ) -> Result<T, StateError> {
        let cipher = self.cipher.clone();
        let engagement_id = engagement_id.to_string();
        let record_kind = record_kind.to_string();
        let record_id = record_id.to_string();
        let plaintext = tokio::task::spawn_blocking(move || {
            cipher.open_record(&engagement_id, &record_kind, &record_id, &envelope)
        })
        .await
        .map_err(|error| StateError::CryptoTask(error.to_string()))??;
        Ok(serde_json::from_slice(&plaintext)?)
    }
}

#[cfg(test)]
pub(crate) fn test_record_cipher() -> Arc<dyn EngagementRecordCipher> {
    Arc::new(KeyringEngagementCipher::new(
        codex_keyring_store::tests::MockKeyringStore::default(),
    ))
}

#[cfg(test)]
pub(crate) async fn open_test_store(path: &Path) -> Result<StateStore, StateError> {
    StateStore::open_with_cipher(path, test_record_cipher()).await
}

#[path = "state_target.rs"]
mod target;

#[path = "state_snapshot.rs"]
mod snapshot;

#[path = "state_credential.rs"]
mod credential;

#[path = "state_credential_use.rs"]
mod credential_use;

#[path = "state_conversation.rs"]
mod conversation;

#[cfg(test)]
#[path = "state_tests.rs"]
mod tests;
