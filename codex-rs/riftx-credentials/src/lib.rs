use codex_keyring_store::CredentialStoreError;
use codex_keyring_store::DefaultKeyringStore;
use codex_keyring_store::KeyringStore;
use thiserror::Error;
use zeroize::Zeroize;

mod process;

pub use process::*;

const LLM_SERVICE: &str = "com.riftx.llm";
const ASSESSMENT_SERVICE: &str = "com.riftx.assessment";
const ACCOUNT_PREFIX: &str = "api-key/";
const MAX_PROFILE_BYTES: usize = 128;
const MAX_IDENTIFIER_BYTES: usize = 128;
const MAX_SECRET_BYTES: usize = 64 * 1024;

#[derive(Clone, PartialEq, Eq)]
pub struct LlmApiKey(String);

impl LlmApiKey {
    pub fn new(value: String) -> Result<Self, CredentialError> {
        let value = value.trim().to_string();
        if value.is_empty() {
            return Err(CredentialError::EmptySecret);
        }
        if value.len() > MAX_SECRET_BYTES {
            return Err(CredentialError::SecretTooLarge);
        }
        Ok(Self(value))
    }

    pub fn into_inner(self) -> String {
        self.0
    }

    pub fn into_bytes(self) -> Vec<u8> {
        self.0.into_bytes()
    }
}

impl std::fmt::Debug for LlmApiKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("LlmApiKey([REDACTED])")
    }
}

#[derive(Clone, PartialEq, Eq)]
pub struct AssessmentSecret(String);

impl AssessmentSecret {
    pub fn new(value: String) -> Result<Self, CredentialError> {
        if value.is_empty() {
            return Err(CredentialError::EmptySecret);
        }
        if value.len() > MAX_SECRET_BYTES {
            return Err(CredentialError::SecretTooLarge);
        }
        Ok(Self(value))
    }

    pub fn into_inner(mut self) -> String {
        std::mem::take(&mut self.0)
    }
}

impl std::fmt::Debug for AssessmentSecret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("AssessmentSecret([REDACTED])")
    }
}

impl Drop for AssessmentSecret {
    fn drop(&mut self) {
        self.0.zeroize();
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CredentialLocator {
    engagement_id: String,
    credential_id: String,
}

impl CredentialLocator {
    pub fn new(
        engagement_id: impl Into<String>,
        credential_id: impl Into<String>,
    ) -> Result<Self, CredentialError> {
        let engagement_id = engagement_id.into();
        if !valid_identifier(&engagement_id) {
            return Err(CredentialError::InvalidEngagementId);
        }
        let credential_id = credential_id.into();
        if !valid_identifier(&credential_id) {
            return Err(CredentialError::InvalidCredentialId);
        }
        Ok(Self {
            engagement_id,
            credential_id,
        })
    }

    pub fn storage_key(&self) -> String {
        format!(
            "engagement/{}/credential/{}",
            self.engagement_id, self.credential_id
        )
    }
}

#[derive(Debug, Error)]
pub enum CredentialError {
    #[error(
        "LLM credential profile must use 1-128 ASCII letters, digits, dots, hyphens, or underscores"
    )]
    InvalidProfile,
    #[error("engagement identifier is invalid")]
    InvalidEngagementId,
    #[error("credential identifier is invalid")]
    InvalidCredentialId,
    #[error("credential secret cannot be empty")]
    EmptySecret,
    #[error("credential secret exceeds the 64 KiB credential limit")]
    SecretTooLarge,
    #[error("operating system credential store failed: {0}")]
    Store(String),
}

impl From<CredentialStoreError> for CredentialError {
    fn from(error: CredentialStoreError) -> Self {
        Self::Store(error.message())
    }
}

#[derive(Debug, Clone)]
pub struct LlmCredentialStore<S = DefaultKeyringStore> {
    store: S,
}

impl Default for LlmCredentialStore {
    fn default() -> Self {
        Self {
            store: DefaultKeyringStore,
        }
    }
}

impl<S> LlmCredentialStore<S>
where
    S: KeyringStore,
{
    pub fn new(store: S) -> Self {
        Self { store }
    }

    pub fn load(&self, profile: &str) -> Result<Option<LlmApiKey>, CredentialError> {
        let account = account(profile)?;
        self.store
            .load(LLM_SERVICE, &account)?
            .map(LlmApiKey::new)
            .transpose()
    }

    pub fn save(&self, profile: &str, api_key: LlmApiKey) -> Result<(), CredentialError> {
        let account = account(profile)?;
        self.store.save(LLM_SERVICE, &account, &api_key.0)?;
        Ok(())
    }

    pub fn delete(&self, profile: &str) -> Result<bool, CredentialError> {
        let account = account(profile)?;
        self.store.delete(LLM_SERVICE, &account).map_err(Into::into)
    }
}

#[derive(Debug, Clone)]
pub struct AssessmentCredentialStore<S = DefaultKeyringStore> {
    store: S,
}

impl Default for AssessmentCredentialStore {
    fn default() -> Self {
        Self {
            store: DefaultKeyringStore,
        }
    }
}

impl<S> AssessmentCredentialStore<S>
where
    S: KeyringStore,
{
    pub fn new(store: S) -> Self {
        Self { store }
    }

    pub fn load(
        &self,
        locator: &CredentialLocator,
    ) -> Result<Option<AssessmentSecret>, CredentialError> {
        self.store
            .load(ASSESSMENT_SERVICE, &locator.storage_key())?
            .map(AssessmentSecret::new)
            .transpose()
    }

    pub fn save(
        &self,
        locator: &CredentialLocator,
        secret: AssessmentSecret,
    ) -> Result<(), CredentialError> {
        self.store
            .save(ASSESSMENT_SERVICE, &locator.storage_key(), &secret.0)?;
        Ok(())
    }

    pub fn delete(&self, locator: &CredentialLocator) -> Result<bool, CredentialError> {
        self.store
            .delete(ASSESSMENT_SERVICE, &locator.storage_key())
            .map_err(Into::into)
    }
}

fn account(profile: &str) -> Result<String, CredentialError> {
    if profile.is_empty()
        || profile.len() > MAX_PROFILE_BYTES
        || !profile
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_'))
    {
        return Err(CredentialError::InvalidProfile);
    }
    Ok(format!("{ACCOUNT_PREFIX}{profile}"))
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_IDENTIFIER_BYTES
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
