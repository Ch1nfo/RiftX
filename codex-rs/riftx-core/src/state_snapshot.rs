use super::EntityTable;
use super::StateError;
use super::StateStore;
use codex_riftx_domain::Engagement;
use codex_riftx_domain::EngagementStateSnapshot;
use serde::de::DeserializeOwned;
use sqlx::Row;
use sqlx::Sqlite;
use sqlx::Transaction;

impl StateStore {
    pub async fn engagement_state_snapshot(
        &self,
        engagement_id: &str,
    ) -> Result<EngagementStateSnapshot, StateError> {
        let mut transaction = self.pool.begin().await?;
        let row = sqlx::query("SELECT data FROM engagements WHERE id = ?")
            .bind(engagement_id)
            .fetch_optional(&mut *transaction)
            .await?
            .ok_or_else(|| StateError::EngagementNotFound(engagement_id.to_string()))?;
        let engagement = self
            .open_value::<Engagement>(
                "engagements",
                engagement_id,
                engagement_id,
                row.try_get::<Vec<u8>, _>("data")?,
            )
            .await?;
        let auto_runs = self
            .snapshot_entities(&mut transaction, EntityTable::AutoRuns, engagement_id)
            .await?;
        let snapshot = EngagementStateSnapshot {
            engagement,
            auto_run: auto_runs.into_iter().next(),
            assets: self
                .snapshot_entities(&mut transaction, EntityTable::Assets, engagement_id)
                .await?,
            asset_relations: self
                .snapshot_entities(&mut transaction, EntityTable::AssetRelations, engagement_id)
                .await?,
            services: self
                .snapshot_entities(&mut transaction, EntityTable::Services, engagement_id)
                .await?,
            identities: self
                .snapshot_entities(&mut transaction, EntityTable::Identities, engagement_id)
                .await?,
            observations: self
                .snapshot_entities(&mut transaction, EntityTable::Observations, engagement_id)
                .await?,
            hypotheses: self
                .snapshot_entities(&mut transaction, EntityTable::Hypotheses, engagement_id)
                .await?,
            test_cases: self
                .snapshot_entities(&mut transaction, EntityTable::TestCases, engagement_id)
                .await?,
            executions: self
                .snapshot_entities(&mut transaction, EntityTable::Executions, engagement_id)
                .await?,
            findings: self
                .snapshot_entities(&mut transaction, EntityTable::Findings, engagement_id)
                .await?,
            evidence: self
                .snapshot_entities(&mut transaction, EntityTable::Evidence, engagement_id)
                .await?,
            attack_paths: self
                .snapshot_entities(&mut transaction, EntityTable::AttackPaths, engagement_id)
                .await?,
            coverage: self
                .snapshot_entities(&mut transaction, EntityTable::Coverage, engagement_id)
                .await?,
            tasks: self
                .snapshot_entities(&mut transaction, EntityTable::Tasks, engagement_id)
                .await?,
            artifacts: self
                .snapshot_entities(&mut transaction, EntityTable::Artifacts, engagement_id)
                .await?,
            approvals: self
                .snapshot_entities(&mut transaction, EntityTable::Approvals, engagement_id)
                .await?,
        };
        transaction.commit().await?;
        Ok(snapshot)
    }

    async fn snapshot_entities<T: DeserializeOwned>(
        &self,
        transaction: &mut Transaction<'_, Sqlite>,
        table: EntityTable,
        engagement_id: &str,
    ) -> Result<Vec<T>, StateError> {
        let rows = sqlx::query(table.list_sql())
            .bind(engagement_id)
            .fetch_all(&mut **transaction)
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
}

#[cfg(test)]
#[path = "state_snapshot_tests.rs"]
mod tests;
