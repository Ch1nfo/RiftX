use super::*;
use crate::CredentialKind;
use crate::Scope;
use pretty_assertions::assert_eq;

#[test]
fn request_validation_and_target_matching_are_strict() {
    let grant = grant();
    let allowed_request = request("10.10.0.42", Some(445));

    assert_eq!(allowed_request.validate(), Ok(()));
    assert!(grant_allows_target(&grant, &allowed_request.target));
    assert!(grant_allows_target(
        &grant,
        &CredentialUseTarget {
            host: "host.lab.example".to_string(),
            port: Some(445),
        }
    ));
    assert!(!grant_allows_target(
        &grant,
        &CredentialUseTarget {
            host: "10.20.0.1".to_string(),
            port: Some(445),
        }
    ));
    assert!(!grant_allows_target(
        &grant,
        &CredentialUseTarget {
            host: "lab.example".to_string(),
            port: Some(445),
        }
    ));
    assert!(!grant_allows_target(
        &grant,
        &CredentialUseTarget {
            host: "host.lab.example".to_string(),
            port: None,
        }
    ));
    let mut invalid_domain = request("host name.lab.example", Some(445));
    assert_eq!(
        invalid_domain.validate(),
        Err(CredentialUseError::InvalidTarget)
    );
    invalid_domain.target.host = "-host.lab.example".to_string();
    assert_eq!(
        invalid_domain.validate(),
        Err(CredentialUseError::InvalidTarget)
    );
    let invalid_port = request("host.lab.example", Some(0));
    assert_eq!(
        invalid_port.validate(),
        Err(CredentialUseError::InvalidTarget)
    );
}

#[test]
fn identity_hash_is_deterministic_and_excludes_plaintext() {
    let reference = CredentialReference {
        id: "credential-1".to_string(),
        engagement_id: "engagement-1".to_string(),
        label: "Lab user".to_string(),
        kind: CredentialKind::Password,
        storage_key: "engagement/engagement-1/credential/credential-1".to_string(),
        username: Some("Administrator".to_string()),
        domain: Some("LAB.EXAMPLE".to_string()),
        configured: true,
        created_at: 100,
    };
    let hash = identity_hash(&reference);
    let normalized = CredentialReference {
        username: Some("administrator".to_string()),
        domain: Some("lab.example".to_string()),
        ..reference
    };

    assert_eq!(hash, identity_hash(&normalized));
    assert_eq!(hash.len(), 64);
    assert!(!hash.contains("administrator"));
    assert!(!hash.contains("lab.example"));
}

fn request(host: &str, port: Option<u16>) -> CredentialUseRequest {
    CredentialUseRequest {
        id: "use-1".to_string(),
        engagement_id: "engagement-1".to_string(),
        grant_id: "grant-1".to_string(),
        target: CredentialUseTarget {
            host: host.to_string(),
            port,
        },
        capability: "credential.testing".to_string(),
        policy_revision: "a".repeat(64),
        requested_at: 150,
    }
}

fn grant() -> CredentialGrant {
    CredentialGrant {
        id: "grant-1".to_string(),
        engagement_id: "engagement-1".to_string(),
        credential_id: "credential-1".to_string(),
        allowed_targets: Scope {
            cidrs: vec!["10.10.0.0/24".parse().expect("CIDR")],
            domains: vec!["*.lab.example".to_string()],
            ports: vec![445],
        },
        allowed_capabilities: vec!["credential.testing".to_string()],
        max_uses: 3,
        max_failures_per_identity: 2,
        starts_at: Some(100),
        expires_at: 200,
        created_at: 100,
        revoked_at: None,
    }
}
