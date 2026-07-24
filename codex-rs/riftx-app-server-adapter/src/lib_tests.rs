use super::*;
use codex_app_server_client::DEFAULT_IN_PROCESS_CHANNEL_CAPACITY;
use codex_app_server_client::EnvironmentManager;
use codex_arg0::Arg0DispatchPaths;
use codex_config::CloudConfigBundleLoader;
use codex_config::LoaderOverrides;
use codex_core::config::ConfigBuilder;
use codex_core::init_state_db;
use codex_feedback::CodexFeedback;
use codex_protocol::protocol::SessionSource;
use pretty_assertions::assert_eq;
use std::sync::Arc;
use tempfile::TempDir;

struct TestAdapter {
    workspace: TempDir,
    adapter: RiftxAppServerAdapter,
}

async fn start_test_adapter() -> TestAdapter {
    let workspace = TempDir::new().expect("workspace");
    let config = Arc::new(
        ConfigBuilder::default()
            .codex_home(workspace.path().join("runtime"))
            .build()
            .await
            .expect("test config"),
    );
    let state_db = init_state_db(config.as_ref())
        .await
        .expect("state db should initialize");
    let adapter = RiftxAppServerAdapter::start(InProcessClientStartArgs {
        arg0_paths: Arg0DispatchPaths::default(),
        config,
        cli_overrides: Vec::new(),
        loader_overrides: LoaderOverrides::default(),
        strict_config: true,
        cloud_config_bundle: CloudConfigBundleLoader::default(),
        feedback: CodexFeedback::new(),
        log_db: None,
        state_db: Some(state_db),
        environment_manager: Arc::new(EnvironmentManager::default_for_tests()),
        config_warnings: Vec::new(),
        session_source: SessionSource::Custom("riftx-test".to_string()),
        enable_codex_api_key_env: false,
        client_name: "riftx-agent-client-test".to_string(),
        client_version: "0.0.0-test".to_string(),
        experimental_api: true,
        mcp_server_openai_form_elicitation: false,
        opt_out_notification_methods: Vec::new(),
        channel_capacity: DEFAULT_IN_PROCESS_CHANNEL_CAPACITY,
    })
    .await
    .expect("adapter should start");
    TestAdapter { workspace, adapter }
}

#[tokio::test]
async fn embedded_runtime_forces_api_key_only_authentication() {
    let runtime_home = TempDir::new().expect("runtime home");
    let runtime = RiftxLlmRuntimeConfig {
        runtime_home: runtime_home.path().to_path_buf(),
        model: "riftx-test-model".to_string(),
        base_url: "http://127.0.0.1:8766/v1".to_string(),
        excluded_api_key_env: Some("RIFTX_TEST_API_KEY".to_string()),
        api_key: RiftxApiKey::new("riftx-test-key".to_string()).expect("API key"),
        process_path: "/test/tools:/usr/bin".to_string(),
    };
    let built = build_runtime_config(&runtime)
        .await
        .expect("runtime config");
    let config = built.config;

    assert_eq!(
        (
            config.model.as_deref(),
            config.model_provider_id.as_str(),
            config.model_provider.name.as_str(),
            config.model_provider.base_url.as_deref(),
            config.model_provider.env_key.as_deref(),
            config.model_provider.requires_openai_auth,
            config.forced_login_method,
            config.cli_auth_credentials_store_mode,
            config.bundled_skills_enabled(),
            config
                .permissions
                .shell_environment_policy
                .r#set
                .get("PATH"),
        ),
        (
            Some("riftx-test-model"),
            "riftx",
            "RiftX LLM",
            Some("http://127.0.0.1:8766/v1"),
            None,
            false,
            Some(ForcedLoginMethod::Api),
            AuthCredentialsStoreMode::Ephemeral,
            false,
            Some(&"/test/tools:/usr/bin".to_string()),
        )
    );
    assert!(
        config
            .permissions
            .shell_environment_policy
            .exclude
            .iter()
            .any(|name| *name == "RIFTX_TEST_API_KEY")
    );
}

#[tokio::test]
async fn exclusive_skill_root_is_the_only_runtime_skill_source() {
    let test = start_test_adapter().await;
    let skills_root = test.workspace.path().join("skills");
    let skill_dir = skills_root.join("lab-recon");
    std::fs::create_dir_all(&skill_dir).expect("create skill");
    std::fs::write(
        skill_dir.join("SKILL.md"),
        "---\nname: lab-recon\ndescription: Run authorized lab reconnaissance\n---\n",
    )
    .expect("write skill");
    let handle = test.adapter.request_handle();
    handle
        .set_exclusive_skill_root(&skills_root)
        .await
        .expect("set skill root");
    let catalog = handle
        .list_skills(test.workspace.path(), /*force_reload*/ true)
        .await
        .expect("list skills");

    assert_eq!(
        catalog
            .skills
            .iter()
            .map(|skill| skill.name.as_str())
            .collect::<Vec<_>>(),
        vec!["lab-recon"]
    );
    test.adapter.shutdown().await.expect("shutdown");
}

#[tokio::test]
async fn local_workspace_starts_a_single_main_agent_thread() {
    let test = start_test_adapter().await;
    let thread_id = test
        .adapter
        .start_local_thread(test.workspace.path())
        .await
        .expect("local thread should start");

    assert!(!thread_id.is_empty());
    test.adapter.shutdown().await.expect("shutdown");
}

#[test]
fn relative_workspaces_are_rejected() {
    assert!(matches!(
        workspace_string(Path::new("relative")),
        Err(AdapterError::InvalidWorkspace(_))
    ));
}

#[test]
fn event_envelope_preserves_lag_information() {
    let event = RiftxAppServerEvent::Lagged { skipped: 3 };
    assert_eq!(
        event.envelope().expect("event envelope"),
        RiftxEventEnvelope {
            kind: "appServer/lagged".to_string(),
            thread_id: None,
            turn_id: None,
            request_id: None,
            data: serde_json::json!({"skipped": 3}),
        }
    );
}
