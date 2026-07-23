use crate::Scope;
use serde::Deserialize;
use serde::Serialize;
use thiserror::Error;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ExecutionMode {
    Native,
    Hardened,
    Auto,
}

impl ExecutionMode {
    pub fn requires_guard(self) -> bool {
        matches!(self, Self::Hardened | Self::Auto)
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum EnvironmentClass {
    Lab,
    Staging,
    Production,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
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
    #[error("Auto Mode is restricted to lab environments")]
    AutoRequiresLab,
    #[error("Auto Mode requires an authorization expiry")]
    AutoRequiresExpiry,
}

#[cfg(test)]
#[path = "authorization_tests.rs"]
mod tests;
