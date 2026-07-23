use super::*;
use pretty_assertions::assert_eq;

#[test]
fn strict_config_rejects_unknown_fields() {
    let input = r#"
[daemon]
ipc_dir = ".riftx/ipc"
state_db = "state.sqlite"
runtime_home = "runtime"
workspace_root = "workspaces"
unexpected = true

[llm]
model = "test-model"
base_url = "http://127.0.0.1:8766/v1"
api_key_env = "RIFTX_TEST_API_KEY"
"#;
    let error = toml::from_str::<RiftxConfig>(input).expect_err("unknown field should fail");
    assert!(error.to_string().contains("unknown field `unexpected`"));
}

#[test]
fn llm_config_accepts_https_and_loopback_but_rejects_remote_http() {
    for base_url in [
        "https://llm.example.test/v1",
        "http://127.0.0.1:8766/v1",
        "http://[::1]:8766/v1",
    ] {
        LlmConfig {
            model: "riftx-model".to_string(),
            base_url: base_url.to_string(),
            api_key_env: "RIFTX_LLM_API_KEY".to_string(),
        }
        .validate()
        .expect("valid LLM config");
    }

    let error = LlmConfig {
        model: "riftx-model".to_string(),
        base_url: "http://llm.example.test/v1".to_string(),
        api_key_env: "RIFTX_LLM_API_KEY".to_string(),
    }
    .validate()
    .expect_err("remote HTTP must be rejected");
    assert_eq!(
        error.to_string(),
        "invalid RiftX config: llm.base_url must use HTTPS; HTTP is allowed only for loopback development endpoints"
    );
}

#[test]
fn policy_layers_only_reduce_access() {
    let managed = ManagedPolicyConfig {
        allowed_capabilities: vec![
            "network.discovery".to_string(),
            "service.enumeration".to_string(),
        ],
        denied_cidrs: Vec::new(),
        denied_domains: Vec::new(),
    };
    let authorization = AuthorizationScope {
        network: Scope {
            cidrs: vec!["10.0.0.0/24".parse().expect("CIDR")],
            domains: vec!["target.local".to_string()],
            ports: vec![80, 443],
        },
        identities: Vec::new(),
        capabilities: vec![
            "network.discovery".to_string(),
            "content.discovery".to_string(),
        ],
        environment: EnvironmentClass::Lab,
        window: AuthorizationWindow {
            starts_at: None,
            expires_at: Some(200),
        },
    };
    let effective = EffectivePolicy::resolve(&managed, ExecutionMode::Native, &authorization, None)
        .expect("valid policy");
    assert_eq!(
        effective.allowed_capabilities,
        ["network.discovery".to_string()].into()
    );
    assert_eq!(
        effective.allowed_cidrs,
        authorization.network.cidrs.into_iter().collect()
    );
    assert!(
        effective
            .denied_cidrs
            .contains(&"169.254.0.0/16".parse().expect("CIDR"))
    );
    assert_eq!(effective.revision.len(), 64);
}
