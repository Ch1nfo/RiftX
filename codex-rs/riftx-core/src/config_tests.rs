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
api_key = { source = "environment", variable = "RIFTX_TEST_API_KEY" }
"#;
    let error = toml::from_str::<RiftxConfig>(input).expect_err("unknown field should fail");
    assert!(error.to_string().contains("unknown field `unexpected`"));
}

#[tokio::test]
async fn config_paths_are_resolved_relative_to_the_config_file() {
    let temp = tempfile::tempdir().expect("tempdir");
    let config_path = temp.path().join("riftx.toml");
    std::fs::write(
        &config_path,
        r#"
[daemon]
ipc_dir = ".riftx/ipc"
state_db = ".riftx/state.sqlite"
runtime_home = ".riftx/runtime"
workspace_root = ".riftx/workspaces"

[skills]
directory = "skills"

[tools]
directories = ["tools"]
extra_paths = ["extra-tools"]

[llm]
model = "test-model"
base_url = "http://127.0.0.1:8766/v1"
api_key = { source = "keyring", profile = "default" }

[policy]
allowed_capabilities = []
denied_cidrs = []
denied_domains = []

[audit]
jsonl_path = ".riftx/audit.jsonl"
fsync = false

[artifacts]
root = ".riftx/artifacts"
max_bytes_per_engagement = 1024
"#,
    )
    .expect("write config");

    let config = RiftxConfig::load_resolved(&config_path)
        .await
        .expect("load config");

    assert_eq!(
        (
            config.daemon.ipc_dir,
            config.daemon.workspace_root,
            config.skills.directory,
            config.tools.directories,
            config.tools.extra_paths,
            config.audit.jsonl_path,
            config.artifacts.root,
        ),
        (
            temp.path().join(".riftx/ipc"),
            temp.path().join(".riftx/workspaces"),
            Some(temp.path().join("skills")),
            vec![temp.path().join("tools")],
            vec![temp.path().join("extra-tools")],
            temp.path().join(".riftx/audit.jsonl"),
            temp.path().join(".riftx/artifacts"),
        )
    );
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
            api_key: LlmApiKeySource::Keyring {
                profile: "default".to_string(),
            },
        }
        .validate()
        .expect("valid LLM config");
    }

    let error = LlmConfig {
        model: "riftx-model".to_string(),
        base_url: "http://llm.example.test/v1".to_string(),
        api_key: LlmApiKeySource::Environment {
            variable: "RIFTX_LLM_API_KEY".to_string(),
        },
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
