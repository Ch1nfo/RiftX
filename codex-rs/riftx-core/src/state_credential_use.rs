use super::*;
use crate::CredentialGrantUse;
use crate::CredentialUseOutcome;
use crate::CredentialUseRequest;
use crate::CredentialUseStatus;
use crate::grant_allows_target;
use crate::identity_hash;
use sqlx::Sqlite;
use sqlx::pool::PoolConnection;

impl StateStore {
    pub async fn reserve_credential_use(
        &self,
        request: &CredentialUseRequest,
    ) -> Result<CredentialGrantUse, StateError> {
        request.validate()?;
        let mut connection = self.pool.acquire().await?;
        sqlx::query("BEGIN IMMEDIATE")
            .execute(&mut *connection)
            .await?;
        let result = reserve(self, &mut connection, request).await;
        finish_transaction(&mut connection, result).await
    }

    pub async fn complete_credential_use(
        &self,
        engagement_id: &str,
        use_id: &str,
        outcome: CredentialUseOutcome,
        completed_at: i64,
    ) -> Result<CredentialGrantUse, StateError> {
        let mut connection = self.pool.acquire().await?;
        sqlx::query("BEGIN IMMEDIATE")
            .execute(&mut *connection)
            .await?;
        let result = complete(
            self,
            &mut connection,
            engagement_id,
            use_id,
            outcome,
            completed_at,
        )
        .await;
        finish_transaction(&mut connection, result).await
    }

    pub async fn credential_uses(
        &self,
        engagement_id: &str,
    ) -> Result<Vec<CredentialGrantUse>, StateError> {
        let rows = sqlx::query(
            "SELECT id, data FROM credential_grant_uses
             WHERE engagement_id = ? ORDER BY started_at, id",
        )
        .bind(engagement_id)
        .fetch_all(&self.pool)
        .await?;
        let mut usages = Vec::with_capacity(rows.len());
        for row in rows {
            let id: String = row.try_get("id")?;
            usages.push(
                self.open_value(
                    "credential_grant_uses",
                    engagement_id,
                    &id,
                    row.try_get::<Vec<u8>, _>("data")?,
                )
                .await?,
            );
        }
        Ok(usages)
    }
}

async fn reserve(
    store: &StateStore,
    connection: &mut PoolConnection<Sqlite>,
    request: &CredentialUseRequest,
) -> Result<CredentialGrantUse, StateError> {
    let engagement = sqlx::query("SELECT data FROM engagements WHERE id = ?")
        .bind(&request.engagement_id)
        .fetch_optional(&mut **connection)
        .await?
        .ok_or_else(|| StateError::EngagementNotFound(request.engagement_id.clone()))?;
    let engagement: Engagement = store
        .open_value(
            "engagements",
            &request.engagement_id,
            &request.engagement_id,
            engagement.try_get("data")?,
        )
        .await?;
    if engagement.policy_revision != request.policy_revision {
        return Err(StateError::CredentialPolicyRevisionMismatch);
    }
    let grant =
        sqlx::query("SELECT data FROM credential_grants WHERE engagement_id = ? AND id = ?")
            .bind(&request.engagement_id)
            .bind(&request.grant_id)
            .fetch_optional(&mut **connection)
            .await?
            .ok_or_else(|| StateError::CredentialGrantNotFound(request.grant_id.clone()))?;
    let grant: CredentialGrant = store
        .open_value(
            EntityTable::CredentialGrants.name(),
            &request.engagement_id,
            &request.grant_id,
            grant.try_get("data")?,
        )
        .await?;
    grant.validate()?;
    if !grant.is_active_at(request.requested_at) {
        return Err(StateError::CredentialGrantInactive);
    }
    if !grant.allowed_capabilities.contains(&request.capability) {
        return Err(StateError::CredentialCapabilityDenied(
            request.capability.clone(),
        ));
    }
    if !grant_allows_target(&grant, &request.target) {
        return Err(StateError::CredentialTargetDenied(format!(
            "{}:{}",
            request.target.host,
            request
                .target
                .port
                .map_or_else(|| "*".to_string(), |port| port.to_string())
        )));
    }
    let reference =
        sqlx::query("SELECT data FROM credential_references WHERE engagement_id = ? AND id = ?")
            .bind(&request.engagement_id)
            .bind(&grant.credential_id)
            .fetch_optional(&mut **connection)
            .await?
            .ok_or_else(|| StateError::CredentialReferenceNotFound(grant.credential_id.clone()))?;
    let reference: CredentialReference = store
        .open_value(
            EntityTable::CredentialReferences.name(),
            &request.engagement_id,
            &grant.credential_id,
            reference.try_get("data")?,
        )
        .await?;
    if !reference.configured {
        return Err(StateError::CredentialSecretUnavailable(reference.id));
    }
    let identity_hash = identity_hash(&reference);
    let use_count: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM credential_grant_uses WHERE grant_id = ?")
            .bind(&grant.id)
            .fetch_one(&mut **connection)
            .await?;
    if use_count >= i64::from(grant.max_uses) {
        return Err(StateError::CredentialUseLimitExceeded);
    }
    let failure_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM credential_grant_uses
         WHERE grant_id = ? AND identity_hash = ? AND status = 'authenticationFailed'",
    )
    .bind(&grant.id)
    .bind(&identity_hash)
    .fetch_one(&mut **connection)
    .await?;
    if failure_count >= i64::from(grant.max_failures_per_identity) {
        return Err(StateError::CredentialFailureLimitExceeded);
    }
    let active_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM credential_grant_uses
         WHERE grant_id = ? AND identity_hash = ? AND status = 'reserved'",
    )
    .bind(&grant.id)
    .bind(&identity_hash)
    .fetch_one(&mut **connection)
    .await?;
    if active_count > 0 {
        return Err(StateError::CredentialUseInProgress);
    }
    let usage = CredentialGrantUse {
        id: request.id.clone(),
        engagement_id: request.engagement_id.clone(),
        grant_id: grant.id,
        credential_id: grant.credential_id,
        identity_hash,
        target: request.target.clone(),
        capability: request.capability.clone(),
        policy_revision: request.policy_revision.clone(),
        status: CredentialUseStatus::Reserved,
        started_at: request.requested_at,
        completed_at: None,
    };
    let data = store
        .seal_value(
            "credential_grant_uses",
            &usage.engagement_id,
            &usage.id,
            &usage,
        )
        .await?;
    sqlx::query(
        "INSERT INTO credential_grant_uses(
            id, engagement_id, grant_id, credential_id, identity_hash,
            status, started_at, completed_at, data
         ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
    )
    .bind(&usage.id)
    .bind(&usage.engagement_id)
    .bind(&usage.grant_id)
    .bind(&usage.credential_id)
    .bind(&usage.identity_hash)
    .bind(usage.status.as_database_value())
    .bind(usage.started_at)
    .bind(usage.completed_at)
    .bind(data)
    .execute(&mut **connection)
    .await?;
    Ok(usage)
}

async fn complete(
    store: &StateStore,
    connection: &mut PoolConnection<Sqlite>,
    engagement_id: &str,
    use_id: &str,
    outcome: CredentialUseOutcome,
    completed_at: i64,
) -> Result<CredentialGrantUse, StateError> {
    let row =
        sqlx::query("SELECT data FROM credential_grant_uses WHERE engagement_id = ? AND id = ?")
            .bind(engagement_id)
            .bind(use_id)
            .fetch_optional(&mut **connection)
            .await?
            .ok_or_else(|| StateError::CredentialUseNotFound(use_id.to_string()))?;
    let mut usage: CredentialGrantUse = store
        .open_value(
            "credential_grant_uses",
            engagement_id,
            use_id,
            row.try_get("data")?,
        )
        .await?;
    let status = CredentialUseStatus::from(outcome);
    if usage.status != CredentialUseStatus::Reserved {
        if usage.status == status {
            return Ok(usage);
        }
        return Err(StateError::InvalidCredentialUseTransition {
            id: usage.id,
            from: usage.status,
            to: status,
        });
    }
    if completed_at < usage.started_at {
        return Err(CredentialUseError::InvalidTimestamp.into());
    }
    usage.status = status;
    usage.completed_at = Some(completed_at);
    let data = store
        .seal_value("credential_grant_uses", engagement_id, use_id, &usage)
        .await?;
    sqlx::query(
        "UPDATE credential_grant_uses
         SET status = ?, completed_at = ?, data = ?
         WHERE engagement_id = ? AND id = ?",
    )
    .bind(status.as_database_value())
    .bind(completed_at)
    .bind(data)
    .bind(engagement_id)
    .bind(use_id)
    .execute(&mut **connection)
    .await?;
    Ok(usage)
}

async fn finish_transaction<T>(
    connection: &mut PoolConnection<Sqlite>,
    result: Result<T, StateError>,
) -> Result<T, StateError> {
    match result {
        Ok(value) => {
            sqlx::query("COMMIT").execute(&mut **connection).await?;
            Ok(value)
        }
        Err(error) => {
            let _ = sqlx::query("ROLLBACK").execute(&mut **connection).await;
            Err(error)
        }
    }
}

#[cfg(test)]
#[path = "state_credential_use_tests.rs"]
mod tests;
