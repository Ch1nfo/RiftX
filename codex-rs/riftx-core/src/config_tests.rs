use super::*;
use pretty_assertions::assert_eq;

#[test]
fn strict_config_rejects_unknown_fields() {
    let input = r#"
[gateway]
listen = "127.0.0.1:8787"
operator_token_env = "RIFTX_OPERATOR_TOKEN"
state_db = "state.sqlite"
unexpected = true
"#;
    let error = toml::from_str::<RiftxConfig>(input).expect_err("unknown field should fail");
    assert!(error.to_string().contains("unknown field `unexpected`"));
}

#[test]
fn policy_layers_only_reduce_access() {
    let managed = ManagedPolicyConfig {
        allowed_tools: vec!["rt_nmap".to_string(), "rt_httpx".to_string()],
        denied_cidrs: Vec::new(),
        denied_domains: Vec::new(),
    };
    let scope = Scope {
        cidrs: vec!["10.0.0.0/24".parse().expect("CIDR")],
        domains: vec!["target.local".to_string()],
        ports: vec![80, 443],
    };
    let profile = ToolProfileConfig {
        allowed_tools: vec!["rt_nmap".to_string(), "rt_ffuf".to_string()],
        scope: Scope {
            cidrs: vec!["0.0.0.0/0".parse().expect("CIDR")],
            domains: vec!["*".to_string()],
            ports: Vec::new(),
        },
        approval: ApprovalMode::HighRisk,
    };
    let effective =
        EffectivePolicy::resolve(&managed, &scope, &profile, None).expect("valid policy");
    assert_eq!(effective.allowed_tools, ["rt_nmap".to_string()].into());
    assert_eq!(effective.allowed_cidrs, scope.cidrs.into_iter().collect());
    assert!(
        effective
            .denied_cidrs
            .contains(&"169.254.0.0/16".parse().expect("CIDR"))
    );
    assert_eq!(effective.revision.len(), 64);
}
