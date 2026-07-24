use super::*;
use ipnet::IpNet;
use pretty_assertions::assert_eq;

#[test]
fn credential_reference_round_trips_without_a_secret() {
    let reference = CredentialReference {
        id: "corp-test-user".to_string(),
        engagement_id: "engagement-1".to_string(),
        label: "Corporate test user".to_string(),
        kind: CredentialKind::Password,
        storage_key: "riftx/engagement-1/corp-test-user".to_string(),
        username: Some("test.user".to_string()),
        domain: Some("LAB".to_string()),
        created_at: 100,
    };

    reference.validate().expect("reference should be valid");
    let encoded = serde_json::to_value(&reference).expect("reference should encode");
    let decoded: CredentialReference =
        serde_json::from_value(encoded.clone()).expect("reference should decode");

    assert_eq!(decoded, reference);
    assert_eq!(
        encoded,
        serde_json::json!({
            "id": "corp-test-user",
            "engagementId": "engagement-1",
            "label": "Corporate test user",
            "kind": "password",
            "storageKey": "riftx/engagement-1/corp-test-user",
            "username": "test.user",
            "domain": "LAB",
            "createdAt": 100
        })
    );
}

#[test]
fn credential_grant_requires_bounded_use_and_time() {
    let mut grant = credential_grant();
    grant.max_uses = 0;
    assert_eq!(grant.validate(), Err(CredentialError::ZeroMaxUses));

    grant.max_uses = 10;
    grant.starts_at = Some(200);
    grant.expires_at = 200;
    assert_eq!(grant.validate(), Err(CredentialError::InvalidWindow));
}

#[test]
fn credential_grant_round_trips_as_a_complete_value() {
    let grant = credential_grant();
    grant.validate().expect("grant should be valid");

    let encoded = serde_json::to_vec(&grant).expect("grant should encode");
    let decoded: CredentialGrant = serde_json::from_slice(&encoded).expect("grant should decode");

    assert_eq!(decoded, grant);
}

#[test]
fn credential_grant_requires_a_target_and_tracks_revocation() {
    let mut grant = credential_grant();
    grant.allowed_targets = Scope {
        cidrs: Vec::new(),
        domains: Vec::new(),
        ports: vec![445],
    };
    assert_eq!(grant.validate(), Err(CredentialError::MissingTarget));

    grant = credential_grant();
    assert!(grant.is_active_at(150));
    grant.revoked_at = Some(160);
    assert!(!grant.is_active_at(170));
    grant.revoked_at = Some(99);
    assert_eq!(grant.validate(), Err(CredentialError::InvalidRevocation));
}

fn credential_grant() -> CredentialGrant {
    CredentialGrant {
        id: "grant-1".to_string(),
        engagement_id: "engagement-1".to_string(),
        credential_id: "corp-test-user".to_string(),
        allowed_targets: Scope {
            cidrs: vec!["10.10.20.0/24".parse::<IpNet>().expect("valid CIDR")],
            domains: vec!["lab.example.test".to_string()],
            ports: vec![22, 445],
        },
        allowed_capabilities: vec!["credentialValidation".to_string()],
        max_uses: 10,
        max_failures_per_identity: 2,
        starts_at: Some(100),
        expires_at: 200,
        created_at: 100,
        revoked_at: None,
    }
}
