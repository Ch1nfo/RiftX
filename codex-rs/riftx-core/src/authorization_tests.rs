use super::*;
use ipnet::IpNet;
use pretty_assertions::assert_eq;

fn scope(environment: EnvironmentClass, expires_at: Option<i64>) -> AuthorizationScope {
    AuthorizationScope {
        network: Scope {
            cidrs: vec!["10.10.0.0/24".parse::<IpNet>().expect("CIDR")],
            domains: vec!["lab.example".to_string()],
            ports: vec![443],
        },
        identities: vec![IdentitySelector {
            domain: Some("lab.example".to_string()),
            tenant: None,
            account: Some("operator".to_string()),
        }],
        capabilities: vec!["network.discovery".to_string()],
        environment,
        window: AuthorizationWindow {
            starts_at: Some(100),
            expires_at,
        },
    }
}

#[test]
fn auto_requires_a_lab_scope_with_expiry() {
    assert_eq!(
        scope(EnvironmentClass::Lab, Some(200)).validate_for(ExecutionMode::Auto),
        Ok(())
    );
    assert_eq!(
        scope(EnvironmentClass::Staging, Some(200)).validate_for(ExecutionMode::Auto),
        Err(AuthorizationError::AutoRequiresLab)
    );
    assert_eq!(
        scope(EnvironmentClass::Lab, None).validate_for(ExecutionMode::Auto),
        Err(AuthorizationError::AutoRequiresExpiry)
    );
}

#[test]
fn hardened_requires_guard_but_native_does_not() {
    assert_eq!(
        [
            ExecutionMode::Native.requires_guard(),
            ExecutionMode::Hardened.requires_guard(),
            ExecutionMode::Auto.requires_guard(),
        ],
        [false, true, true]
    );
}

#[test]
fn authorization_scope_round_trips_as_a_complete_value() {
    let expected = scope(EnvironmentClass::Production, Some(200));
    let encoded = serde_json::to_string(&expected).expect("serialize scope");
    let decoded: AuthorizationScope = serde_json::from_str(&encoded).expect("deserialize scope");
    assert_eq!(decoded, expected);
}

#[test]
fn authorization_requires_explicit_capabilities() {
    let mut authorization = scope(EnvironmentClass::Lab, Some(200));
    authorization.capabilities.clear();
    assert_eq!(
        authorization.validate_for(ExecutionMode::Native),
        Err(AuthorizationError::MissingCapabilities)
    );
}
