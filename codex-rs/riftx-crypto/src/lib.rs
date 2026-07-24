use aes_gcm::Aes256Gcm;
use aes_gcm::Nonce;
use aes_gcm::aead::Aead;
use aes_gcm::aead::AeadCore;
use aes_gcm::aead::KeyInit;
use aes_gcm::aead::OsRng;
use aes_gcm::aead::Payload;
use base64::Engine;
use base64::engine::general_purpose::STANDARD_NO_PAD;
use codex_keyring_store::DefaultKeyringStore;
use codex_keyring_store::KeyringStore;
use thiserror::Error;
use zeroize::Zeroize;
use zeroize::Zeroizing;

const KEY_SERVICE: &str = "com.riftx.engagement-key";
const KEY_BYTES: usize = 32;
const NONCE_BYTES: usize = 12;
const TAG_BYTES: usize = 16;
const ENVELOPE_HEADER: &[u8; 4] = b"RXE1";
const MAX_CONTEXT_COMPONENT_BYTES: usize = 256;
const MAX_ENVELOPE_PLAINTEXT_BYTES: usize = 16 * 1024 * 1024;

pub struct EngagementDataKey(Zeroizing<[u8; KEY_BYTES]>);

impl EngagementDataKey {
    pub fn generate() -> Self {
        let mut generated = Aes256Gcm::generate_key(&mut OsRng);
        let mut bytes = [0_u8; KEY_BYTES];
        bytes.copy_from_slice(&generated);
        generated.zeroize();
        Self(Zeroizing::new(bytes))
    }

    fn from_bytes(bytes: [u8; KEY_BYTES]) -> Self {
        Self(Zeroizing::new(bytes))
    }

    fn expose(&self) -> &[u8; KEY_BYTES] {
        &self.0
    }
}

impl std::fmt::Debug for EngagementDataKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("EngagementDataKey([REDACTED])")
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EncryptionContext {
    associated_data: Vec<u8>,
}

impl EncryptionContext {
    pub fn new(
        engagement_id: &str,
        record_kind: &str,
        record_id: &str,
    ) -> Result<Self, CryptoError> {
        let mut associated_data = b"RiftX/engagement-envelope/v1".to_vec();
        for component in [engagement_id, record_kind, record_id] {
            validate_component(component)?;
            let length =
                u32::try_from(component.len()).map_err(|_| CryptoError::InvalidContextComponent)?;
            associated_data.extend_from_slice(&length.to_be_bytes());
            associated_data.extend_from_slice(component.as_bytes());
        }
        Ok(Self { associated_data })
    }

    pub fn as_bytes(&self) -> &[u8] {
        &self.associated_data
    }
}

pub fn seal(
    key: &EngagementDataKey,
    context: &EncryptionContext,
    plaintext: &[u8],
) -> Result<Vec<u8>, CryptoError> {
    if plaintext.len() > MAX_ENVELOPE_PLAINTEXT_BYTES {
        return Err(CryptoError::PlaintextTooLarge);
    }
    let cipher =
        Aes256Gcm::new_from_slice(key.expose()).map_err(|_| CryptoError::InvalidStoredKey)?;
    let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
    let ciphertext = cipher
        .encrypt(
            &nonce,
            Payload {
                msg: plaintext,
                aad: context.as_bytes(),
            },
        )
        .map_err(|_| CryptoError::EncryptionFailed)?;
    let mut envelope = Vec::with_capacity(ENVELOPE_HEADER.len() + NONCE_BYTES + ciphertext.len());
    envelope.extend_from_slice(ENVELOPE_HEADER);
    envelope.extend_from_slice(&nonce);
    envelope.extend_from_slice(&ciphertext);
    Ok(envelope)
}

pub fn open(
    key: &EngagementDataKey,
    context: &EncryptionContext,
    envelope: &[u8],
) -> Result<Zeroizing<Vec<u8>>, CryptoError> {
    let minimum_bytes = ENVELOPE_HEADER.len() + NONCE_BYTES + TAG_BYTES;
    let maximum_bytes =
        ENVELOPE_HEADER.len() + NONCE_BYTES + MAX_ENVELOPE_PLAINTEXT_BYTES + TAG_BYTES;
    if envelope.len() < minimum_bytes
        || envelope.len() > maximum_bytes
        || !envelope.starts_with(ENVELOPE_HEADER)
    {
        return Err(CryptoError::MalformedEnvelope);
    }
    let nonce_start = ENVELOPE_HEADER.len();
    let ciphertext_start = nonce_start + NONCE_BYTES;
    let nonce = Nonce::from_slice(&envelope[nonce_start..ciphertext_start]);
    let cipher =
        Aes256Gcm::new_from_slice(key.expose()).map_err(|_| CryptoError::InvalidStoredKey)?;
    cipher
        .decrypt(
            nonce,
            Payload {
                msg: &envelope[ciphertext_start..],
                aad: context.as_bytes(),
            },
        )
        .map(Zeroizing::new)
        .map_err(|_| CryptoError::AuthenticationFailed)
}

#[derive(Debug, Clone)]
pub struct EngagementKeyStore<S = DefaultKeyringStore> {
    store: S,
}

impl Default for EngagementKeyStore {
    fn default() -> Self {
        Self {
            store: DefaultKeyringStore,
        }
    }
}

impl<S> EngagementKeyStore<S>
where
    S: KeyringStore,
{
    pub fn new(store: S) -> Self {
        Self { store }
    }

    pub fn load(&self, engagement_id: &str) -> Result<Option<EngagementDataKey>, CryptoError> {
        let account = key_account(engagement_id)?;
        let Some(encoded) = self.store.load(KEY_SERVICE, &account)? else {
            return Ok(None);
        };
        let encoded = Zeroizing::new(encoded);
        let decoded = STANDARD_NO_PAD
            .decode(encoded.as_bytes())
            .map_err(|_| CryptoError::InvalidStoredKey)?;
        let decoded = Zeroizing::new(decoded);
        let bytes: [u8; KEY_BYTES] = decoded
            .as_slice()
            .try_into()
            .map_err(|_| CryptoError::InvalidStoredKey)?;
        Ok(Some(EngagementDataKey::from_bytes(bytes)))
    }

    pub fn create(&self, engagement_id: &str) -> Result<EngagementDataKey, CryptoError> {
        if self.load(engagement_id)?.is_some() {
            return Err(CryptoError::KeyAlreadyExists);
        }
        let key = EngagementDataKey::generate();
        self.save(engagement_id, &key)?;
        Ok(key)
    }

    pub fn save(&self, engagement_id: &str, key: &EngagementDataKey) -> Result<(), CryptoError> {
        let account = key_account(engagement_id)?;
        let encoded = Zeroizing::new(STANDARD_NO_PAD.encode(key.expose()));
        self.store.save(KEY_SERVICE, &account, &encoded)?;
        Ok(())
    }

    pub fn delete(&self, engagement_id: &str) -> Result<bool, CryptoError> {
        let account = key_account(engagement_id)?;
        self.store.delete(KEY_SERVICE, &account).map_err(Into::into)
    }
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum CryptoError {
    #[error("encryption context component is invalid")]
    InvalidContextComponent,
    #[error("plaintext exceeds the 16 MiB record encryption limit")]
    PlaintextTooLarge,
    #[error("encrypted envelope is malformed")]
    MalformedEnvelope,
    #[error("encrypted envelope authentication failed")]
    AuthenticationFailed,
    #[error("record encryption failed")]
    EncryptionFailed,
    #[error("stored engagement data key is invalid")]
    InvalidStoredKey,
    #[error("engagement data key already exists")]
    KeyAlreadyExists,
    #[error("operating-system engagement key store failed: {0}")]
    KeyStore(String),
}

impl From<codex_keyring_store::CredentialStoreError> for CryptoError {
    fn from(error: codex_keyring_store::CredentialStoreError) -> Self {
        Self::KeyStore(error.message())
    }
}

fn key_account(engagement_id: &str) -> Result<String, CryptoError> {
    validate_component(engagement_id)?;
    Ok(format!("engagement/{engagement_id}"))
}

fn validate_component(value: &str) -> Result<(), CryptoError> {
    if value.is_empty()
        || value.len() > MAX_CONTEXT_COMPONENT_BYTES
        || value.bytes().any(|byte| byte == 0)
    {
        return Err(CryptoError::InvalidContextComponent);
    }
    Ok(())
}

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
