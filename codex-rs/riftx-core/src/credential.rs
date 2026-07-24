use crate::Scope;
use serde::Deserialize;
use serde::Serialize;
use thiserror::Error;

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
    pub engagement_id: String,
    pub label: String,
    pub kind: CredentialKind,
    pub storage_key: String,
    pub username: Option<String>,
    pub domain: Option<String>,
    pub created_at: i64,
}

impl CredentialReference {
    pub fn validate(&self) -> Result<(), CredentialError> {
        if !valid_identifier(&self.id) || !valid_identifier(&self.engagement_id) {
            return Err(CredentialError::MissingIdentity);
        }
        if !valid_text(&self.label, 128) {
            return Err(CredentialError::InvalidLabel);
        }
        if !valid_text(&self.storage_key, 512) {
            return Err(CredentialError::MissingStorageKey);
        }
        for value in [&self.username, &self.domain].into_iter().flatten() {
            if !valid_text(value, 256) {
                return Err(CredentialError::InvalidPrincipal);
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CredentialGrant {
    pub id: String,
    pub engagement_id: String,
    pub credential_id: String,
    pub allowed_targets: Scope,
    pub allowed_capabilities: Vec<String>,
    pub max_uses: u32,
    pub max_failures_per_identity: u32,
    pub starts_at: Option<i64>,
    pub expires_at: i64,
    pub created_at: i64,
    pub revoked_at: Option<i64>,
}

impl CredentialGrant {
    pub fn validate(&self) -> Result<(), CredentialError> {
        if !valid_identifier(&self.id)
            || !valid_identifier(&self.engagement_id)
            || !valid_identifier(&self.credential_id)
        {
            return Err(CredentialError::MissingIdentity);
        }
        if self.allowed_targets.cidrs.is_empty() && self.allowed_targets.domains.is_empty() {
            return Err(CredentialError::MissingTarget);
        }
        if self.allowed_targets.domains.iter().any(|domain| {
            !valid_text(domain, 253)
                || domain.starts_with('.')
                || domain.ends_with('.')
                || domain.contains("..")
        }) {
            return Err(CredentialError::InvalidTarget);
        }
        if self.allowed_capabilities.is_empty()
            || self
                .allowed_capabilities
                .iter()
                .any(|capability| !valid_text(capability, 128))
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
            || self.expires_at <= 0
        {
            return Err(CredentialError::InvalidWindow);
        }
        if self
            .revoked_at
            .is_some_and(|revoked_at| revoked_at < self.created_at)
        {
            return Err(CredentialError::InvalidRevocation);
        }
        Ok(())
    }

    pub fn is_active_at(&self, timestamp: i64) -> bool {
        self.revoked_at.is_none()
            && self
                .starts_at
                .is_none_or(|starts_at| starts_at <= timestamp)
            && timestamp < self.expires_at
    }
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum CredentialError {
    #[error("credential and grant identifiers must not be empty")]
    MissingIdentity,
    #[error("credential label must be 1-128 bytes and contain no control characters")]
    InvalidLabel,
    #[error("credential storage key must not be empty")]
    MissingStorageKey,
    #[error("credential username and domain must be 1-256 bytes and contain no control characters")]
    InvalidPrincipal,
    #[error("credential grant must include at least one CIDR or domain")]
    MissingTarget,
    #[error("credential grant domains must be valid non-empty scope values")]
    InvalidTarget,
    #[error("credential grant must include at least one capability")]
    MissingCapability,
    #[error("credential grant max uses must be greater than zero")]
    ZeroMaxUses,
    #[error("credential grant failure limit must be greater than zero")]
    ZeroFailureLimit,
    #[error("credential grant must expire after it starts")]
    InvalidWindow,
    #[error("credential grant cannot be revoked before it was created")]
    InvalidRevocation,
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn valid_text(value: &str, max_bytes: usize) -> bool {
    !value.trim().is_empty() && value.len() <= max_bytes && !value.chars().any(char::is_control)
}

#[cfg(test)]
#[path = "credential_tests.rs"]
mod tests;
