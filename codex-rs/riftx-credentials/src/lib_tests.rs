use super::*;
use codex_keyring_store::tests::MockKeyringStore;
use pretty_assertions::assert_eq;

#[test]
fn api_key_debug_output_is_redacted() {
    let api_key = LlmApiKey::new("  riftx-secret\n".to_string()).expect("API key");
    let debug = format!("{api_key:?}");

    assert_eq!(api_key.into_inner(), "riftx-secret");
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

#[test]
fn assessment_secret_debug_output_is_redacted_without_changing_whitespace() {
    let secret = AssessmentSecret::new("  password\n".to_string()).expect("assessment secret");
    let debug = format!("{secret:?}");

    assert_eq!(secret.into_inner(), "  password\n");
    assert!(debug.contains("[REDACTED]"));
    assert!(!debug.contains("password"));
}

#[test]
fn assessment_store_round_trips_and_deletes_a_credential() {
    let backend = MockKeyringStore::default();
    let store = AssessmentCredentialStore::new(backend);
    let locator = CredentialLocator::new("engagement-1", "credential-1").expect("locator");

    assert_eq!(store.load(&locator).expect("load missing secret"), None);
    store
        .save(
            &locator,
            AssessmentSecret::new("saved-secret".to_string()).expect("secret"),
        )
        .expect("save secret");
    assert_eq!(
        store.load(&locator).expect("load secret"),
        Some(AssessmentSecret::new("saved-secret".to_string()).expect("secret"))
    );
    assert!(store.delete(&locator).expect("delete secret"));
    assert_eq!(store.load(&locator).expect("load deleted secret"), None);
}

#[test]
fn credential_locator_rejects_unsafe_identifiers() {
    assert!(matches!(
        CredentialLocator::new("../engagement", "credential"),
        Err(CredentialError::InvalidEngagementId)
    ));
    assert!(matches!(
        CredentialLocator::new("engagement", "../credential"),
        Err(CredentialError::InvalidCredentialId)
    ));
}
