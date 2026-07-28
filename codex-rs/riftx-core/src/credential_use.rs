use crate::CredentialGrant;
use crate::CredentialReference;
use ipnet::IpNet;
use serde::Deserialize;
use serde::Serialize;
use sha2::Digest;
use sha2::Sha256;
use std::net::IpAddr;
use thiserror::Error;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CredentialUseRequest {
    pub id: String,
    pub engagement_id: String,
    pub grant_id: String,
    pub target: CredentialUseTarget,
    pub capability: String,
    pub policy_revision: String,
    pub requested_at: i64,
}

impl CredentialUseRequest {
    pub fn validate(&self) -> Result<(), CredentialUseError> {
        for id in [&self.id, &self.engagement_id, &self.grant_id] {
            if !valid_identifier(id) {
                return Err(CredentialUseError::InvalidIdentifier);
            }
        }
        self.target.validate()?;
        if !valid_text(&self.capability, 128) {
            return Err(CredentialUseError::InvalidCapability);
        }
        if !valid_revision(&self.policy_revision) {
            return Err(CredentialUseError::InvalidPolicyRevision);
        }
        if self.requested_at <= 0 {
            return Err(CredentialUseError::InvalidTimestamp);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CredentialUseTarget {
    pub host: String,
    pub port: Option<u16>,
}

impl CredentialUseTarget {
    pub fn validate(&self) -> Result<(), CredentialUseError> {
        if self.port == Some(0) {
            return Err(CredentialUseError::InvalidTarget);
        }
        if self.host.parse::<IpAddr>().is_ok() {
            return Ok(());
        }
        if !valid_domain(&self.host) {
            return Err(CredentialUseError::InvalidTarget);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum CredentialUseStatus {
    Reserved,
    Succeeded,
    AuthenticationFailed,
    ExecutionFailed,
    Interrupted,
}

impl CredentialUseStatus {
    pub(crate) fn as_database_value(self) -> &'static str {
        match self {
            Self::Reserved => "reserved",
            Self::Succeeded => "succeeded",
            Self::AuthenticationFailed => "authenticationFailed",
            Self::ExecutionFailed => "executionFailed",
            Self::Interrupted => "interrupted",
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum CredentialUseOutcome {
    Succeeded,
    AuthenticationFailed,
    ExecutionFailed,
    Interrupted,
}

impl From<CredentialUseOutcome> for CredentialUseStatus {
    fn from(value: CredentialUseOutcome) -> Self {
        match value {
            CredentialUseOutcome::Succeeded => Self::Succeeded,
            CredentialUseOutcome::AuthenticationFailed => Self::AuthenticationFailed,
            CredentialUseOutcome::ExecutionFailed => Self::ExecutionFailed,
            CredentialUseOutcome::Interrupted => Self::Interrupted,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CredentialGrantUse {
    pub id: String,
    pub engagement_id: String,
    pub grant_id: String,
    pub credential_id: String,
    pub identity_hash: String,
    pub target: CredentialUseTarget,
    pub capability: String,
    pub policy_revision: String,
    pub status: CredentialUseStatus,
    pub started_at: i64,
    pub completed_at: Option<i64>,
}

pub(crate) fn identity_hash(reference: &CredentialReference) -> String {
    let principal = format!(
        "{}\u{0}{}\u{0}{}",
        reference.id,
        reference
            .domain
            .as_deref()
            .unwrap_or_default()
            .trim()
            .to_lowercase(),
        reference
            .username
            .as_deref()
            .unwrap_or_default()
            .trim()
            .to_lowercase()
    );
    format!("{:x}", Sha256::digest(principal.as_bytes()))
}

pub(crate) fn grant_allows_target(grant: &CredentialGrant, target: &CredentialUseTarget) -> bool {
    if grant
        .allowed_targets
        .ports
        .first()
        .is_some_and(|_| target.port.is_none())
        || target.port.is_some_and(|port| {
            !grant.allowed_targets.ports.is_empty() && !grant.allowed_targets.ports.contains(&port)
        })
    {
        return false;
    }
    if let Ok(address) = target.host.parse::<IpAddr>() {
        return grant
            .allowed_targets
            .cidrs
            .iter()
            .any(|network: &IpNet| network.contains(&address));
    }
    let target = target.host.trim_end_matches('.').to_lowercase();
    grant.allowed_targets.domains.iter().any(|allowed| {
        let allowed = allowed.trim_end_matches('.').to_lowercase();
        match allowed.strip_prefix("*.") {
            Some(suffix) => target != suffix && target.ends_with(&format!(".{suffix}")),
            None => target == allowed,
        }
    })
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum CredentialUseError {
    #[error(
        "credential use identifiers must use 1-128 ASCII letters, digits, hyphens, or underscores"
    )]
    InvalidIdentifier,
    #[error("credential use target must be an IP address or concrete domain")]
    InvalidTarget,
    #[error("credential use capability must be 1-128 bytes and contain no control characters")]
    InvalidCapability,
    #[error("credential use policy revision must be a lowercase SHA-256 digest")]
    InvalidPolicyRevision,
    #[error("credential use timestamp must be positive")]
    InvalidTimestamp,
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn valid_revision(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn valid_domain(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 253
        && !value.starts_with('.')
        && !value.ends_with('.')
        && value.split('.').all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && !label.starts_with('-')
                && !label.ends_with('-')
                && label
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        })
}

fn valid_text(value: &str, max_bytes: usize) -> bool {
    !value.trim().is_empty() && value.len() <= max_bytes && !value.chars().any(char::is_control)
}

#[cfg(test)]
#[path = "credential_use_tests.rs"]
mod tests;
