use codex_keyring_store::CredentialStoreError;
use codex_keyring_store::DefaultKeyringStore;
use codex_keyring_store::KeyringStore;
use thiserror::Error;

const SERVICE: &str = "com.riftx.llm";
const ACCOUNT_PREFIX: &str = "api-key/";
const MAX_PROFILE_BYTES: usize = 128;
const MAX_SECRET_BYTES: usize = 64 * 1024;

#[derive(Clone, PartialEq, Eq)]
pub struct LlmApiKey(String);

impl LlmApiKey {
    pub fn new(value: String) -> Result<Self, CredentialError> {
        if value.trim().is_empty() {
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
}

impl std::fmt::Debug for LlmApiKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("LlmApiKey([REDACTED])")
    }
}

#[derive(Debug, Error)]
pub enum CredentialError {
    #[error(
        "LLM credential profile must use 1-128 ASCII letters, digits, dots, hyphens, or underscores"
    )]
    InvalidProfile,
    #[error("LLM API key cannot be empty")]
    EmptySecret,
    #[error("LLM API key exceeds the 64 KiB credential limit")]
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
            .load(SERVICE, &account)?
            .map(LlmApiKey::new)
            .transpose()
    }

    pub fn save(&self, profile: &str, api_key: LlmApiKey) -> Result<(), CredentialError> {
        let account = account(profile)?;
        self.store.save(SERVICE, &account, &api_key.0)?;
        Ok(())
    }

    pub fn delete(&self, profile: &str) -> Result<bool, CredentialError> {
        let account = account(profile)?;
        self.store.delete(SERVICE, &account).map_err(Into::into)
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

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
