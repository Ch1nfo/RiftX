use crate::Artifact;
use crate::Asset;
use crate::AssetRelation;
use crate::Engagement;
use crate::EngagementStatus;
use crate::Evidence;
use crate::Finding;
use crate::Service;
use crate::TargetStateError;
use crate::Task;
use serde::Serialize;
use serde::de::DeserializeOwned;
use sqlx::Row;
use sqlx::SqlitePool;
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::sqlite::SqlitePoolOptions;
use std::path::Path;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum StateError {
    #[error(transparent)]
    Database(#[from] sqlx::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
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
    #[error("invalid conversation entry: {0}")]
    InvalidConversationEntry(String),
    #[error("invalid conversation query: {0}")]
    InvalidConversationQuery(String),
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
}

macro_rules! entity_tables {
    ($($variant:ident => $table:literal),+ $(,)?) => {
        #[derive(Clone, Copy)]
        enum EntityTable {
            $($variant),+
        }

        impl EntityTable {
            fn create_sql(self) -> &'static str {
                match self {
                    $(Self::$variant => concat!(
                        "CREATE TABLE IF NOT EXISTS ",
                        $table,
                        " (id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL, data TEXT NOT NULL, FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE)"
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
                        "SELECT data FROM ",
                        $table,
                        " WHERE engagement_id = ? ORDER BY id"
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
    Tasks => "tasks",
    Artifacts => "artifacts",
);

impl StateStore {
    pub async fn open(path: &Path) -> Result<Self, StateError> {
        let options = SqliteConnectOptions::new()
            .filename(path)
            .create_if_missing(true)
            .foreign_keys(true);
        let pool = SqlitePoolOptions::new()
            .max_connections(5)
            .connect_with(options)
            .await?;
        let store = Self { pool };
        store.initialize().await?;
        Ok(store)
    }

    async fn initialize(&self) -> Result<(), StateError> {
        sqlx::query(
            "CREATE TABLE IF NOT EXISTS engagements (id TEXT PRIMARY KEY, data TEXT NOT NULL)",
        )
        .execute(&self.pool)
        .await?;
        sqlx::query(
            "CREATE TABLE IF NOT EXISTS conversation_entries (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL,
                engagement_id TEXT NOT NULL,
                data TEXT NOT NULL,
                UNIQUE(engagement_id, id),
                FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE
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
            EntityTable::Tasks,
            EntityTable::Artifacts,
        ] {
            sqlx::query(table.create_sql()).execute(&self.pool).await?;
        }
        Ok(())
    }

    pub async fn put_engagement(&self, engagement: &Engagement) -> Result<(), StateError> {
        sqlx::query(
            "INSERT INTO engagements(id, data) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
        )
        .bind(&engagement.id)
        .bind(serde_json::to_string(engagement)?)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn engagement(&self, id: &str) -> Result<Engagement, StateError> {
        let row = sqlx::query("SELECT data FROM engagements WHERE id = ?")
            .bind(id)
            .fetch_optional(&self.pool)
            .await?
            .ok_or_else(|| StateError::EngagementNotFound(id.to_string()))?;
        Ok(serde_json::from_str(row.try_get("data")?)?)
    }

    pub async fn engagements(&self) -> Result<Vec<Engagement>, StateError> {
        let rows = sqlx::query("SELECT data FROM engagements ORDER BY id")
            .fetch_all(&self.pool)
            .await?;
        rows.into_iter()
            .map(|row| serde_json::from_str(row.get("data")).map_err(StateError::from))
            .collect()
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
        sqlx::query(table.upsert_sql())
            .bind(id)
            .bind(engagement_id)
            .bind(serde_json::to_string(value)?)
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
        rows.into_iter()
            .map(|row| serde_json::from_str(row.get("data")).map_err(StateError::from))
            .collect()
    }
}

#[path = "state_target.rs"]
mod target;

#[path = "state_conversation.rs"]
mod conversation;

#[cfg(test)]
#[path = "state_tests.rs"]
mod tests;
