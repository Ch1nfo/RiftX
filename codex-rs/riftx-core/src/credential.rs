use crate::Scope;
use serde::Deserialize;
use serde::Serialize;
use thiserror::Error;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum CredentialStore {
    MacOsKeychain,
    WindowsCredentialManager,
    LinuxSecretService,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum CredentialKind {
    Password,
    ApiToken,
    PrivateKey,
    Certificate,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CredentialReference {
    pub id: String,
    pub label: String,
    pub kind: CredentialKind,
    pub store: CredentialStore,
    pub storage_key: String,
    pub username: Option<String>,
    pub domain: Option<String>,
}

impl CredentialReference {
    pub fn validate(&self) -> Result<(), CredentialError> {
        if self.id.trim().is_empty() || self.label.trim().is_empty() {
            return Err(CredentialError::MissingIdentity);
        }
        if self.storage_key.trim().is_empty() {
            return Err(CredentialError::MissingStorageKey);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CredentialGrant {
    pub id: String,
    pub credential_id: String,
    pub allowed_targets: Scope,
    pub allowed_capabilities: Vec<String>,
    pub max_uses: u32,
    pub max_failures_per_identity: u32,
    pub starts_at: Option<i64>,
    pub expires_at: i64,
}

impl CredentialGrant {
    pub fn validate(&self) -> Result<(), CredentialError> {
        if self.id.trim().is_empty() || self.credential_id.trim().is_empty() {
            return Err(CredentialError::MissingIdentity);
        }
        if self.allowed_capabilities.is_empty()
            || self
                .allowed_capabilities
                .iter()
                .any(|capability| capability.trim().is_empty())
        {
            return Err(CredentialError::MissingCapability);
        }
        if self.max_uses == 0 {
            return Err(CredentialError::ZeroMaxUses);
        }
        if self.max_failures_per_identity == 0 {
            return Err(CredentialError::ZeroFailureLimit);
        }
        if self
            .starts_at
            .is_some_and(|starts_at| starts_at >= self.expires_at)
        {
            return Err(CredentialError::InvalidWindow);
        }
        Ok(())
    }
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum CredentialError {
    #[error("credential and grant identifiers must not be empty")]
    MissingIdentity,
    #[error("credential storage key must not be empty")]
    MissingStorageKey,
    #[error("credential grant must include at least one capability")]
    MissingCapability,
    #[error("credential grant max uses must be greater than zero")]
    ZeroMaxUses,
    #[error("credential grant failure limit must be greater than zero")]
    ZeroFailureLimit,
    #[error("credential grant must expire after it starts")]
    InvalidWindow,
}

#[cfg(test)]
#[path = "credential_tests.rs"]
mod tests;
