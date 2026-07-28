use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::AtomicU8;
use std::sync::atomic::Ordering;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;

use axum::http::HeaderMap;
use axum::http::header::AUTHORIZATION;
use serde::Deserialize;
use sha2::Digest;
use sha2::Sha256;
use subtle::ConstantTimeEq;
use thiserror::Error;
use uuid::Uuid;

const BOOTSTRAP_AVAILABLE: u8 = 0;
const BOOTSTRAP_RESERVED: u8 = 1;
const BOOTSTRAP_CONSUMED: u8 = 2;

#[derive(Clone)]
pub struct ExecServerWebSocketAuth {
    state: Arc<AuthState>,
}

struct AuthState {
    bootstrap_sha256: [u8; 32],
    expires_at: u64,
    bootstrap_state: AtomicU8,
}

#[derive(Debug, Error)]
pub enum ExecServerWebSocketAuthError {
    #[error("failed to read exec-server authentication file: {0}")]
    Read(#[from] std::io::Error),
    #[error("failed to parse exec-server authentication file: {0}")]
    Parse(#[from] serde_json::Error),
    #[error("bootstrapSha256 must contain exactly 64 hexadecimal characters")]
    InvalidHash,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct AuthFile {
    bootstrap_sha256: String,
    expires_at: u64,
}

pub(crate) enum ConnectionAuthorization {
    Open,
    Bootstrap(BootstrapReservation),
    Session(String),
}

pub(crate) struct BootstrapReservation {
    state: Arc<AuthState>,
    consumed: AtomicBool,
}

impl ExecServerWebSocketAuth {
    pub fn from_file(path: &Path) -> Result<Self, ExecServerWebSocketAuthError> {
        let contents = std::fs::read_to_string(path)?;
        let auth_file: AuthFile = serde_json::from_str(&contents)?;
        let hash = decode_sha256(&auth_file.bootstrap_sha256)?;
        Ok(Self {
            state: Arc::new(AuthState {
                bootstrap_sha256: hash,
                expires_at: auth_file.expires_at,
                bootstrap_state: AtomicU8::new(BOOTSTRAP_AVAILABLE),
            }),
        })
    }

    pub(crate) fn authorize(&self, headers: &HeaderMap) -> Option<ConnectionAuthorization> {
        let authorization = headers.get(AUTHORIZATION)?.to_str().ok()?;
        if let Some(token) = authorization.strip_prefix("Bearer ") {
            return self.authorize_bootstrap(token);
        }
        let session_id = authorization.strip_prefix("RiftX-Session ")?;
        if self.state.bootstrap_state.load(Ordering::SeqCst) != BOOTSTRAP_CONSUMED
            || Uuid::parse_str(session_id).is_err()
        {
            return None;
        }
        Some(ConnectionAuthorization::Session(session_id.to_string()))
    }

    fn authorize_bootstrap(&self, token: &str) -> Option<ConnectionAuthorization> {
        let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
        let actual = Sha256::digest(token.as_bytes());
        if now > self.state.expires_at
            || !bool::from(actual.as_slice().ct_eq(&self.state.bootstrap_sha256))
            || self
                .state
                .bootstrap_state
                .compare_exchange(
                    BOOTSTRAP_AVAILABLE,
                    BOOTSTRAP_RESERVED,
                    Ordering::SeqCst,
                    Ordering::SeqCst,
                )
                .is_err()
        {
            return None;
        }
        Some(ConnectionAuthorization::Bootstrap(BootstrapReservation {
            state: Arc::clone(&self.state),
            consumed: AtomicBool::new(false),
        }))
    }
}

impl ConnectionAuthorization {
    pub(crate) fn validate_initialize(
        &self,
        resume_session_id: Option<&str>,
    ) -> Result<(), String> {
        match self {
            Self::Open => Ok(()),
            Self::Bootstrap(_) if resume_session_id.is_none() => Ok(()),
            Self::Bootstrap(_) => {
                Err("bootstrap authorization cannot resume an existing session".to_string())
            }
            Self::Session(expected) if resume_session_id == Some(expected.as_str()) => Ok(()),
            Self::Session(_) => Err(
                "RiftX-Session authorization must match the requested resumeSessionId".to_string(),
            ),
        }
    }

    pub(crate) fn consume_bootstrap(&self) {
        if let Self::Bootstrap(reservation) = self {
            reservation.consumed.store(true, Ordering::SeqCst);
            reservation
                .state
                .bootstrap_state
                .store(BOOTSTRAP_CONSUMED, Ordering::SeqCst);
        }
    }
}

impl Drop for BootstrapReservation {
    fn drop(&mut self) {
        if !self.consumed.load(Ordering::SeqCst) {
            let _ = self.state.bootstrap_state.compare_exchange(
                BOOTSTRAP_RESERVED,
                BOOTSTRAP_AVAILABLE,
                Ordering::SeqCst,
                Ordering::SeqCst,
            );
        }
    }
}

fn decode_sha256(value: &str) -> Result<[u8; 32], ExecServerWebSocketAuthError> {
    if value.len() != 64 {
        return Err(ExecServerWebSocketAuthError::InvalidHash);
    }
    let mut decoded = [0_u8; 32];
    for (index, output) in decoded.iter_mut().enumerate() {
        let offset = index * 2;
        *output = u8::from_str_radix(&value[offset..offset + 2], 16)
            .map_err(|_| ExecServerWebSocketAuthError::InvalidHash)?;
    }
    Ok(decoded)
}

#[cfg(test)]
#[path = "websocket_auth_tests.rs"]
mod tests;
