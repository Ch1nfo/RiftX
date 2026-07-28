use crate::Scope;
use serde::Deserialize;
use serde::Serialize;
use thiserror::Error;

/// Exact phrase operators must type to create or switch into Auto mode.
pub const AUTO_MODE_CONFIRMATION: &str = "AUTO MODE - TEST ENVIRONMENT ONLY";

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ExecutionMode {
    /// Guarded-pace red-team / exercise mode (was `Hardened` in v0.7).
    #[serde(alias = "hardened")]
    RedTeam,
    /// Operator-led pentest / inspection mode (was `Native` in v0.7).
    #[serde(alias = "native")]
    Pentest,
    Auto,
}

impl ExecutionMode {
    /// v0.8 does not require OS Guard for any mode.
    pub fn requires_guard(self) -> bool {
        false
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum EnvironmentClass {
    Lab,
    Staging,
    Production,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct IdentitySelector {
    pub domain: Option<String>,
    pub tenant: Option<String>,
    pub account: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AuthorizationWindow {
    pub starts_at: Option<i64>,
    pub expires_at: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AuthorizationScope {
    pub network: Scope,
    #[serde(default)]
    pub identities: Vec<IdentitySelector>,
    #[serde(default)]
    pub capabilities: Vec<String>,
    pub environment: EnvironmentClass,
    pub window: AuthorizationWindow,
}

impl AuthorizationScope {
    pub fn validate_for(&self, mode: ExecutionMode) -> Result<(), AuthorizationError> {
        if let (Some(starts_at), Some(expires_at)) = (self.window.starts_at, self.window.expires_at)
            && starts_at >= expires_at
        {
            return Err(AuthorizationError::InvalidWindow);
        }
        for identity in &self.identities {
            if identity.domain.as_deref().is_none_or(str::is_empty)
                && identity.tenant.as_deref().is_none_or(str::is_empty)
                && identity.account.as_deref().is_none_or(str::is_empty)
            {
                return Err(AuthorizationError::EmptyIdentitySelector);
            }
        }
        if self.capabilities.is_empty() {
            return Err(AuthorizationError::MissingCapabilities);
        }
        if self.capabilities.iter().any(|capability| {
            capability.trim().is_empty()
                || capability.len() > 128
                || capability.chars().any(char::is_control)
        }) {
            return Err(AuthorizationError::InvalidCapability);
        }
        if mode == ExecutionMode::Auto {
            if self.environment != EnvironmentClass::Lab {
                return Err(AuthorizationError::AutoRequiresLab);
            }
            if self.window.expires_at.is_none() {
                return Err(AuthorizationError::AutoRequiresExpiry);
            }
        }
        Ok(())
    }
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum AuthorizationError {
    #[error("authorization window must expire after it starts")]
    InvalidWindow,
    #[error("identity selectors must contain a domain, tenant, or account")]
    EmptyIdentitySelector,
    #[error("authorization requires at least one capability")]
    MissingCapabilities,
    #[error(
        "capabilities must be non-empty, contain no control characters, and be at most 128 bytes"
    )]
    InvalidCapability,
    #[error("Auto Mode is restricted to lab environments")]
    AutoRequiresLab,
    #[error("Auto Mode requires an authorization expiry")]
    AutoRequiresExpiry,
}

#[cfg(test)]
#[path = "authorization_tests.rs"]
mod tests;
