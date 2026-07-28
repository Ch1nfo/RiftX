use crate::CryptoError;
use crate::EncryptionContext;
use crate::EngagementDataKey;
use crate::EngagementKeyStore;
use crate::open;
use crate::seal;
use codex_keyring_store::DefaultKeyringStore;
use codex_keyring_store::KeyringStore;
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::PoisonError;
use std::sync::RwLock;

/// Encrypts engagement-owned records without exposing raw data keys to state or service code.
///
/// Implementations must bind ciphertext to all three record identifiers, keep decrypted keys out
/// of persistent application state, and return `CryptoError::KeyMissing` instead of generating a
/// replacement key during reads.
pub trait EngagementRecordCipher: Send + Sync {
    fn create_engagement(&self, engagement_id: &str) -> Result<(), CryptoError>;

    fn prepare_engagement(&self, engagement_id: &str) -> Result<(), CryptoError>;

    fn seal_record(
        &self,
        engagement_id: &str,
        record_kind: &str,
        record_id: &str,
        plaintext: &[u8],
    ) -> Result<Vec<u8>, CryptoError>;

    fn open_record(
        &self,
        engagement_id: &str,
        record_kind: &str,
        record_id: &str,
        envelope: &[u8],
    ) -> Result<zeroize::Zeroizing<Vec<u8>>, CryptoError>;

    fn delete_engagement(&self, engagement_id: &str) -> Result<bool, CryptoError>;
}

#[derive(Debug)]
pub struct KeyringEngagementCipher<S = DefaultKeyringStore> {
    store: EngagementKeyStore<S>,
    keys: RwLock<HashMap<String, Arc<EngagementDataKey>>>,
}

impl Default for KeyringEngagementCipher {
    fn default() -> Self {
        Self::new(DefaultKeyringStore)
    }
}

impl<S> KeyringEngagementCipher<S>
where
    S: KeyringStore,
{
    pub fn new(store: S) -> Self {
        Self {
            store: EngagementKeyStore::new(store),
            keys: RwLock::new(HashMap::new()),
        }
    }

    fn key(&self, engagement_id: &str) -> Result<Arc<EngagementDataKey>, CryptoError> {
        if let Some(key) = self
            .keys
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .get(engagement_id)
            .cloned()
        {
            return Ok(key);
        }
        let key = Arc::new(
            self.store
                .load(engagement_id)?
                .ok_or(CryptoError::KeyMissing)?,
        );
        self.keys
            .write()
            .unwrap_or_else(PoisonError::into_inner)
            .insert(engagement_id.to_string(), key.clone());
        Ok(key)
    }
}

impl<S> EngagementRecordCipher for KeyringEngagementCipher<S>
where
    S: KeyringStore + Send + Sync,
{
    fn create_engagement(&self, engagement_id: &str) -> Result<(), CryptoError> {
        let key = Arc::new(self.store.create(engagement_id)?);
        self.keys
            .write()
            .unwrap_or_else(PoisonError::into_inner)
            .insert(engagement_id.to_string(), key);
        Ok(())
    }

    fn prepare_engagement(&self, engagement_id: &str) -> Result<(), CryptoError> {
        self.key(engagement_id).map(|_| ())
    }

    fn seal_record(
        &self,
        engagement_id: &str,
        record_kind: &str,
        record_id: &str,
        plaintext: &[u8],
    ) -> Result<Vec<u8>, CryptoError> {
        let context = EncryptionContext::new(engagement_id, record_kind, record_id)?;
        let key = self.key(engagement_id)?;
        seal(&key, &context, plaintext)
    }

    fn open_record(
        &self,
        engagement_id: &str,
        record_kind: &str,
        record_id: &str,
        envelope: &[u8],
    ) -> Result<zeroize::Zeroizing<Vec<u8>>, CryptoError> {
        let context = EncryptionContext::new(engagement_id, record_kind, record_id)?;
        let key = self.key(engagement_id)?;
        open(&key, &context, envelope)
    }

    fn delete_engagement(&self, engagement_id: &str) -> Result<bool, CryptoError> {
        self.keys
            .write()
            .unwrap_or_else(PoisonError::into_inner)
            .remove(engagement_id);
        self.store.delete(engagement_id)
    }
}
