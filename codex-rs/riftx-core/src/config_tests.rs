use super::*;
use pretty_assertions::assert_eq;
use std::collections::BTreeMap;
use std::path::PathBuf;

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
default_profile = "default"

[llm.profiles.default]
model = "test-model"
base_url = "http://127.0.0.1:8766/v1"
api_key = { source = "environment", variable = "RIFTX_TEST_API_KEY" }
timeout_seconds = 300
reasoning_level = "high"
context_budget = 200000
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
default_profile = "default"

[llm.profiles.default]
model = "test-model"
base_url = "http://127.0.0.1:8766/v1"
api_key = { source = "keyring", credential = "default" }
timeout_seconds = 300
reasoning_level = "high"
context_budget = 200000

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
        let profile = LlmProfileConfig {
            enabled: true,
            protocol: LlmProtocol::Responses,
            model: "riftx-model".to_string(),
            base_url: base_url.to_string(),
            api_key: LlmApiKeySource::Keyring {
                credential: "default".to_string(),
            },
            timeout_seconds: 300,
            reasoning_level: LlmReasoningLevel::High,
            context_budget: 200_000,
        };
        LlmConfig {
            config_version: LLM_CONFIG_VERSION,
            default_profile: "default".to_string(),
            profiles: BTreeMap::from([("default".to_string(), profile)]),
        }
        .validate()
        .expect("valid LLM config");
    }

    let profile = LlmProfileConfig {
        enabled: true,
        protocol: LlmProtocol::Responses,
        model: "riftx-model".to_string(),
        base_url: "http://llm.example.test/v1".to_string(),
        api_key: LlmApiKeySource::Environment {
            variable: "RIFTX_LLM_API_KEY".to_string(),
        },
        timeout_seconds: 300,
        reasoning_level: LlmReasoningLevel::High,
        context_budget: 200_000,
    };
    let error = LlmConfig {
        config_version: LLM_CONFIG_VERSION,
        default_profile: "default".to_string(),
        profiles: BTreeMap::from([("default".to_string(), profile)]),
    }
    .validate()
    .expect_err("remote HTTP must be rejected");
    assert_eq!(
        error.to_string(),
        "invalid RiftX config: llm.profiles.default.base_url must use HTTPS; HTTP is allowed only for loopback development endpoints"
    );
}

#[test]
fn llm_config_requires_an_existing_default_profile() {
    let config = LlmConfig {
        config_version: LLM_CONFIG_VERSION,
        default_profile: "missing".to_string(),
        profiles: BTreeMap::from([(
            "available".to_string(),
            LlmProfileConfig {
                enabled: true,
                protocol: LlmProtocol::Responses,
                model: "riftx-model".to_string(),
                base_url: "https://llm.example.test/v1".to_string(),
                api_key: LlmApiKeySource::Keyring {
                    credential: "available".to_string(),
                },
                timeout_seconds: 300,
                reasoning_level: LlmReasoningLevel::Medium,
                context_budget: 200_000,
            },
        )]),
    };

    assert_eq!(
        config
            .validate()
            .expect_err("missing default profile")
            .to_string(),
        "invalid RiftX config: llm.default_profile \"missing\" does not exist in llm.profiles"
    );
}

#[test]
fn llm_config_bounds_the_number_of_runtime_profiles() {
    let profile = LlmProfileConfig {
        enabled: true,
        protocol: LlmProtocol::Responses,
        model: "riftx-model".to_string(),
        base_url: "https://llm.example.test/v1".to_string(),
        api_key: LlmApiKeySource::Environment {
            variable: "RIFTX_LLM_API_KEY".to_string(),
        },
        timeout_seconds: 300,
        reasoning_level: LlmReasoningLevel::Medium,
        context_budget: 200_000,
    };
    let profiles = (0..17)
        .map(|index| (format!("profile-{index}"), profile.clone()))
        .collect();
    let config = LlmConfig {
        config_version: LLM_CONFIG_VERSION,
        default_profile: "profile-0".to_string(),
        profiles,
    };

    assert_eq!(
        config
            .validate()
            .expect_err("too many profiles")
            .to_string(),
        "invalid RiftX config: llm.profiles must define between 1 and 16 profiles"
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
    let effective =
        EffectivePolicy::resolve(&managed, ExecutionMode::Pentest, &authorization, None)
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

#[tokio::test]
async fn write_atomic_replaces_config_without_leaving_tmp() {
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

[tools]
directories = []
extra_paths = []

[skills]

[llm]
default_profile = "default"

[llm.profiles.default]
model = "test-model"
base_url = "http://127.0.0.1:8766/v1"
api_key = { source = "environment", variable = "RIFTX_TEST_API_KEY" }
timeout_seconds = 300
reasoning_level = "high"
context_budget = 200000

[policy]
allowed_capabilities = ["network.discovery"]
denied_cidrs = []
denied_domains = []

[audit]
jsonl_path = ".riftx/audit.jsonl"
fsync = true

[artifacts]
root = ".riftx/artifacts"
max_bytes_per_engagement = 1073741824
"#,
    )
    .expect("seed config");

    let mut config = RiftxConfig::load(&config_path).await.expect("load");
    config.llm.default_profile = "default".to_string();
    config
        .llm
        .profiles
        .get_mut("default")
        .expect("profile")
        .model = "updated".to_string();
    config
        .write_atomic(&config_path)
        .await
        .expect("atomic write");

    let reloaded = RiftxConfig::load(&config_path).await.expect("reload");
    assert_eq!(reloaded.llm.profiles["default"].model, "updated");
    assert!(!config_path.with_extension("toml.tmp").exists());
    let mut tmp_name = config_path.as_os_str().to_owned();
    tmp_name.push(".tmp");
    assert!(!PathBuf::from(tmp_name).exists());
}

#[tokio::test]
async fn missing_protocol_defaults_to_responses_and_migrates_once() {
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

[tools]
directories = []
extra_paths = []

[skills]

[llm]
default_profile = "default"

[llm.profiles.default]
model = "test-model"
base_url = "http://127.0.0.1:8766/v1"
api_key = { source = "environment", variable = "RIFTX_TEST_API_KEY" }
timeout_seconds = 300
reasoning_level = "high"
context_budget = 200000

[policy]
allowed_capabilities = ["network.discovery"]
denied_cidrs = []
denied_domains = []

[audit]
jsonl_path = ".riftx/audit.jsonl"
fsync = true

[artifacts]
root = ".riftx/artifacts"
max_bytes_per_engagement = 1073741824
"#,
    )
    .expect("seed legacy config");

    let migrated = RiftxConfig::load_migrating(&config_path)
        .await
        .expect("migrate");
    assert_eq!(migrated.llm.config_version, LLM_CONFIG_VERSION);
    assert_eq!(
        migrated.llm.profiles["default"].protocol,
        LlmProtocol::Responses
    );
    let content = std::fs::read_to_string(&config_path).expect("read");
    assert!(content.contains("config_version = 2"));
    assert!(content.contains("enabled = true"));
    assert!(content.contains("protocol = \"responses\""));

    let again = RiftxConfig::load_migrating(&config_path)
        .await
        .expect("idempotent");
    assert_eq!(again.llm.config_version, LLM_CONFIG_VERSION);
}

#[test]
fn unknown_protocol_is_rejected() {
    let input = r#"
[daemon]
ipc_dir = ".riftx/ipc"
state_db = "state.sqlite"
runtime_home = "runtime"
workspace_root = "workspaces"

[llm]
default_profile = "default"
config_version = 1

[llm.profiles.default]
protocol = "websocket"
model = "test-model"
base_url = "http://127.0.0.1:8766/v1"
api_key = { source = "environment", variable = "RIFTX_TEST_API_KEY" }
timeout_seconds = 300
reasoning_level = "high"
context_budget = 200000

[tools]
directories = []
extra_paths = []

[policy]
allowed_capabilities = []
denied_cidrs = []
denied_domains = []

[audit]
jsonl_path = "audit.jsonl"
fsync = true

[artifacts]
root = "artifacts"
max_bytes_per_engagement = 1
"#;
    assert!(toml::from_str::<RiftxConfig>(input).is_err());
}

#[test]
fn llm_config_rejects_a_disabled_default_profile() {
    let config = LlmConfig {
        config_version: LLM_CONFIG_VERSION,
        default_profile: "default".to_string(),
        profiles: BTreeMap::from([(
            "default".to_string(),
            LlmProfileConfig {
                enabled: false,
                protocol: LlmProtocol::Responses,
                model: "riftx-model".to_string(),
                base_url: "https://llm.example.test/v1".to_string(),
                api_key: LlmApiKeySource::Keyring {
                    credential: "default".to_string(),
                },
                timeout_seconds: 300,
                reasoning_level: LlmReasoningLevel::High,
                context_budget: 200_000,
            },
        )]),
    };

    assert_eq!(
        config.validate().expect_err("disabled default").to_string(),
        "invalid RiftX config: llm.default_profile \"default\" cannot be disabled"
    );
}
