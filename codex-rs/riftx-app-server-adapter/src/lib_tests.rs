use super::*;
use codex_app_server_client::DEFAULT_IN_PROCESS_CHANNEL_CAPACITY;
use codex_app_server_client::EnvironmentManager;
use codex_app_server_protocol::EnvironmentStatusKind;
use codex_arg0::Arg0DispatchPaths;
use codex_config::CloudConfigBundleLoader;
use codex_config::LoaderOverrides;
use codex_core::config::ConfigBuilder;
use codex_core::init_state_db;
use codex_exec_server::ExecServerRuntimePaths;
use codex_feedback::CodexFeedback;
use codex_protocol::protocol::SessionSource;
use pretty_assertions::assert_eq;
use std::sync::Arc;
use tempfile::TempDir;
use tokio::net::TcpListener;
use tokio::time::Duration;
use tokio::time::sleep;

struct TestAdapter {
    _codex_home: TempDir,
    adapter: RiftxAppServerAdapter,
}

async fn start_test_adapter() -> TestAdapter {
    let codex_home = TempDir::new().expect("temp dir");
    let config = Arc::new(
        ConfigBuilder::default()
            .codex_home(codex_home.path().to_path_buf())
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
        session_source: SessionSource::Exec,
        enable_codex_api_key_env: false,
        client_name: "riftx-adapter-test".to_string(),
        client_version: "0.0.0-test".to_string(),
        experimental_api: true,
        mcp_server_openai_form_elicitation: false,
        opt_out_notification_methods: Vec::new(),
        channel_capacity: DEFAULT_IN_PROCESS_CHANNEL_CAPACITY,
    })
    .await
    .expect("adapter should start");
    TestAdapter {
        _codex_home: codex_home,
        adapter,
    }
}

#[tokio::test]
async fn unknown_environment_status_is_typed() {
    let test = start_test_adapter().await;
    let status = test
        .adapter
        .environment_status("missing".to_string())
        .await
        .expect("status request should succeed");
    assert_eq!(
        status,
        EnvironmentStatusResponse {
            status: EnvironmentStatusKind::Unknown,
            error: Some("unknown environment id `missing`".to_string()),
        }
    );
    test.adapter.shutdown().await.expect("shutdown");
}

#[tokio::test]
async fn registered_remote_environment_starts_pending() {
    let test = start_test_adapter().await;
    test.adapter
        .add_environment(EnvironmentRegistration {
            environment_id: "sandbox-1".to_string(),
            exec_server_url: "ws://127.0.0.1:9".to_string(),
            connect_timeout_ms: Some(50),
        })
        .await
        .expect("environment/add should succeed");
    let status = test
        .adapter
        .environment_status("sandbox-1".to_string())
        .await
        .expect("status request should succeed");
    assert_eq!(status.status, EnvironmentStatusKind::Pending);
    test.adapter.shutdown().await.expect("shutdown");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn connects_to_real_exec_server() {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("reserve exec-server port");
    let address = listener.local_addr().expect("listener address");
    drop(listener);

    let runtime_paths = ExecServerRuntimePaths::new(
        std::env::current_exe().expect("current test executable"),
        /*codex_linux_sandbox_exe*/ None,
    )
    .expect("runtime paths");
    let exec_server = tokio::spawn(async move {
        codex_exec_server::run_main(&format!("ws://{address}"), runtime_paths).await
    });
    sleep(Duration::from_millis(50)).await;

    let test = start_test_adapter().await;
    test.adapter
        .add_environment(EnvironmentRegistration {
            environment_id: "sandbox-real".to_string(),
            exec_server_url: format!("ws://{address}"),
            connect_timeout_ms: Some(2_000),
        })
        .await
        .expect("environment/add should succeed");
    let info = test
        .adapter
        .environment_info("sandbox-real".to_string())
        .await
        .expect("real exec-server should report environment info");
    assert!(!info.shell.name.is_empty());
    assert!(!info.shell.path.is_empty());

    let status = test
        .adapter
        .environment_status("sandbox-real".to_string())
        .await
        .expect("status request should succeed");
    assert_eq!(status.status, EnvironmentStatusKind::Ready);

    test.adapter.shutdown().await.expect("shutdown");
    exec_server.abort();
}
