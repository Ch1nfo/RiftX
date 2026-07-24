use super::*;
use clap::Parser;
use pretty_assertions::assert_eq;

#[test]
fn credential_add_has_no_secret_argument() {
    let parsed = crate::Cli::try_parse_from([
        "riftx",
        "credentials",
        "add",
        "engagement-1",
        "--label",
        "Domain admin",
        "--kind",
        "password",
        "--secret-stdin",
    ])
    .expect("credential command");

    let crate::Command::Credentials {
        command:
            CredentialCommand::Add {
                id,
                label,
                kind,
                username,
                domain,
                secret_stdin,
            },
    } = parsed.command
    else {
        panic!("expected credential add command");
    };
    assert_eq!(id, "engagement-1");
    assert_eq!(label, "Domain admin");
    assert_eq!(kind.as_str(), "password");
    assert_eq!(username, None);
    assert_eq!(domain, None);
    assert!(secret_stdin);
}

#[test]
fn credential_grant_parses_explicit_safety_limits() {
    let parsed = crate::Cli::try_parse_from([
        "riftx",
        "credentials",
        "grant",
        "engagement-1",
        "credential-1",
        "--cidr",
        "10.10.0.0/24",
        "--capability",
        "credential-use",
        "--max-uses",
        "4",
        "--max-failures-per-identity",
        "2",
        "--expires-at",
        "2000000000",
    ])
    .expect("credential grant command");

    let crate::Command::Credentials {
        command:
            CredentialCommand::Grant {
                max_uses,
                max_failures_per_identity,
                expires_at,
                ..
            },
    } = parsed.command
    else {
        panic!("expected credential grant command");
    };
    assert_eq!(max_uses, 4);
    assert_eq!(max_failures_per_identity, 2);
    assert_eq!(expires_at, 2_000_000_000);
}

#[test]
fn entity_lookup_returns_only_the_requested_credential() {
    let references = json!([
        {"id": "credential-1", "label": "first"},
        {"id": "credential-2", "label": "second"},
    ]);

    assert_eq!(
        entity_by_id(&references, "credential-2", "credential").expect("credential"),
        json!({"id": "credential-2", "label": "second"})
    );
    assert!(entity_by_id(&references, "../credential", "credential").is_err());
}

#[test]
fn deleting_a_secret_selects_every_grant_for_revocation() {
    let grants = json!([
        {"id": "grant-1", "credentialId": "credential-1", "revokedAt": null},
        {"id": "grant-2", "credentialId": "credential-2", "revokedAt": null},
        {"id": "grant-3", "credentialId": "credential-1", "revokedAt": 123},
    ]);

    assert_eq!(
        grants_for_credential(&grants, "credential-1")
            .iter()
            .map(|grant| grant["id"].as_str().expect("grant id"))
            .collect::<Vec<_>>(),
        vec!["grant-1", "grant-3"]
    );
}
