use super::*;
use pretty_assertions::assert_eq;

fn sample_config_toml() -> &'static str {
    r#"
[daemon]
ipc_dir = ".riftx/ipc"
state_db = ".riftx/state.sqlite"
runtime_home = ".riftx/runtime"
workspace_root = ".riftx/workspaces"

[tools]
directories = ["tools"]
extra_paths = []

[skills]

[llm]
default_profile = "openai"

[llm.profiles.openai]
model = "gpt-5.2"
base_url = "https://api.openai.com/v1"
api_key = { source = "keyring", credential = "openai" }
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
"#
}

#[tokio::test]
async fn settings_view_lists_every_profile_in_name_order() {
    let temp = tempfile::tempdir().expect("temp dir");
    let path = temp.path().join("riftx.toml");
    tokio::fs::write(
        &path,
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
default_profile = "zeta"

[llm.profiles.alpha]
model = "model-a"
base_url = "https://alpha.example.test/v1"
api_key = { source = "environment", variable = "RIFTX_SETTINGS_ALPHA_UNSET" }
timeout_seconds = 60
reasoning_level = "medium"
context_budget = 64000

[llm.profiles.zeta]
model = "model-z"
base_url = "https://zeta.example.test/v1"
api_key = { source = "environment", variable = "RIFTX_SETTINGS_ZETA_UNSET" }
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
    .await
    .expect("write config");

    let config = load_riftx_config(&path).await.expect("load");
    let view = settings_view(&config, true)
        .await
        .expect("multi-profile settings view");

    assert_eq!(
        view,
        LlmSettingsView {
            default_profile: "zeta".to_string(),
            profiles: vec![
                LlmProfileSettingsView {
                    profile_name: "alpha".to_string(),
                    model: "model-a".to_string(),
                    base_url: "https://alpha.example.test/v1".to_string(),
                    timeout_seconds: 60,
                    reasoning_level: "medium".to_string(),
                    context_budget: 64_000,
                    credential_source: "environment".to_string(),
                    credential_name: "RIFTX_SETTINGS_ALPHA_UNSET".to_string(),
                    configured: false,
                },
                LlmProfileSettingsView {
                    profile_name: "zeta".to_string(),
                    model: "model-z".to_string(),
                    base_url: "https://zeta.example.test/v1".to_string(),
                    timeout_seconds: 300,
                    reasoning_level: "high".to_string(),
                    context_budget: 200_000,
                    credential_source: "environment".to_string(),
                    credential_name: "RIFTX_SETTINGS_ZETA_UNSET".to_string(),
                    configured: false,
                },
            ],
            daemon_restart_required: true,
        }
    );
}

#[tokio::test]
async fn tools_settings_round_trip_directories() {
    let temp = tempfile::tempdir().expect("temp dir");
    let path = temp.path().join("riftx.toml");
    tokio::fs::write(&path, sample_config_toml())
        .await
        .expect("write config");

    let mut config = load_riftx_config(&path).await.expect("load");
    config.tools.directories = vec![
        PathBuf::from("/tmp/team-tools"),
        PathBuf::from("/opt/scanners"),
    ];
    write_riftx_config(&path, &config)
        .await
        .expect("write tools");
    let reloaded = load_riftx_config(&path).await.expect("reload");
    assert_eq!(
        tools_settings_view(&reloaded, false),
        ToolsSettingsView {
            directories: vec!["/tmp/team-tools".to_string(), "/opt/scanners".to_string(),],
            daemon_restart_required: false,
        }
    );
}

#[tokio::test]
async fn upsert_and_delete_llm_profiles_enforce_defaults() {
    let temp = tempfile::tempdir().expect("temp dir");
    let path = temp.path().join("riftx.toml");
    tokio::fs::write(&path, sample_config_toml())
        .await
        .expect("write config");

    let mut config = load_riftx_config(&path).await.expect("load");
    config.llm.profiles.insert(
        "lab".to_string(),
        LlmProfileConfig {
            model: "local-model".to_string(),
            base_url: "http://127.0.0.1:8080/v1".to_string(),
            api_key: LlmApiKeySource::Keyring {
                credential: "lab".to_string(),
            },
            timeout_seconds: 300,
            reasoning_level: LlmReasoningLevel::Medium,
            context_budget: 64_000,
        },
    );
    config.llm.default_profile = "lab".to_string();
    config.validate().expect("valid");
    write_riftx_config(&path, &config).await.expect("write");

    let mut config = load_riftx_config(&path).await.expect("reload");
    assert!(config.llm.profiles.contains_key("lab"));
    assert_eq!(config.llm.default_profile, "lab");
    assert!(config.llm.profiles.remove("openai").is_some());
    config.validate().expect("still valid with one profile");
    write_riftx_config(&path, &config)
        .await
        .expect("write single");
    let single = load_riftx_config(&path).await.expect("single");
    assert_eq!(single.llm.profiles.len(), 1);
    assert_eq!(single.llm.default_profile, "lab");
}

#[tokio::test]
async fn cannot_validate_after_removing_last_profile() {
    let temp = tempfile::tempdir().expect("temp dir");
    let path = temp.path().join("riftx.toml");
    tokio::fs::write(&path, sample_config_toml())
        .await
        .expect("write config");

    let mut config = load_riftx_config(&path).await.expect("load");
    config.llm.profiles.clear();
    assert!(config.validate().is_err());
}
