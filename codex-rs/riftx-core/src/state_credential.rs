use super::*;

impl StateStore {
    pub async fn put_credential_reference(
        &self,
        value: &CredentialReference,
    ) -> Result<(), StateError> {
        value.validate()?;
        self.put_entity(
            EntityTable::CredentialReferences,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn credential_reference(
        &self,
        engagement_id: &str,
        id: &str,
    ) -> Result<Option<CredentialReference>, StateError> {
        self.entity(EntityTable::CredentialReferences, engagement_id, id)
            .await
    }

    pub async fn credential_references(
        &self,
        engagement_id: &str,
    ) -> Result<Vec<CredentialReference>, StateError> {
        self.entities(EntityTable::CredentialReferences, engagement_id)
            .await
    }

    pub async fn delete_credential_reference(
        &self,
        engagement_id: &str,
        id: &str,
    ) -> Result<bool, StateError> {
        self.delete_entity(EntityTable::CredentialReferences, engagement_id, id)
            .await
    }

    pub async fn put_credential_grant(&self, value: &CredentialGrant) -> Result<(), StateError> {
        value.validate()?;
        self.put_entity(
            EntityTable::CredentialGrants,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn credential_grant(
        &self,
        engagement_id: &str,
        id: &str,
    ) -> Result<Option<CredentialGrant>, StateError> {
        self.entity(EntityTable::CredentialGrants, engagement_id, id)
            .await
    }

    pub async fn credential_grants(
        &self,
        engagement_id: &str,
    ) -> Result<Vec<CredentialGrant>, StateError> {
        self.entities(EntityTable::CredentialGrants, engagement_id)
            .await
    }
}

#[cfg(test)]
#[path = "state_credential_tests.rs"]
mod tests;
