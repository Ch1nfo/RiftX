use super::*;
use axum::body::Body;
use axum::http::Request;
use codex_riftx_core::AUTO_MODE_CONFIRMATION;
use codex_riftx_core::Artifact;
use codex_riftx_core::ArtifactConfig;
use codex_riftx_core::Asset;
use codex_riftx_core::AssetRelation;
use codex_riftx_core::AuditConfig;
use codex_riftx_core::AuthorizationScope;
use codex_riftx_core::AuthorizationWindow;
use codex_riftx_core::ConversationEntryDraft;
use codex_riftx_core::ConversationKind;
use codex_riftx_core::ConversationRole;
use codex_riftx_core::DaemonConfig;
use codex_riftx_core::EffectivePolicy;
use codex_riftx_core::EnvironmentClass;
use codex_riftx_core::ExecutionMode;
use codex_riftx_core::LlmApiKeySource;
use codex_riftx_core::LlmConfig;
use codex_riftx_core::LlmProfileConfig;
use codex_riftx_core::LlmProtocol;
use codex_riftx_core::LlmReasoningLevel;
use codex_riftx_core::ManagedPolicyConfig;
use codex_riftx_core::Observation;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::Scope;
use codex_riftx_core::StateStore;
use codex_riftx_core::StateSubject;
use codex_riftx_core::TargetStateError;
use codex_riftx_crypto::CryptoError;
use codex_riftx_crypto::KeyringEngagementCipher;
use codex_riftx_ipc::AuditHealthState;
use codex_riftx_ipc::DaemonControlStatus;
use codex_riftx_ipc::DaemonPauseReason;
use codex_riftx_ipc::DaemonRunState;
use codex_riftx_ipc::SkillCatalog as IpcSkillCatalog;
use codex_riftx_ipc::ToolInventory as IpcToolInventory;
use codex_riftx_skills::SkillCatalog;
use codex_riftx_skills::SkillDirectoryConfig;
use codex_riftx_tools::ToolInventory;
use codex_riftx_tools::ToolScanConfig;
use http_body_util::BodyExt;
use pretty_assertions::assert_eq;
use std::collections::BTreeMap;
use std::sync::Arc;
use tempfile::TempDir;
use tower::ServiceExt;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

pub(crate) async fn test_state(temp: &TempDir) -> GatewayState {
    let config = RiftxConfig {
        daemon: DaemonConfig {
            ipc_dir: temp.path().join("ipc"),
            state_db: temp.path().join("state.sqlite"),
            runtime_home: temp.path().join("runtime"),
            workspace_root: temp.path().join("workspaces"),
        },
        llm: LlmConfig {
            config_version: codex_riftx_core::LLM_CONFIG_VERSION,
            default_profile: "default".to_string(),
            profiles: BTreeMap::from([
                (
                    "alternate".to_string(),
                    LlmProfileConfig {
                        enabled: true,
                        protocol: LlmProtocol::Responses,
                        model: "riftx-alternate-test-model".to_string(),
                        base_url: "http://127.0.0.1:8766/v1".to_string(),
                        api_key: LlmApiKeySource::Environment {
                            variable: "RIFTX_ALTERNATE_TEST_API_KEY".to_string(),
                        },
                        timeout_seconds: 300,
                        reasoning_level: LlmReasoningLevel::High,
                        context_budget: 200_000,
                    },
                ),
                (
                    "default".to_string(),
                    LlmProfileConfig {
                        enabled: true,
                        protocol: LlmProtocol::Responses,
                        model: "riftx-test-model".to_string(),
                        base_url: "http://127.0.0.1:8766/v1".to_string(),
                        api_key: LlmApiKeySource::Environment {
                            variable: "RIFTX_TEST_API_KEY".to_string(),
                        },
                        timeout_seconds: 300,
                        reasoning_level: LlmReasoningLevel::High,
                        context_budget: 200_000,
                    },
                ),
            ]),
        },
        policy: ManagedPolicyConfig {
            allowed_capabilities: vec![
                "network.discovery".to_string(),
                "web.discovery".to_string(),
                "attack_path.analysis".to_string(),
            ],
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
    let cipher = Arc::new(KeyringEngagementCipher::new(
        codex_keyring_store::tests::MockKeyringStore::default(),
    ));
    let store = StateStore::open_with_cipher(&config.daemon.state_db, cipher)
        .await
        .expect("state store");
    GatewayState::new(
        config,
        store,
        SkillCatalog::empty(temp.path().join("skills")),
        ToolInventory::empty(),
    )
}

async fn test_router(temp: &TempDir) -> Router {
    build_router(test_state(temp).await)
}

pub(crate) async fn block_audit(temp: &TempDir) {
    let path = temp.path().join("audit.jsonl");
    match tokio::fs::remove_file(&path).await {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => panic!("remove audit file: {error}"),
    }
    tokio::fs::create_dir(&path)
        .await
        .expect("replace audit file with directory");
}

pub(crate) async fn unblock_audit(temp: &TempDir) {
    tokio::fs::remove_dir(temp.path().join("audit.jsonl"))
        .await
        .expect("remove blocking audit directory");
}

#[tokio::test]
async fn profile_runtime_events_are_isolated_to_matching_active_turns() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    state.active_turns.write().await.extend([
        (
            "engagement-a".to_string(),
            ActiveTurn {
                profile_name: "profile-a".to_string(),
                thread_id: "thread-a".to_string(),
                turn_id: "turn-a".to_string(),
            },
        ),
        (
            "engagement-b".to_string(),
            ActiveTurn {
                profile_name: "profile-b".to_string(),
                thread_id: "thread-b".to_string(),
                turn_id: "turn-b".to_string(),
            },
        ),
    ]);
    let mut events_a = state.event_sender("engagement-a").await.subscribe();
    let mut events_b = state.event_sender("engagement-b").await.subscribe();

    state
        .publish_to_profile_active("profile-a", "runtime/test", json!({}))
        .await;

    assert_eq!(
        events_a
            .recv()
            .await
            .expect("profile-a event")
            .engagement_id,
        "engagement-a"
    );
    assert!(matches!(
        events_b.try_recv(),
        Err(tokio::sync::broadcast::error::TryRecvError::Empty)
    ));
}

pub(crate) fn native_engagement(
    state: &GatewayState,
    id: &str,
    status: EngagementStatus,
) -> Engagement {
    let authorization = AuthorizationScope {
        network: Scope {
            cidrs: vec!["10.10.0.0/24".parse().expect("CIDR")],
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
    };
    let policy_revision = EffectivePolicy::resolve(
        &state.config.policy,
        ExecutionMode::Pentest,
        &authorization,
        None,
    )
    .expect("effective policy")
    .revision;
    Engagement {
        id: id.to_string(),
        name: "Controlled lab".to_string(),
        status,
        objective: AssessmentObjective {
            summary: "Validate runtime controls".to_string(),
            success_criteria: Vec::new(),
            structured_criteria: Vec::new(),
        },
        entry_points: vec!["10.10.0.10".to_string()],
        mode: ExecutionMode::Pentest,
        llm_profile: "default".to_string(),
        authorization,
        policy_revision,
        thread_id: None,
        created_at: 1,
        updated_at: 1,
    }
}

#[tokio::test]
async fn extension_endpoints_return_typed_startup_inventories() {
    let temp = TempDir::new().expect("temp dir");
    let app = test_router(&temp).await;
    let tools_response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/tools")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");
    let skills_response = app
        .oneshot(
            Request::builder()
                .uri("/v1/skills")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(
        (tools_response.status(), skills_response.status()),
        (StatusCode::OK, StatusCode::OK)
    );
    let tools: IpcToolInventory = serde_json::from_slice(
        &tools_response
            .into_body()
            .collect()
            .await
            .expect("tool body")
            .to_bytes(),
    )
    .expect("tool inventory");
    let skills: IpcSkillCatalog = serde_json::from_slice(
        &skills_response
            .into_body()
            .collect()
            .await
            .expect("skill body")
            .to_bytes(),
    )
    .expect("skill catalog");

    assert_eq!(
        (tools, skills),
        (
            IpcToolInventory {
                roots: Vec::new(),
                path_entries: Vec::new(),
                tools: Vec::new(),
                snapshot_sha256: ToolInventory::empty().snapshot_sha256,
                diagnostics: Vec::new(),
            },
            IpcSkillCatalog {
                root: temp.path().join("skills"),
                skills: Vec::new(),
                snapshot_sha256: SkillCatalog::empty(temp.path().join("skills")).snapshot_sha256,
                diagnostics: Vec::new(),
            },
        )
    );
}

#[tokio::test]
async fn tool_doctor_rescans_the_configured_directory() {
    let temp = TempDir::new().expect("temp dir");
    let tools_root = temp.path().join("doctor-tools");
    tokio::fs::create_dir_all(&tools_root)
        .await
        .expect("tool directory");
    let tool_path = tools_root.join(if cfg!(windows) {
        "doctor-probe.exe"
    } else {
        "doctor-probe"
    });
    tokio::fs::write(&tool_path, b"doctor probe")
        .await
        .expect("tool file");
    #[cfg(unix)]
    {
        let mut permissions = tokio::fs::metadata(&tool_path)
            .await
            .expect("tool metadata")
            .permissions();
        permissions.set_mode(0o755);
        tokio::fs::set_permissions(&tool_path, permissions)
            .await
            .expect("tool permissions");
    }
    let mut state = test_state(&temp).await;
    Arc::make_mut(&mut state.config).tools.directories = vec![tools_root.clone()];
    let startup_snapshot = state.tools.snapshot_sha256.clone();
    let response = build_router(state)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/tools/doctor")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::OK);
    let inventory: IpcToolInventory = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("tool doctor inventory");

    assert_eq!(inventory.roots, vec![tools_root]);
    assert_eq!(
        inventory
            .tools
            .iter()
            .map(|tool| tool.name.as_str())
            .collect::<Vec<_>>(),
        vec!["doctor-probe"]
    );
    assert_ne!(inventory.snapshot_sha256, startup_snapshot);
}

#[tokio::test]
async fn llm_profiles_list_reports_configured_state() {
    let temp = TempDir::new().expect("temp dir");
    unsafe {
        std::env::set_var("RIFTX_TEST_API_KEY", "test-key");
    }
    let state = test_state(&temp).await;
    let response = build_router(state)
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/v1/llm/profiles")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::OK);
    let list: codex_riftx_ipc::LlmProfileList = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("profile list");
    assert_eq!(list.default_profile, "default");
    assert!(
        list.profiles
            .iter()
            .any(|profile| profile.name == "default" && profile.configured)
    );
}

#[tokio::test]
async fn llm_profile_connection_test_reports_capability_matrix() {
    let upstream = wiremock::MockServer::start().await;
    wiremock::Mock::given(wiremock::matchers::method("POST"))
        .and(wiremock::matchers::path("/v1/responses"))
        .respond_with(
            wiremock::ResponseTemplate::new(200).set_body_string(
                "event: response.created\ndata: {\"type\":\"response.created\"}\n\n\
                 event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"ping\"}\n\n\
                 event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n",
            ),
        )
        .up_to_n_times(1)
        .mount(&upstream)
        .await;
    wiremock::Mock::given(wiremock::matchers::method("POST"))
        .and(wiremock::matchers::path("/v1/responses"))
        .respond_with(
            wiremock::ResponseTemplate::new(200).set_body_string(
                "event: response.created\ndata: {\"type\":\"response.created\"}\n\n\
                 event: response.output_item.done\n\
                 data: {\"type\":\"response.output_item.done\",\"item\":{\"type\":\"function_call\",\"name\":\"riftx_connection_test\",\"call_id\":\"c1\",\"arguments\":\"{\\\"ping\\\":\\\"ok\\\"}\"}}\n\n\
                 event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n",
            ),
        )
        .mount(&upstream)
        .await;

    let temp = TempDir::new().expect("temp dir");
    unsafe {
        std::env::set_var("RIFTX_TEST_API_KEY", "test-key");
    }
    let mut state = test_state(&temp).await;
    {
        let config = Arc::make_mut(&mut state.config);
        let profile = config
            .llm
            .profiles
            .get_mut("default")
            .expect("default profile");
        profile.base_url = format!("{}/v1", upstream.uri());
        profile.timeout_seconds = 5;
    }

    let response = build_router(state)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/llm/profiles/default/test")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::OK);
    let result: codex_riftx_ipc::LlmConnectionTestResult = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("connection test");
    assert!(result.ok, "{result:?}");
    assert_eq!(
        result.capabilities.stream_text.status,
        codex_riftx_ipc::LlmCheckStatus::Passed
    );
    assert_eq!(
        result.capabilities.function_tools.status,
        codex_riftx_ipc::LlmCheckStatus::Passed
    );
}

#[tokio::test]
async fn restart_reconciliation_pauses_active_engagements_without_ending_them() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = Engagement {
        id: "eng-active".to_string(),
        name: "Juice Shop".to_string(),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Validate exploitable web risks".to_string(),
            success_criteria: Vec::new(),
            structured_criteria: Vec::new(),
        },
        entry_points: vec!["10.10.0.10".to_string()],
        mode: ExecutionMode::Pentest,
        llm_profile: "default".to_string(),
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: vec!["10.10.0.0/24".parse().expect("CIDR")],
                domains: Vec::new(),
                ports: vec![80],
            },
            identities: Vec::new(),
            capabilities: vec!["network.discovery".to_string()],
            environment: EnvironmentClass::Lab,
            window: AuthorizationWindow {
                starts_at: None,
                expires_at: Some(2_000_000_000),
            },
        },
        policy_revision: "rev-1".to_string(),
        thread_id: Some("thread-stale".to_string()),
        created_at: 1,
        updated_at: 1,
    };
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    state
        .reconcile_after_restart()
        .await
        .expect("reconcile state");
    assert_eq!(
        state
            .store
            .engagement(&engagement.id)
            .await
            .expect("read engagement")
            .status,
        EngagementStatus::Active
    );
    assert!(
        state
            .deadline_tasks
            .read()
            .await
            .contains_key(&engagement.id)
    );
    let paused = state.control_status().await;
    assert_eq!(
        paused,
        DaemonControlStatus {
            state: DaemonRunState::Paused,
            reason: Some(DaemonPauseReason::OperatorPause),
            updated_at: paused.updated_at,
            audit: paused.audit.clone(),
        }
    );
}

#[tokio::test]
async fn kill_switch_survives_gateway_restart() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = native_engagement(&state, "eng-kill-restart", EngagementStatus::Active);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store active kill engagement");
    let expected = state
        .set_control(DaemonRunState::Paused, Some(DaemonPauseReason::KillSwitch))
        .await
        .expect("activate kill switch");
    let restarted = GatewayState::new(
        state.config.as_ref().clone(),
        state.store.clone(),
        state.skills.as_ref().clone(),
        state.tools.as_ref().clone(),
    );
    restarted
        .reconcile_after_restart()
        .await
        .expect("restore runtime state");

    assert_eq!(restarted.control_status().await, expected);
    assert_eq!(
        restarted
            .store
            .engagement(&engagement.id)
            .await
            .expect("reconciled kill engagement")
            .status,
        EngagementStatus::Interrupted
    );
    assert!(
        !restarted
            .deadline_tasks
            .read()
            .await
            .contains_key(&engagement.id)
    );
}

#[tokio::test]
async fn operator_pause_survives_gateway_restart() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let expected = state
        .set_control(
            DaemonRunState::Paused,
            Some(DaemonPauseReason::OperatorPause),
        )
        .await
        .expect("pause");
    let restarted = GatewayState::new(
        state.config.as_ref().clone(),
        state.store.clone(),
        state.skills.as_ref().clone(),
        state.tools.as_ref().clone(),
    );
    restarted
        .reconcile_after_restart()
        .await
        .expect("restore runtime state");

    assert_eq!(restarted.control_status().await, expected);
}

#[tokio::test]
async fn clean_running_state_without_active_engagements_restores_as_running() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let expected = state
        .set_control(DaemonRunState::Running, None)
        .await
        .expect("mark running");
    let restarted = GatewayState::new(
        state.config.as_ref().clone(),
        state.store.clone(),
        state.skills.as_ref().clone(),
        state.tools.as_ref().clone(),
    );
    restarted
        .reconcile_after_restart()
        .await
        .expect("restore runtime state");

    assert_eq!(restarted.control_status().await, expected);
}

#[tokio::test]
async fn audit_failure_blocks_execution_survives_restart_and_allows_kill_and_recovery() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = native_engagement(&state, "eng-audit-degraded", EngagementStatus::Draft);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    block_audit(&temp).await;

    assert!(
        state
            .publish_critical(&engagement, "execution/test", json!({}))
            .await
            .is_err()
    );
    let degraded = state.control_status().await;
    assert_eq!(degraded.audit.state, AuditHealthState::Degraded);
    assert_eq!(
        degraded.audit.message.as_deref(),
        Some("audit log cannot be written")
    );

    let response = build_router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/engagements/{}/activate", engagement.id))
                .body(Body::empty())
                .expect("activate request"),
        )
        .await
        .expect("activate response");
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    let body: serde_json::Value = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("error body")
            .to_bytes(),
    )
    .expect("error JSON");
    assert_eq!(body["code"], "audit_unavailable");

    let restarted = GatewayState::new(
        state.config.as_ref().clone(),
        state.store.clone(),
        state.skills.as_ref().clone(),
        state.tools.as_ref().clone(),
    );
    restarted
        .reconcile_after_restart()
        .await
        .expect("restore degraded audit health");
    assert_eq!(
        restarted.control_status().await.audit.state,
        AuditHealthState::Degraded
    );

    let app = build_router(restarted.clone());
    let kill = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/system/kill")
                .body(Body::empty())
                .expect("kill request"),
        )
        .await
        .expect("kill response");
    assert_eq!(kill.status(), StatusCode::OK);
    let killed = restarted.control_status().await;
    assert_eq!(killed.state, DaemonRunState::Paused);
    assert_eq!(killed.reason, Some(DaemonPauseReason::KillSwitch));
    assert_eq!(killed.audit.state, AuditHealthState::Degraded);

    let resume = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/system/resume")
                .body(Body::empty())
                .expect("resume request"),
        )
        .await
        .expect("resume response");
    assert_eq!(resume.status(), StatusCode::SERVICE_UNAVAILABLE);

    unblock_audit(&temp).await;
    restarted
        .append_system_critical("audit/recoveryProbe", json!({}))
        .await
        .expect("recover audit");
    assert_eq!(
        restarted.control_status().await.audit.state,
        AuditHealthState::Healthy
    );
    let resumed = restarted
        .set_control(DaemonRunState::Running, None)
        .await
        .expect("resume after audit recovery");
    assert_eq!(resumed.state, DaemonRunState::Running);
}

#[tokio::test]
async fn missing_audit_encryption_key_fails_closed_without_exposing_key_errors() {
    let temp = TempDir::new().expect("temp dir");
    let mut state = test_state(&temp).await;
    let engagement = native_engagement(&state, "eng-audit-key", EngagementStatus::Draft);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let healthy_audit = state.audit.clone();
    let unavailable_cipher = Arc::new(KeyringEngagementCipher::new(
        codex_keyring_store::tests::MockKeyringStore::default(),
    ));
    let unavailable_store = StateStore::open_with_cipher(
        &temp.path().join("unavailable-audit-key.sqlite"),
        unavailable_cipher,
    )
    .await
    .expect("state store with unavailable audit key");
    state.audit = unavailable_store.audit_writer(&state.config.audit);

    assert!(
        state
            .publish_critical(&engagement, "execution/test", json!({}))
            .await
            .is_err()
    );
    let degraded = state.control_status().await;
    assert_eq!(degraded.audit.state, AuditHealthState::Degraded);
    assert_eq!(
        degraded.audit.message.as_deref(),
        Some("audit encryption is unavailable")
    );
    assert!(
        !degraded
            .audit
            .message
            .as_deref()
            .unwrap_or_default()
            .contains("key")
    );
    state
        .append_system_critical("audit/systemProbe", json!({}))
        .await
        .expect("system audit remains writable");
    assert_eq!(
        state.control_status().await.audit.state,
        AuditHealthState::Degraded
    );

    state.audit = healthy_audit;
    state
        .publish_critical(&engagement, "execution/recoveryProbe", json!({}))
        .await
        .expect("encrypted audit recovery");
    assert_eq!(
        state.control_status().await.audit.state,
        AuditHealthState::Healthy
    );
}

#[tokio::test]
async fn approval_cannot_succeed_when_critical_audit_is_unavailable() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = native_engagement(&state, "eng-approval-audit", EngagementStatus::Active);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let (decision_tx, decision_rx) = tokio::sync::oneshot::channel();
    let approval_id = "approval-audit".to_string();
    state.pending_approvals.write().await.insert(
        approval_id.clone(),
        crate::gateway_state::PendingApprovalRequest {
            profile_name: "default".to_string(),
            engagement_id: engagement.id.clone(),
            view: PendingApproval {
                id: approval_id.clone(),
                engagement_id: engagement.id.clone(),
                policy_revision: engagement.policy_revision.clone(),
                kind: codex_riftx_ipc::ApprovalKind::Tool,
                requested_at: unix_timestamp(),
                command: None,
                cwd: None,
                reason: Some("test approval".to_string()),
                execution_intent: None,
            },
            kind: PendingApprovalKind::Tool { decision_tx },
        },
    );
    block_audit(&temp).await;

    let response = build_router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/approvals/{approval_id}/decision"))
                .header("content-type", "application/json")
                .body(Body::from(r#"{"decision":"approve"}"#))
                .expect("approval request"),
        )
        .await
        .expect("approval response");

    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(!decision_rx.await.expect("tool decision"));
    assert!(
        !state
            .pending_approvals
            .read()
            .await
            .contains_key(&approval_id)
    );
    assert_eq!(
        state.control_status().await.audit.state,
        AuditHealthState::Degraded
    );
}

#[tokio::test]
async fn pause_stops_active_work_and_resume_keeps_the_engagement_ready() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = native_engagement(&state, "eng-pause", EngagementStatus::Active);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    state.register_authorization_deadline(&engagement).await;
    state
        .agent_threads
        .write()
        .await
        .insert(engagement.id.clone(), "thread-pause".to_string());
    let app = build_router(state.clone());

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/system/pause")
                .body(Body::empty())
                .expect("pause request"),
        )
        .await
        .expect("pause response");
    assert_eq!(response.status(), StatusCode::OK);
    let paused: DaemonControlStatus = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("pause body")
            .to_bytes(),
    )
    .expect("pause status");
    assert_eq!(
        (paused.state, paused.reason),
        (
            DaemonRunState::Paused,
            Some(DaemonPauseReason::OperatorPause),
        )
    );
    assert_eq!(
        state
            .store
            .engagement(&engagement.id)
            .await
            .expect("paused engagement")
            .status,
        EngagementStatus::Active
    );
    assert!(
        state
            .deadline_tasks
            .read()
            .await
            .contains_key(&engagement.id)
    );
    assert_eq!(
        state
            .agent_threads
            .read()
            .await
            .get(&engagement.id)
            .map(String::as_str),
        Some("thread-pause")
    );

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/engagements/{}/activate", engagement.id))
                .body(Body::empty())
                .expect("blocked activation request"),
        )
        .await
        .expect("blocked activation response");
    assert_eq!(response.status(), StatusCode::CONFLICT);

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/system/resume")
                .body(Body::empty())
                .expect("resume request"),
        )
        .await
        .expect("resume response");
    let resumed: DaemonControlStatus = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("resume body")
            .to_bytes(),
    )
    .expect("resume status");
    assert_eq!(
        (resumed.state, resumed.reason),
        (DaemonRunState::Running, None)
    );
    assert_eq!(
        state
            .store
            .engagement(&engagement.id)
            .await
            .expect("engagement after resume")
            .status,
        EngagementStatus::Active
    );
    assert!(
        state
            .deadline_tasks
            .read()
            .await
            .contains_key(&engagement.id)
    );

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/engagements/{}/activate", engagement.id))
                .body(Body::empty())
                .expect("redundant activation request"),
        )
        .await
        .expect("redundant activation response");
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    let audit = tokio::fs::read_to_string(&state.config.audit.jsonl_path)
        .await
        .expect("control audit");
    assert!(audit.contains("\"event\":\"daemon/paused\""));
    assert!(audit.contains("\"event\":\"daemon/resumed\""));
}

#[tokio::test]
async fn kill_switch_blocks_new_execution_with_a_distinct_reason() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = native_engagement(&state, "eng-kill", EngagementStatus::Draft);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let app = build_router(state);

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/system/kill")
                .body(Body::empty())
                .expect("kill request"),
        )
        .await
        .expect("kill response");
    let killed: DaemonControlStatus = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("kill body")
            .to_bytes(),
    )
    .expect("kill status");
    assert_eq!(
        (killed.state, killed.reason),
        (DaemonRunState::Paused, Some(DaemonPauseReason::KillSwitch),)
    );

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/engagements/{}/activate", engagement.id))
                .body(Body::empty())
                .expect("activation request"),
        )
        .await
        .expect("activation response");
    assert_eq!(response.status(), StatusCode::CONFLICT);
}

#[tokio::test]
async fn engagement_can_be_created_and_read() {
    let temp = TempDir::new().expect("temp dir");
    let app = test_router(&temp).await;
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/engagements")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"name":"Juice Shop","objective":{"summary":"Validate exploitable web risks","successCriteria":["Record evidence"],"structuredCriteria":[]},"entryPoints":["juice.local"],"mode":"native","llmProfile":"alternate","authorization":{"network":{"cidrs":["10.10.0.0/24"],"domains":["juice.local"],"ports":[80]},"identities":[],"capabilities":["web.discovery"],"environment":"lab","window":{"startsAt":null,"expiresAt":2000000000}}}"#,
                ))
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::CREATED);
    let engagement: Engagement = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("engagement JSON");
    assert_eq!(engagement.status, EngagementStatus::Draft);
    assert_eq!(engagement.llm_profile, "alternate");
    assert_eq!(
        engagement.objective,
        AssessmentObjective {
            summary: "Validate exploitable web risks".to_string(),
            success_criteria: vec!["Record evidence".to_string()],
            structured_criteria: Vec::new(),
        }
    );

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("/v1/engagements/{}", engagement.id))
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::OK);

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("/v1/engagements/{}/approvals", engagement.id))
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::OK);
    let approvals: Vec<codex_riftx_ipc::PendingApproval> = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("approval list JSON");
    assert_eq!(approvals, Vec::new());

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/engagements")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::OK);
    let engagements: Vec<Engagement> = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("engagement list JSON");
    assert_eq!(engagements, vec![engagement]);
}

#[tokio::test]
async fn engagement_rejects_an_unknown_llm_profile() {
    let temp = TempDir::new().expect("temp dir");
    let app = test_router(&temp).await;
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/engagements")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"name":"Unknown runtime","objective":{"summary":"Reject an unknown runtime","successCriteria":[],"structuredCriteria":[]},"entryPoints":[],"mode":"native","llmProfile":"missing","authorization":{"network":{"cidrs":[],"domains":[],"ports":[]},"identities":[],"capabilities":["web.discovery"],"environment":"lab","window":{"startsAt":null,"expiresAt":2000000000}}}"#,
                ))
                .expect("request"),
        )
        .await
        .expect("response");

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn conversation_endpoint_returns_latest_entries_with_an_older_cursor() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = Engagement {
        id: "eng-conversation".to_string(),
        name: "Conversation lab".to_string(),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Preserve the operator transcript".to_string(),
            success_criteria: Vec::new(),
            structured_criteria: Vec::new(),
        },
        entry_points: vec!["127.0.0.1".to_string()],
        mode: ExecutionMode::Pentest,
        llm_profile: "default".to_string(),
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: vec!["127.0.0.0/8".parse().expect("CIDR")],
                domains: Vec::new(),
                ports: Vec::new(),
            },
            identities: Vec::new(),
            capabilities: Vec::new(),
            environment: EnvironmentClass::Lab,
            window: AuthorizationWindow {
                starts_at: None,
                expires_at: None,
            },
        },
        policy_revision: "revision-1".to_string(),
        thread_id: None,
        created_at: 1,
        updated_at: 1,
    };
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    for (id, role, text, created_at) in [
        (
            "operator-1",
            ConversationRole::Operator,
            "Inspect the authorized target.",
            2,
        ),
        (
            "agent-1",
            ConversationRole::Agent,
            "The first pass is complete.",
            3,
        ),
    ] {
        state
            .store
            .append_conversation_entry(&ConversationEntryDraft {
                id: id.to_string(),
                engagement_id: engagement.id.clone(),
                turn_id: Some("turn-1".to_string()),
                role,
                kind: ConversationKind::Message,
                text: text.to_string(),
                created_at,
            })
            .await
            .expect("store conversation entry");
    }
    let app = build_router(state);
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!(
                    "/v1/engagements/{}/conversation?limit=1",
                    engagement.id
                ))
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::OK);
    let page: serde_json::Value = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("conversation page");
    assert_eq!(page["data"][0]["id"], "agent-1");
    let cursor = page["nextCursor"].as_str().expect("older cursor");

    let response = app
        .oneshot(
            Request::builder()
                .uri(format!(
                    "/v1/engagements/{}/conversation?limit=1&cursor={cursor}",
                    engagement.id
                ))
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");
    let page: serde_json::Value = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("older conversation page");
    assert_eq!(page["data"][0]["id"], "operator-1");
}

#[tokio::test]
async fn artifacts_are_captured_listed_and_exported_from_the_workspace() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = Engagement {
        id: "eng-artifacts".to_string(),
        name: "Artifact lab".to_string(),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Preserve authorized evidence".to_string(),
            success_criteria: Vec::new(),
            structured_criteria: Vec::new(),
        },
        entry_points: vec!["127.0.0.1".to_string()],
        mode: ExecutionMode::Pentest,
        llm_profile: "default".to_string(),
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
        thread_id: None,
        created_at: 1,
        updated_at: 1,
    };
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let workspace = state.config.daemon.workspace_root.join(&engagement.id);
    tokio::fs::create_dir_all(workspace.join("artifacts"))
        .await
        .expect("create artifact directory");
    tokio::fs::write(workspace.join("artifacts/result.json"), br#"{"ok":true}"#)
        .await
        .expect("write artifact");
    let app = build_router(state.clone());

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/engagements/{}/artifacts", engagement.id))
                .header("content-type", "application/json")
                .body(Body::from(r#"{"path":"artifacts/result.json"}"#))
                .expect("request"),
        )
        .await
        .expect("capture response");
    assert_eq!(response.status(), StatusCode::CREATED);
    let artifact: Artifact = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("capture body")
            .to_bytes(),
    )
    .expect("artifact JSON");

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!(
                    "/v1/engagements/{}/artifacts/{}/content",
                    engagement.id, artifact.id
                ))
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("export response");
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response
            .into_body()
            .collect()
            .await
            .expect("export body")
            .to_bytes(),
        br#"{"ok":true}"#.as_slice()
    );

    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/v1/engagements/{}/artifacts", engagement.id))
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("list response");
    let artifacts: Vec<Artifact> = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("list body")
            .to_bytes(),
    )
    .expect("artifact list");
    assert_eq!(artifacts, vec![artifact]);
    let audit = state
        .audit
        .read_records(/*limit*/ 100)
        .await
        .expect("artifact audit");
    let captured = audit
        .iter()
        .find(|record| record.event == "artifact/captured")
        .expect("artifact captured audit");
    assert!(
        captured
            .details
            .as_ref()
            .is_some_and(|details| details.to_string().contains(&artifacts[0].sha256))
    );
}

#[tokio::test]
async fn red_team_mode_can_activate_without_os_guard() {
    let temp = TempDir::new().expect("temp dir");
    let app = test_router(&temp).await;
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/engagements")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"name":"Red team lab","objective":{"summary":"Validate red-team path","successCriteria":[],"structuredCriteria":[]},"entryPoints":["10.10.0.10"],"mode":"redTeam","authorization":{"network":{"cidrs":["10.10.0.0/24"],"domains":[],"ports":[]},"identities":[],"capabilities":["network.discovery"],"environment":"lab","window":{"startsAt":null,"expiresAt":2000000000}}}"#,
                ))
                .expect("request"),
        )
        .await
        .expect("response");
    let engagement: Engagement = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("engagement JSON");
    assert_eq!(engagement.mode, ExecutionMode::RedTeam);

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/engagements/{}/activate", engagement.id))
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");

    assert_eq!(response.status(), StatusCode::OK);
    let activated: Engagement = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("engagement JSON");
    assert_eq!(activated.status, EngagementStatus::Active);
    assert_eq!(activated.mode, ExecutionMode::RedTeam);
}

#[tokio::test]
async fn red_team_draft_can_switch_to_pentest_with_a_new_audited_policy() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let mut engagement = native_engagement(&state, "eng-mode-pentest", EngagementStatus::Draft);
    engagement.mode = ExecutionMode::RedTeam;
    engagement.policy_revision = EffectivePolicy::resolve(
        &state.config.policy,
        engagement.mode,
        &engagement.authorization,
        None,
    )
    .expect("red-team policy")
    .revision;
    let previous_revision = engagement.policy_revision.clone();
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");

    let response = build_router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/engagements/{}/mode", engagement.id))
                .header("content-type", "application/json")
                .body(Body::from(r#"{"mode":"pentest","confirmation":null}"#))
                .expect("request"),
        )
        .await
        .expect("response");

    assert_eq!(response.status(), StatusCode::OK);
    let changed: Engagement = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("engagement JSON");
    assert_eq!(changed.mode, ExecutionMode::Pentest);
    assert_ne!(changed.policy_revision, previous_revision);
    assert_eq!(
        state
            .store
            .engagement(&engagement.id)
            .await
            .expect("stored engagement"),
        changed
    );
    let audit = state
        .audit
        .read_records(/*limit*/ 100)
        .await
        .expect("mode audit");
    let changed_record = audit
        .iter()
        .find(|record| record.event == "engagement/modeChanged")
        .expect("mode changed audit");
    let details = changed_record.details.as_ref().expect("mode audit details");
    assert!(details.to_string().contains(&previous_revision));
    assert!(details.to_string().contains(&changed.policy_revision));
}

#[tokio::test]
async fn mode_switch_is_not_committed_when_critical_audit_fails() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let mut engagement = native_engagement(&state, "eng-mode-audit", EngagementStatus::Draft);
    engagement.mode = ExecutionMode::RedTeam;
    engagement.policy_revision = EffectivePolicy::resolve(
        &state.config.policy,
        engagement.mode,
        &engagement.authorization,
        None,
    )
    .expect("red-team policy")
    .revision;
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    block_audit(&temp).await;

    let response = build_router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/engagements/{}/mode", engagement.id))
                .header("content-type", "application/json")
                .body(Body::from(r#"{"mode":"pentest","confirmation":null}"#))
                .expect("request"),
        )
        .await
        .expect("response");

    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(
        state
            .store
            .engagement(&engagement.id)
            .await
            .expect("stored engagement"),
        engagement
    );
    assert_eq!(
        state.control_status().await.audit.state,
        AuditHealthState::Degraded
    );
}

#[tokio::test]
async fn mode_switch_rejects_active_turns_and_preserves_policy() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = native_engagement(&state, "eng-mode-active", EngagementStatus::Active);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    state.active_turns.write().await.insert(
        engagement.id.clone(),
        ActiveTurn {
            profile_name: "default".to_string(),
            thread_id: "thread-active".to_string(),
            turn_id: "turn-active".to_string(),
        },
    );

    let response = build_router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/engagements/{}/mode", engagement.id))
                .header("content-type", "application/json")
                .body(Body::from(r#"{"mode":"redTeam","confirmation":null}"#))
                .expect("request"),
        )
        .await
        .expect("response");

    assert_eq!(response.status(), StatusCode::CONFLICT);
    let error: serde_json::Value = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("error JSON");
    assert_eq!(error["code"], "mode_switch_conflict");
    assert_eq!(
        state
            .store
            .engagement(&engagement.id)
            .await
            .expect("stored engagement"),
        engagement
    );
}

#[tokio::test]
async fn auto_mode_requires_exact_confirmation_phrase() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = native_engagement(&state, "eng-mode-auto", EngagementStatus::Draft);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let app = build_router(state.clone());
    let uri = format!("/v1/engagements/{}/mode", engagement.id);

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(&uri)
                .header("content-type", "application/json")
                .body(Body::from(r#"{"mode":"auto","confirmation":"AUTO"}"#))
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(uri)
                .header("content-type", "application/json")
                .body(Body::from(format!(
                    r#"{{"mode":"auto","confirmation":"{AUTO_MODE_CONFIRMATION}"}}"#
                )))
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::OK);
    let changed: Engagement = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("engagement JSON");
    assert_eq!(changed.mode, ExecutionMode::Auto);
}

#[tokio::test]
async fn create_auto_engagement_requires_exact_confirmation_phrase() {
    let temp = TempDir::new().expect("temp dir");
    let app = test_router(&temp).await;
    let base = r#"{"name":"Auto lab","objective":{"summary":"Run unattended range work","successCriteria":[],"structuredCriteria":[]},"entryPoints":["10.10.0.10"],"mode":"auto","authorization":{"network":{"cidrs":["10.10.0.0/24"],"domains":[],"ports":[]},"identities":[],"capabilities":["network.discovery"],"environment":"lab","window":{"startsAt":null,"expiresAt":2000000000}}}"#;

    let missing = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/engagements")
                .header("content-type", "application/json")
                .body(Body::from(base))
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(missing.status(), StatusCode::BAD_REQUEST);
    let missing_error: serde_json::Value = serde_json::from_slice(
        &missing
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("error JSON");
    assert_eq!(missing_error["code"], "auto_confirmation_required");

    let wrong = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/engagements")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"name":"Auto lab","objective":{"summary":"Run unattended range work","successCriteria":[],"structuredCriteria":[]},"entryPoints":["10.10.0.10"],"mode":"auto","confirmation":"AUTO","authorization":{"network":{"cidrs":["10.10.0.0/24"],"domains":[],"ports":[]},"identities":[],"capabilities":["network.discovery"],"environment":"lab","window":{"startsAt":null,"expiresAt":2000000000}}}"#,
                ))
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(wrong.status(), StatusCode::BAD_REQUEST);

    let created = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/engagements")
                .header("content-type", "application/json")
                .body(Body::from(format!(
                    r#"{{"name":"Auto lab","objective":{{"summary":"Run unattended range work","successCriteria":[],"structuredCriteria":[]}},"entryPoints":["10.10.0.10"],"mode":"auto","confirmation":"{AUTO_MODE_CONFIRMATION}","authorization":{{"network":{{"cidrs":["10.10.0.0/24"],"domains":[],"ports":[]}},"identities":[],"capabilities":["network.discovery"],"environment":"lab","window":{{"startsAt":null,"expiresAt":2000000000}}}}}}"#
                )))
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(created.status(), StatusCode::CREATED);
    let engagement: Engagement = serde_json::from_slice(
        &created
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("engagement JSON");
    assert_eq!(engagement.mode, ExecutionMode::Auto);
}

#[tokio::test]
async fn expired_authorization_cannot_activate() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let app = build_router(state.clone());
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/engagements")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"name":"Expired lab","objective":{"summary":"Validate expiry enforcement","successCriteria":[],"structuredCriteria":[]},"entryPoints":["10.10.0.10"],"mode":"native","authorization":{"network":{"cidrs":["10.10.0.0/24"],"domains":[],"ports":[]},"identities":[],"capabilities":["network.discovery"],"environment":"lab","window":{"startsAt":null,"expiresAt":1}}}"#,
                ))
                .expect("request"),
        )
        .await
        .expect("response");
    let engagement: Engagement = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("engagement JSON");

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/engagements/{}/activate", engagement.id))
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response");

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert_eq!(
        state
            .store
            .engagement(&engagement.id)
            .await
            .expect("expired engagement")
            .status,
        EngagementStatus::Expired
    );
}

#[tokio::test]
async fn entry_points_must_be_inside_the_authorized_scope() {
    let temp = TempDir::new().expect("temp dir");
    let response = test_router(&temp)
        .await
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/engagements")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"name":"Internal assessment","objective":{"summary":"Map authorized services","successCriteria":[],"structuredCriteria":[]},"entryPoints":["10.20.0.10"],"mode":"native","authorization":{"network":{"cidrs":["10.10.0.0/24"],"domains":[],"ports":[]},"identities":[],"capabilities":["network.discovery"],"environment":"lab","window":{"startsAt":null,"expiresAt":2000000000}}}"#,
                ))
                .expect("request"),
        )
        .await
        .expect("response");

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn managed_policy_rejects_unapproved_capabilities() {
    let temp = TempDir::new().expect("temp dir");
    let response = test_router(&temp)
        .await
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/engagements")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"name":"Denied capability","objective":{"summary":"Validate policy enforcement","successCriteria":[],"structuredCriteria":[]},"entryPoints":["10.10.0.10"],"mode":"native","authorization":{"network":{"cidrs":["10.10.0.0/24"],"domains":[],"ports":[]},"identities":[],"capabilities":["code_execution"],"environment":"lab","window":{"startsAt":null,"expiresAt":2000000000}}}"#,
                ))
                .expect("request"),
        )
        .await
        .expect("response");

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn operational_turn_context_includes_objective_and_multi_asset_graph() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let engagement = Engagement {
        id: "eng-domain".to_string(),
        name: "Authorized domain lab".to_string(),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Validate a path to domain administrator".to_string(),
            success_criteria: vec!["Preserve evidence without persistence".to_string()],
            structured_criteria: Vec::new(),
        },
        entry_points: vec!["10.10.0.10".to_string()],
        mode: ExecutionMode::Pentest,
        llm_profile: "default".to_string(),
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: vec!["10.10.0.0/24".parse().expect("CIDR")],
                domains: vec!["lab.example".to_string()],
                ports: Vec::new(),
            },
            identities: Vec::new(),
            capabilities: vec!["attack_path.analysis".to_string()],
            environment: EnvironmentClass::Lab,
            window: AuthorizationWindow {
                starts_at: None,
                expires_at: Some(2_000_000_000),
            },
        },
        policy_revision: "revision-1".to_string(),
        thread_id: None,
        created_at: 1,
        updated_at: 1,
    };
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    for asset in [
        Asset {
            id: "workstation".to_string(),
            engagement_id: engagement.id.clone(),
            kind: "host".to_string(),
            value: "10.10.0.10".to_string(),
            discovered_at: 2,
        },
        Asset {
            id: "domain-controller".to_string(),
            engagement_id: engagement.id.clone(),
            kind: "host".to_string(),
            value: "10.10.0.20".to_string(),
            discovered_at: 3,
        },
    ] {
        state.store.put_asset(&asset).await.expect("store asset");
    }
    state
        .store
        .put_asset_relation(&AssetRelation {
            id: "relation-1".to_string(),
            engagement_id: engagement.id.clone(),
            source_asset_id: "workstation".to_string(),
            target_asset_id: "domain-controller".to_string(),
            kind: "domainMemberOf".to_string(),
            evidence_id: None,
            discovered_at: 4,
        })
        .await
        .expect("store relation");
    state
        .store
        .put_observation(&Observation {
            id: "observation-1".to_string(),
            engagement_id: engagement.id.clone(),
            subject: StateSubject::Asset {
                asset_id: "domain-controller".to_string(),
            },
            execution_id: None,
            source: "local:nmap".to_string(),
            kind: "serviceDiscovery".to_string(),
            summary: "Domain controller exposes an authorized test service".to_string(),
            confidence_basis_points: 8_000,
            observed_at: 5,
        })
        .await
        .expect("store observation");

    let context = match operational_agent_input(
        &state,
        &engagement.id,
        "Continue the assessment".to_string(),
    )
    .await
    {
        Ok(context) => context,
        Err(error) => panic!("agent context: {}", error.message),
    };

    for expected in [
        "Validate a path to domain administrator",
        "10.10.0.10",
        "10.10.0.20",
        "domainMemberOf",
        "Domain controller exposes an authorized test service",
        "Entry points are starting clues, not the scope boundary",
    ] {
        assert!(context.contains(expected), "missing context: {expected}");
    }
}

#[test]
fn invalid_target_state_maps_to_bad_request() {
    let error = ApiError::from(StateError::InvalidTargetState(
        TargetStateError::InvalidCoverage,
    ));

    assert_eq!(error.status, StatusCode::BAD_REQUEST);
}

#[test]
fn encrypted_state_errors_are_redacted() {
    let error = ApiError::from(StateError::Crypto(CryptoError::KeyStore(
        "sensitive operating-system detail".to_string(),
    )));

    assert_eq!(error.status, StatusCode::INTERNAL_SERVER_ERROR);
    assert_eq!(error.code, "state_error");
    assert_eq!(
        error.message,
        "encrypted engagement state is unavailable".to_string()
    );
}

#[tokio::test]
async fn disabled_llm_profile_is_reported_and_rejected_for_new_engagements() {
    let temp = TempDir::new().expect("temp dir");
    let mut state = test_state(&temp).await;
    Arc::make_mut(&mut state.config)
        .llm
        .profiles
        .get_mut("alternate")
        .expect("alternate profile")
        .enabled = false;
    let app = build_router(state);

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/llm/profiles")
                .body(Body::empty())
                .expect("profile request"),
        )
        .await
        .expect("profile response");
    let list: codex_riftx_ipc::LlmProfileList = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("profile body")
            .to_bytes(),
    )
    .expect("profile list");
    assert!(list.profiles.iter().any(|profile| {
        profile.name == "alternate"
            && profile.state == codex_riftx_ipc::LlmProfileState::Disabled
            && !profile.runtime_ready
    }));

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/engagements")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"name":"Disabled runtime","objective":{"summary":"Reject a disabled runtime","successCriteria":[],"structuredCriteria":[]},"entryPoints":[],"mode":"native","llmProfile":"alternate","authorization":{"network":{"cidrs":[],"domains":[],"ports":[]},"identities":[],"capabilities":["web.discovery"],"environment":"lab","window":{"startsAt":null,"expiresAt":2000000000}}}"#,
                ))
                .expect("engagement request"),
        )
        .await
        .expect("engagement response");
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn profile_runtime_failure_survives_gateway_state_reopen_and_clear() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    state
        .record_runtime_failure("alternate", "upstream timeout".to_string())
        .await;
    assert_eq!(
        state.runtime_failure("alternate").await.as_deref(),
        Some("upstream timeout")
    );
    drop(state);

    let restarted = test_state(&temp).await;
    assert_eq!(
        restarted.runtime_failure("alternate").await.as_deref(),
        Some("upstream timeout")
    );
    restarted.clear_runtime_failure("alternate").await;
    drop(restarted);

    let recovered = test_state(&temp).await;
    assert_eq!(recovered.runtime_failure("alternate").await, None);
}
