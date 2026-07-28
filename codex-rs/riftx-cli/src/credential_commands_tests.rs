use super::*;
use clap::Parser;
use pretty_assertions::assert_eq;

fn reference(id: &str) -> CredentialReference {
    CredentialReference {
        id: id.to_string(),
        engagement_id: "engagement-1".to_string(),
        label: id.to_string(),
        kind: CredentialKind::Password,
        storage_key: format!("engagement/engagement-1/credential/{id}"),
        username: None,
        domain: None,
        configured: true,
        created_at: 1,
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
    assert_eq!(CredentialKind::from(kind), CredentialKind::Password);
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
    let references = vec![reference("credential-1"), reference("credential-2")];

    assert_eq!(
        credential_by_id(&references, "credential-2").expect("credential"),
        &references[1]
    );
    assert!(credential_by_id(&references, "../credential").is_err());
}

#[test]
fn deleting_a_secret_selects_every_grant_for_revocation() {
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
