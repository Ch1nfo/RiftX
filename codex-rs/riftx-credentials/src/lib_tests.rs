use super::*;
use codex_keyring_store::tests::MockKeyringStore;
use pretty_assertions::assert_eq;

#[test]
fn api_key_debug_output_is_redacted() {
    let api_key = LlmApiKey::new("riftx-secret".to_string()).expect("API key");
    let debug = format!("{api_key:?}");

    assert!(debug.contains("[REDACTED]"));
    assert!(!debug.contains("riftx-secret"));
}

#[test]
fn credential_store_round_trips_and_deletes_a_profile() {
    let backend = MockKeyringStore::default();
    let store = LlmCredentialStore::new(backend);

    assert_eq!(store.load("default").expect("load missing key"), None);
    store
        .save(
            "default",
            LlmApiKey::new("saved-key".to_string()).expect("API key"),
        )
        .expect("save key");
    assert_eq!(
        store.load("default").expect("load key"),
        Some(LlmApiKey::new("saved-key".to_string()).expect("API key"))
    );
    assert!(store.delete("default").expect("delete key"));
    assert_eq!(store.load("default").expect("load deleted key"), None);
}

#[test]
fn credential_store_rejects_unsafe_profile_names() {
    let store = LlmCredentialStore::new(MockKeyringStore::default());

    assert!(matches!(
        store.load("../default"),
        Err(CredentialError::InvalidProfile)
    ));
}
