use super::*;
use codex_riftx_app_server_adapter::CommandExecutionOutputDeltaNotification;
use codex_riftx_app_server_adapter::ItemCompletedNotification;
use codex_riftx_app_server_adapter::ItemStartedNotification;
use codex_riftx_app_server_adapter::TerminalInteractionNotification;
use codex_riftx_core::ArtifactConfig;
use codex_riftx_core::AssessmentObjective;
use codex_riftx_core::AuditConfig;
use codex_riftx_core::AuditRecord;
use codex_riftx_core::AuthorizationScope;
use codex_riftx_core::AuthorizationWindow;
use codex_riftx_core::DaemonConfig;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::EnvironmentClass;
use codex_riftx_core::ExecutionMode;
use codex_riftx_core::LlmConfig;
use codex_riftx_core::ManagedPolicyConfig;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::Scope;
use codex_riftx_core::StateStore;
use codex_riftx_skills::SkillCatalog;
use codex_riftx_skills::SkillDirectoryConfig;
use codex_riftx_tools::ToolInventory;
use codex_riftx_tools::ToolScanConfig;
use pretty_assertions::assert_eq;
use serde_json::json;
use tempfile::TempDir;

#[tokio::test]
async fn command_lifecycle_persists_redacted_execution_and_stream_hashes() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = engagement();
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    state
        .thread_engagements
        .write()
        .await
        .insert("thread-1".to_string(), engagement.id.clone());

    let executable = std::env::current_exe().expect("current executable");
    let raw_argv = [
        executable.to_string_lossy().into_owned(),
        "--token".to_string(),
        "super-secret".to_string(),
        "visible".to_string(),
    ];
    let command = shlex::try_join(raw_argv.iter().map(String::as_str)).expect("join command");
    let started_item = command_item(&command, temp.path(), "inProgress", None, None, None);
    process_notification(
        &state,
        &ServerNotification::ItemStarted(ItemStartedNotification {
            item: started_item,
            thread_id: "thread-1".to_string(),
            turn_id: "turn-1".to_string(),
            started_at_ms: 100_000,
        }),
    )
    .await;
    process_notification(
        &state,
        &ServerNotification::CommandExecutionOutputDelta(CommandExecutionOutputDeltaNotification {
            thread_id: "thread-1".to_string(),
            turn_id: "turn-1".to_string(),
            item_id: "execution-1".to_string(),
            stream: CommandExecutionOutputStream::Stdout,
            delta: "stdout".to_string(),
        }),
    )
    .await;
    process_notification(
        &state,
        &ServerNotification::CommandExecutionOutputDelta(CommandExecutionOutputDeltaNotification {
            thread_id: "thread-1".to_string(),
            turn_id: "turn-1".to_string(),
            item_id: "execution-1".to_string(),
            stream: CommandExecutionOutputStream::Stderr,
            delta: "stderr".to_string(),
        }),
    )
    .await;
    process_notification(
        &state,
        &ServerNotification::TerminalInteraction(TerminalInteractionNotification {
            thread_id: "thread-1".to_string(),
            turn_id: "turn-1".to_string(),
            item_id: "execution-1".to_string(),
            process_id: "process-1".to_string(),
            stdin: "stdin-secret".to_string(),
        }),
    )
    .await;
    let completed_item = command_item(
        &command,
        temp.path(),
        "completed",
        Some("process-1"),
        Some(0),
        Some(10_000),
    );
    process_notification(
        &state,
        &ServerNotification::ItemCompleted(ItemCompletedNotification {
            item: completed_item,
            thread_id: "thread-1".to_string(),
            turn_id: "turn-1".to_string(),
            completed_at_ms: 110_000,
        }),
    )
    .await;

    let canonical_executable = tokio::fs::canonicalize(&executable)
        .await
        .expect("canonical executable");
    let expected = Execution {
        id: "execution-1".to_string(),
        engagement_id: "eng-1".to_string(),
        test_case_id: None,
        task_id: None,
        turn_id: "turn-1".to_string(),
        runner: format!("local:{}", executable.to_string_lossy()),
        status: ExecutionStatus::Completed,
        started_at: 100,
        completed_at: Some(110),
        exit_code: Some(0),
        duration_ms: Some(10_000),
        argv: vec![
            executable.to_string_lossy().into_owned(),
            "--token".to_string(),
            "[REDACTED]".to_string(),
            "visible".to_string(),
        ],
        command_sha256: digest(command.as_bytes()),
        cwd: temp.path().to_string_lossy().into_owned(),
        process_id: Some("process-1".to_string()),
        tool: Some(ExecutionTool {
            requested_name: executable.to_string_lossy().into_owned(),
            resolved_path: Some(canonical_executable.to_string_lossy().into_owned()),
            sha256: Some(
                hash_file(&canonical_executable)
                    .await
                    .expect("hash executable"),
            ),
            metadata_sha256: None,
            version: None,
            managed: false,
        }),
        tool_inventory_sha256: ToolInventory::empty().snapshot_sha256,
        stdout_sha256: Some(digest(b"stdout")),
        stderr_sha256: Some(digest(b"stderr")),
        stdin_sha256: Some(digest(b"stdin-secret")),
        stdout_bytes: 6,
        stderr_bytes: 6,
        stdin_bytes: 12,
    };
    let audit = tokio::fs::read_to_string(&state.config.audit.jsonl_path)
        .await
        .expect("audit");
    assert!(!audit.contains("super-secret"));
    assert!(!audit.contains("stdin-secret"));
    let records = audit
        .lines()
        .map(|line| serde_json::from_str::<AuditRecord>(line).expect("audit record"))
        .collect::<Vec<_>>();
    let completed = records
        .iter()
        .find(|record| record.event == "execution/completed")
        .expect("completed audit record");
    assert_eq!(
        completed.details,
        Some(serde_json::to_value(&expected).expect("execution details"))
    );
    assert_eq!(
        state.store.executions("eng-1").await.expect("executions"),
        vec![expected]
    );
}

#[test]
fn shell_wrappers_are_removed_before_auditing() {
    assert_eq!(
        effective_argv("zsh -lc 'nmap --password secret 127.0.0.1'"),
        vec![
            "nmap".to_string(),
            "--password".to_string(),
            "secret".to_string(),
            "127.0.0.1".to_string(),
        ]
    );
}

#[test]
fn inline_secrets_are_redacted_before_persistence() {
    assert_eq!(
        redact_argv(&[
            "tool".to_string(),
            "--api-key=value".to_string(),
            "Authorization: Bearer value".to_string(),
            "https://user:password@example.test/path".to_string(),
        ]),
        vec![
            "tool".to_string(),
            "--api-key=[REDACTED]".to_string(),
            "Authorization: [REDACTED]".to_string(),
            "[REDACTED_URL]".to_string(),
        ]
    );
}

fn command_item(
    command: &str,
    cwd: &Path,
    status: &str,
    process_id: Option<&str>,
    exit_code: Option<i32>,
    duration_ms: Option<i64>,
) -> ThreadItem {
    serde_json::from_value(json!({
        "type": "commandExecution",
        "id": "execution-1",
        "command": command,
        "cwd": cwd.to_string_lossy(),
        "processId": process_id,
        "source": "agent",
        "status": status,
        "commandActions": [],
        "aggregatedOutput": null,
        "exitCode": exit_code,
        "durationMs": duration_ms,
    }))
    .expect("command item")
}

async fn test_state(temp: &TempDir) -> GatewayState {
    let config = RiftxConfig {
        daemon: DaemonConfig {
            ipc_dir: temp.path().join("ipc"),
            state_db: temp.path().join("state.sqlite"),
            runtime_home: temp.path().join("runtime"),
            workspace_root: temp.path().join("workspaces"),
        },
        llm: LlmConfig {
            model: "riftx-test-model".to_string(),
            base_url: "http://127.0.0.1:8766/v1".to_string(),
            api_key_env: "RIFTX_TEST_API_KEY".to_string(),
        },
        policy: ManagedPolicyConfig {
            allowed_capabilities: vec!["network.discovery".to_string()],
            denied_cidrs: Vec::new(),
            denied_domains: Vec::new(),
        },
        audit: AuditConfig {
            jsonl_path: temp.path().join("audit.jsonl"),
            fsync: false,
        },
        artifacts: ArtifactConfig {
            root: temp.path().join("artifacts"),
            max_bytes_per_engagement: 1024,
        },
        skills: SkillDirectoryConfig::default(),
        tools: ToolScanConfig {
            directories: Vec::new(),
            extra_paths: Vec::new(),
        },
    };
    let store = StateStore::open(&config.daemon.state_db)
        .await
        .expect("state store");
    GatewayState::new(
        config,
        store,
        SkillCatalog::empty(temp.path().join("skills")),
        ToolInventory::empty(),
    )
}

fn engagement() -> Engagement {
    Engagement {
        id: "eng-1".to_string(),
        name: "Authorized lab".to_string(),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Validate authorized targets".to_string(),
            success_criteria: Vec::new(),
            structured_criteria: Vec::new(),
        },
        entry_points: vec!["127.0.0.1".to_string()],
        mode: ExecutionMode::Native,
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: vec!["127.0.0.0/8".parse().expect("CIDR")],
                domains: Vec::new(),
                ports: Vec::new(),
            },
            identities: Vec::new(),
            capabilities: vec!["network.discovery".to_string()],
            environment: EnvironmentClass::Lab,
            window: AuthorizationWindow {
                starts_at: None,
                expires_at: Some(2_000_000_000),
            },
        },
        policy_revision: "revision-1".to_string(),
        thread_id: Some("thread-1".to_string()),
        created_at: 1,
        updated_at: 1,
    }
}
