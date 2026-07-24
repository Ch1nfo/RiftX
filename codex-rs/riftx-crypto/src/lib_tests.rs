use super::*;
use codex_keyring_store::tests::MockKeyringStore;
use pretty_assertions::assert_eq;

#[test]
fn envelope_round_trip_is_bound_to_engagement_and_record() {
    let key = EngagementDataKey::generate();
    let context =
        EncryptionContext::new("engagement-1", "evidence", "evidence-1").expect("context");
    let plaintext = b"authorized assessment evidence";

    let first = seal(&key, &context, plaintext).expect("first envelope");
    let second = seal(&key, &context, plaintext).expect("second envelope");

    assert_ne!(first, second);
    assert_eq!(
        open(&key, &context, &first).expect("decrypted").as_slice(),
        plaintext
    );
    let wrong_context =
        EncryptionContext::new("engagement-1", "evidence", "evidence-2").expect("context");
    assert_eq!(
        open(&key, &wrong_context, &first),
        Err(CryptoError::AuthenticationFailed)
    );
}

#[test]
fn envelope_rejects_tampering_and_malformed_input() {
    let key = EngagementDataKey::generate();
    let context = EncryptionContext::new("engagement-1", "finding", "finding-1").expect("context");
    let mut envelope = seal(&key, &context, b"validated finding").expect("envelope");
    let last = envelope.len() - 1;
    envelope[last] ^= 0x01;

    assert_eq!(
        open(&key, &context, &envelope),
        Err(CryptoError::AuthenticationFailed)
    );
    assert_eq!(
        open(&key, &context, b"not-an-envelope"),
        Err(CryptoError::MalformedEnvelope)
    );
}

#[test]
fn engagement_key_store_round_trips_and_deletes_without_exposing_the_key() {
    let backend = MockKeyringStore::default();
    let store = EngagementKeyStore::new(backend);
    let key = store.create("engagement-1").expect("engagement key");
    assert!(matches!(
        store.create("engagement-1"),
        Err(CryptoError::KeyAlreadyExists)
    ));
    let loaded = store
        .load("engagement-1")
        .expect("stored key")
        .expect("key exists");
    let context = EncryptionContext::new("engagement-1", "asset", "asset-1").expect("context");
    let envelope = seal(&key, &context, b"asset state").expect("envelope");

    assert_eq!(
        open(&loaded, &context, &envelope)
            .expect("decrypted")
            .as_slice(),
        b"asset state"
    );
    assert_eq!(format!("{key:?}"), "EngagementDataKey([REDACTED])");
    assert!(store.delete("engagement-1").expect("delete"));
    assert!(store.load("engagement-1").expect("missing key").is_none());
}

#[test]
fn context_and_key_storage_inputs_are_bounded() {
    assert_eq!(
        EncryptionContext::new("", "asset", "asset-1"),
        Err(CryptoError::InvalidContextComponent)
    );
    assert_eq!(
        EncryptionContext::new("engagement-1", &"x".repeat(257), "asset-1"),
        Err(CryptoError::InvalidContextComponent)
    );

    let backend = MockKeyringStore::default();
    backend
        .save(
            "com.riftx.engagement-key",
            "engagement/engagement-1",
            "not-a-key",
        )
        .expect("invalid key fixture");
    assert!(matches!(
        EngagementKeyStore::new(backend).load("engagement-1"),
        Err(CryptoError::InvalidStoredKey)
    ));
}

#[test]
fn record_cipher_caches_existing_keys_and_never_recreates_missing_keys() {
    let backend = MockKeyringStore::default();
    let first = KeyringEngagementCipher::new(backend.clone());
    first
        .create_engagement("engagement-1")
        .expect("create engagement key");
    let envelope = first
        .seal_record("engagement-1", "observation", "observation-1", b"state")
        .expect("sealed state");
    let reopened = KeyringEngagementCipher::new(backend.clone());
    reopened
        .prepare_engagement("engagement-1")
        .expect("prepare existing key");

    assert_eq!(
        reopened
            .open_record("engagement-1", "observation", "observation-1", &envelope,)
            .expect("open state")
            .as_slice(),
        b"state"
    );
    assert_eq!(
        reopened.prepare_engagement("missing-engagement"),
        Err(CryptoError::KeyMissing)
    );
    assert!(
        reopened
            .delete_engagement("engagement-1")
            .expect("delete key")
    );
    assert_eq!(
        KeyringEngagementCipher::new(backend).prepare_engagement("engagement-1"),
        Err(CryptoError::KeyMissing)
    );
}
