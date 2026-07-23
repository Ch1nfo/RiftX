use super::*;

#[test]
fn effective_policy_checks_ip_domain_and_port_scope() {
    let policy = EffectivePolicy {
        execution_mode: ExecutionMode::Native,
        environment: EnvironmentClass::Lab,
        authorization_window: AuthorizationWindow {
            starts_at: None,
            expires_at: Some(200),
        },
        allowed_identities: Vec::new(),
        allowed_capabilities: BTreeSet::new(),
        allowed_cidrs: BTreeSet::from(["10.20.0.0/16".parse().expect("valid CIDR")]),
        allowed_domains: BTreeSet::from(["*.example.test".to_string()]),
        allowed_ports: BTreeSet::from([443]),
        denied_cidrs: BTreeSet::from(["10.20.30.0/24".parse().expect("valid CIDR")]),
        denied_domains: BTreeSet::new(),
        revision: "test".to_string(),
    };

    assert!(policy.check_target("10.20.1.5").is_ok());
    assert!(policy.check_target("https://api.example.test").is_ok());
    assert!(policy.check_target("10.20.30.5").is_err());
    assert!(policy.check_target("https://example.test").is_err());
    assert!(policy.check_target("http://api.example.test").is_err());
}
