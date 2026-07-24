use super::*;
use pretty_assertions::assert_eq;

fn create_input(secret: &str) -> CreateAssessmentCredentialInput {
    CreateAssessmentCredentialInput {
        engagement_id: "engagement-1".to_string(),
        label: "Domain admin".to_string(),
        kind: "password".to_string(),
        username: Some("administrator".to_string()),
        domain: Some("lab.example".to_string()),
        secret: secret.to_string(),
    }
}

#[test]
fn create_reference_request_never_contains_the_secret() {
    let input = create_input("do-not-send");
    let body = create_reference_body(&input).expect("request body");
    let value: Value = serde_json::from_slice(&body).expect("request JSON");

    assert_eq!(
        value,
        json!({
            "label": "Domain admin",
            "kind": "password",
            "username": "administrator",
            "domain": "lab.example",
        })
    );
    assert!(
        !String::from_utf8(body)
            .expect("UTF-8")
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
        serde_json::from_slice::<Value>(&grant_body(&input).expect("request body"))
            .expect("request JSON"),
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
    assert_eq!(validate_credential_kind("sshKey"), Ok(()));
    assert_eq!(
        validate_credential_kind("password; rm -rf /"),
        Err(DesktopError::new(
            "invalid_credential_kind",
            "credential kind is invalid",
        ))
    );
}

#[test]
fn configured_state_is_added_without_changing_gateway_metadata() {
    let mut reference = json!({
        "id": "credential-1",
        "label": "Domain admin",
    });

    set_configured(&mut reference, false).expect("configured state");
    assert_eq!(
        reference,
        json!({
            "id": "credential-1",
            "label": "Domain admin",
            "configured": false,
        })
    );
}
