use crate::Artifact;
use crate::Asset;
use crate::Engagement;
use crate::EngagementStatus;
use crate::Evidence;
use crate::Finding;
use crate::Service;
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
}

#[derive(Clone)]
pub struct StateStore {
    pool: SqlitePool,
}

#[derive(Clone, Copy)]
enum EntityTable {
    Assets,
    Services,
    Findings,
    Evidence,
    Tasks,
    Artifacts,
}

impl EntityTable {
    fn create_sql(self) -> &'static str {
        match self {
            Self::Assets => {
                "CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL, data TEXT NOT NULL, FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE)"
            }
            Self::Services => {
                "CREATE TABLE IF NOT EXISTS services (id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL, data TEXT NOT NULL, FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE)"
            }
            Self::Findings => {
                "CREATE TABLE IF NOT EXISTS findings (id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL, data TEXT NOT NULL, FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE)"
            }
            Self::Evidence => {
                "CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL, data TEXT NOT NULL, FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE)"
            }
            Self::Tasks => {
                "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL, data TEXT NOT NULL, FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE)"
            }
            Self::Artifacts => {
                "CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL, data TEXT NOT NULL, FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE)"
            }
        }
    }

    fn upsert_sql(self) -> &'static str {
        match self {
            Self::Assets => {
                "INSERT INTO assets(id, engagement_id, data) VALUES(?, ?, ?) ON CONFLICT(id) DO UPDATE SET engagement_id=excluded.engagement_id, data=excluded.data"
            }
            Self::Services => {
                "INSERT INTO services(id, engagement_id, data) VALUES(?, ?, ?) ON CONFLICT(id) DO UPDATE SET engagement_id=excluded.engagement_id, data=excluded.data"
            }
            Self::Findings => {
                "INSERT INTO findings(id, engagement_id, data) VALUES(?, ?, ?) ON CONFLICT(id) DO UPDATE SET engagement_id=excluded.engagement_id, data=excluded.data"
            }
            Self::Evidence => {
                "INSERT INTO evidence(id, engagement_id, data) VALUES(?, ?, ?) ON CONFLICT(id) DO UPDATE SET engagement_id=excluded.engagement_id, data=excluded.data"
            }
            Self::Tasks => {
                "INSERT INTO tasks(id, engagement_id, data) VALUES(?, ?, ?) ON CONFLICT(id) DO UPDATE SET engagement_id=excluded.engagement_id, data=excluded.data"
            }
            Self::Artifacts => {
                "INSERT INTO artifacts(id, engagement_id, data) VALUES(?, ?, ?) ON CONFLICT(id) DO UPDATE SET engagement_id=excluded.engagement_id, data=excluded.data"
            }
        }
    }

    fn list_sql(self) -> &'static str {
        match self {
            Self::Assets => "SELECT data FROM assets WHERE engagement_id = ? ORDER BY id",
            Self::Services => "SELECT data FROM services WHERE engagement_id = ? ORDER BY id",
            Self::Findings => "SELECT data FROM findings WHERE engagement_id = ? ORDER BY id",
            Self::Evidence => "SELECT data FROM evidence WHERE engagement_id = ? ORDER BY id",
            Self::Tasks => "SELECT data FROM tasks WHERE engagement_id = ? ORDER BY id",
            Self::Artifacts => "SELECT data FROM artifacts WHERE engagement_id = ? ORDER BY id",
        }
    }
}

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
        for table in [
            EntityTable::Assets,
            EntityTable::Services,
            EntityTable::Findings,
            EntityTable::Evidence,
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

#[cfg(test)]
#[path = "state_tests.rs"]
mod tests;
