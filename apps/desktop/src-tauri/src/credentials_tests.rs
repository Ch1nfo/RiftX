use super::*;
use pretty_assertions::assert_eq;
use serde_json::json;

fn create_input(secret: &str) -> CreateAssessmentCredentialInput {
    CreateAssessmentCredentialInput {
        engagement_id: "engagement-1".to_string(),
        label: "Domain admin".to_string(),
        kind: CredentialKind::Password,
        username: Some("administrator".to_string()),
        domain: Some("lab.example".to_string()),
        secret: secret.to_string(),
    }
}

fn grant(id: &str, credential_id: &str, revoked_at: Option<i64>) -> CredentialGrant {
    CredentialGrant {
        id: id.to_string(),
        engagement_id: "engagement-1".to_string(),
        credential_id: credential_id.to_string(),
        allowed_targets: Scope {
            cidrs: vec!["10.10.0.0/24".parse().expect("CIDR")],
            domains: Vec::new(),
            ports: Vec::new(),
        },
        allowed_capabilities: vec!["credential-use".to_string()],
        max_uses: 1,
        max_failures_per_identity: 1,
        starts_at: None,
        expires_at: 2_000_000_000,
        created_at: 1,
        revoked_at,
    }
}

#[test]
fn create_reference_request_never_contains_the_secret() {
    let input = create_input("do-not-send");
    let params = create_reference_params(&input);

    assert_eq!(
        params,
        CreateCredentialReferenceParams {
            label: "Domain admin".to_string(),
            kind: CredentialKind::Password,
            username: Some("administrator".to_string()),
            domain: Some("lab.example".to_string()),
        }
    );
    assert!(
        !serde_json::to_string(&params)
            .expect("request JSON")
            .contains("do-not-send")
    );
}

#[test]
fn grant_request_includes_explicit_scope_and_limits() {
    let input = CreateCredentialGrantInput {
        engagement_id: "engagement-1".to_string(),
        credential_id: "credential-1".to_string(),
        cidrs: vec!["10.10.0.0/24".to_string()],
        domains: vec!["lab.example".to_string()],
        ports: vec![443],
        capabilities: vec!["credential-use".to_string()],
        max_uses: 4,
        max_failures_per_identity: 2,
        starts_at: None,
        expires_at: 2_000_000_000,
    };

    assert_eq!(
        serde_json::to_value(grant_params(&input).expect("grant params")).expect("request JSON"),
        json!({
            "credentialId": "credential-1",
            "allowedTargets": {
                "cidrs": ["10.10.0.0/24"],
                "domains": ["lab.example"],
                "ports": [443],
            },
            "allowedCapabilities": ["credential-use"],
            "maxUses": 4,
            "maxFailuresPerIdentity": 2,
            "startsAt": null,
            "expiresAt": 2_000_000_000_i64,
        })
    );
}

#[test]
fn credential_kind_is_closed_and_typed() {
    assert_eq!(
        serde_json::from_str::<CredentialKind>(r#""sshKey""#).expect("credential kind"),
        CredentialKind::SshKey
    );
    assert!(serde_json::from_str::<CredentialKind>(r#""password; rm -rf /""#).is_err());
}

#[test]
fn secret_removal_selects_all_historical_grants_for_revocation() {
    let grants = vec![
        grant("grant-1", "credential-1", None),
        grant("grant-2", "credential-2", None),
        grant("grant-3", "credential-1", Some(123)),
    ];

    assert_eq!(
        grants_for_credential(&grants, "credential-1")
            .iter()
            .map(|grant| grant.id.as_str())
            .collect::<Vec<_>>(),
        vec!["grant-1", "grant-3"]
    );
}
