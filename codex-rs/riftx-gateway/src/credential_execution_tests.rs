use super::*;
use crate::api::build_router;
use crate::api::tests::block_audit;
use crate::api::tests::native_engagement;
use crate::api::tests::test_state;
use axum::body::Body;
use axum::http::Request;
use axum::http::StatusCode;
use codex_riftx_core::CredentialGrant;
use codex_riftx_core::CredentialKind;
use codex_riftx_core::CredentialReference;
use codex_riftx_credentials::AssessmentSecret;
use codex_riftx_credentials::AssessmentSecretProvider;
use codex_riftx_credentials::CredentialError;
use codex_riftx_tools::ToolScanConfig;
use codex_riftx_tools::ToolScanner;
use http_body_util::BodyExt;
use pretty_assertions::assert_eq;
use std::sync::Arc;
use tempfile::TempDir;
use tower::ServiceExt;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

const SECRET: &str = "gateway-credential-secret";

#[cfg(unix)]
enum FixtureTool {
    Exit(i32),
    Hang,
    SpawnStubbornChild,
}

#[cfg(unix)]
impl FixtureTool {
    fn script(&self) -> String {
        let behavior = match self {
            Self::Exit(exit_code) => format!("exit {exit_code}\n"),
            Self::Hang => "while :; do sleep 1; done\n".to_string(),
            Self::SpawnStubbornChild => concat!(
                "marker=\"$(dirname \"$0\")/child-survived\"\n",
                "pid_file=\"$(dirname \"$0\")/child.pid\"\n",
                "(trap '' TERM; sleep 4; printf survived > \"$marker\") &\n",
                "printf '%s\\n' \"$!\" > \"$pid_file\"\n",
                "wait\n",
            )
            .to_string(),
        };
        format!(
            "#!/bin/sh\nread secret\nprintf 'target=%s secret=%s\\n' \"$1\" \"$secret\"\n{behavior}"
        )
    }
}

struct TestSecretProvider(Option<&'static str>);

impl AssessmentSecretProvider for TestSecretProvider {
    fn load_secret(
        &self,
        _locator: &CredentialLocator,
    ) -> Result<Option<AssessmentSecret>, CredentialError> {
        self.0
            .map(|secret| AssessmentSecret::new(secret.to_string()))
            .transpose()
    }

    fn save_secret(
        &self,
        _locator: &CredentialLocator,
        _secret: AssessmentSecret,
    ) -> Result<(), CredentialError> {
        Ok(())
    }

    fn delete_secret(&self, _locator: &CredentialLocator) -> Result<bool, CredentialError> {
        Ok(self.0.is_some())
    }
}

#[cfg(unix)]
#[tokio::test]
async fn credential_execution_is_target_bound_redacted_persisted_and_audited() {
    let (temp, state, engagement_id, grant_id) = fixture(Some(SECRET), FixtureTool::Exit(0)).await;
    let app = build_router(state.clone());

    let response = post_execution(app, &engagement_id, &grant_id).await;

    assert_eq!(response.status(), StatusCode::OK);
    let body = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    let body_text = String::from_utf8(body.to_vec()).expect("UTF-8 response");
    assert!(!body_text.contains(SECRET));
    let response: CredentialExecutionResponse =
        serde_json::from_str(&body_text).expect("execution response");
    assert_eq!(response.usage.status, CredentialUseStatus::Succeeded);
    assert_eq!(response.execution.status, ExecutionStatus::Completed);
    assert!(response.stdout.contains("[REDACTED]"));
    let agent_output = model_output(&response);
    assert!(agent_output.len() <= 32 * 1024);
    assert!(!agent_output.contains(SECRET));
    let mut large_response = response.clone();
    large_response.stdout = "测".repeat(32 * 1024);
    large_response.stderr = "error".repeat(8 * 1024);
    let bounded_model_output = model_output(&large_response);
    assert!(bounded_model_output.len() <= 32 * 1024);
    assert!(bounded_model_output.is_char_boundary(bounded_model_output.len()));
    assert_eq!(response.execution.stdin_sha256, None);
    assert_eq!(response.execution.stdin_bytes, 0);
    assert_eq!(
        state
            .store
            .credential_uses(&engagement_id)
            .await
            .expect("uses")
            .into_iter()
            .map(ipc_credential_grant_use)
            .collect::<Vec<_>>(),
        vec![response.usage]
    );
    assert_eq!(
        state
            .store
            .executions(&engagement_id)
            .await
            .expect("executions"),
        vec![response.execution]
    );
    let audit = state
        .audit
        .read_records(/*limit*/ 100)
        .await
        .expect("audit");
    let events = audit
        .iter()
        .map(|record| record.event.as_str())
        .collect::<Vec<_>>();
    assert!(events.contains(&"credential/useStarted"));
    assert!(events.contains(&"credential/useCompleted"));
    let raw_audit = tokio::fs::read_to_string(temp.path().join("audit.jsonl"))
        .await
        .expect("raw audit");
    assert!(!raw_audit.contains(SECRET));
}

#[test]
fn dynamic_tool_arguments_reject_argv_and_secret_fields() {
    for forbidden in [
        json!({
            "grantId": "grant-1",
            "tool": "probe",
            "target": {"host": "10.10.0.10", "port": null},
            "args": ["--other-target", "10.20.0.1"],
        }),
        json!({
            "grantId": "grant-1",
            "tool": "probe",
            "target": {"host": "10.10.0.10", "port": null},
            "secret": "must-not-be-accepted",
        }),
    ] {
        assert!(
            serde_json::from_value::<CredentialExecutionParams>(forbidden).is_err(),
            "forbidden dynamic tool field was accepted"
        );
    }
}

#[cfg(unix)]
#[tokio::test]
async fn dynamic_credential_execution_requires_and_revalidates_bound_approval() {
    let (_temp, state, engagement_id, grant_id) = fixture(Some(SECRET), FixtureTool::Exit(0)).await;
    let engagement = state
        .store
        .engagement(&engagement_id)
        .await
        .expect("engagement");
    let params = CredentialExecutionParams {
        grant_id,
        tool: "credential-probe".to_string(),
        target: CredentialUseTarget {
            host: "10.10.0.10".to_string(),
            port: None,
        },
    };
    let mut origin = CredentialExecutionOrigin::DynamicTool {
        thread_id: "thread-1".to_string(),
        tool_call_id: "call-1".to_string(),
        turn_id: "turn-1".to_string(),
        approved_binding: None,
    };
    let intent = credential_execution_intent(&state, &engagement, &params, &origin, "preview")
        .expect("intent");
    assert_eq!(
        decide(
            &intent,
            DecisionContext {
                now: unix_timestamp(),
                authorized_capabilities: &engagement.authorization.capabilities,
            },
        )
        .disposition,
        ExecutionDisposition::RequireApproval
    );
    assert!(!origin.approves(&intent));
    let CredentialExecutionOrigin::DynamicTool {
        approved_binding, ..
    } = &mut origin
    else {
        unreachable!();
    };
    *approved_binding = Some(intent.binding_sha256.clone());
    assert!(origin.approves(&intent));

    let tool_path = state.tools.tools[0].path.clone();
    tokio::fs::write(&tool_path, b"replacement")
        .await
        .expect("replace tool");
    let replaced = credential_execution_intent(&state, &engagement, &params, &origin, "preview")
        .expect("replacement intent");
    assert_ne!(intent.binding_sha256, replaced.binding_sha256);
    assert!(!origin.approves(&replaced));
}

#[cfg(unix)]
#[tokio::test]
async fn audit_failure_closes_the_reservation_without_starting_an_execution() {
    let (temp, state, engagement_id, grant_id) = fixture(Some(SECRET), FixtureTool::Exit(0)).await;
    block_audit(&temp).await;

    let response = post_execution(build_router(state.clone()), &engagement_id, &grant_id).await;

    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(state.credential_processes.read().await.is_empty());
    assert!(
        state
            .store
            .executions(&engagement_id)
            .await
            .expect("executions")
            .is_empty()
    );
    let uses = state
        .store
        .credential_uses(&engagement_id)
        .await
        .expect("uses");
    assert_eq!(uses.len(), 1);
    assert_eq!(
        uses[0].status,
        codex_riftx_core::CredentialUseStatus::ExecutionFailed
    );
}

#[cfg(unix)]
#[tokio::test]
async fn missing_secret_closes_the_reserved_use() {
    let (_temp, state, engagement_id, grant_id) = fixture(None, FixtureTool::Exit(0)).await;
    let response = post_execution(build_router(state.clone()), &engagement_id, &grant_id).await;

    assert_eq!(response.status(), StatusCode::CONFLICT);
    let uses = state
        .store
        .credential_uses(&engagement_id)
        .await
        .expect("uses");
    assert_eq!(uses.len(), 1);
    assert_eq!(
        uses[0].status,
        codex_riftx_core::CredentialUseStatus::ExecutionFailed
    );
}

#[cfg(unix)]
#[tokio::test]
async fn declared_authentication_exit_code_consumes_the_failure_budget() {
    let (_temp, state, engagement_id, grant_id) = fixture(Some(SECRET), FixtureTool::Exit(3)).await;
    let response = post_execution(build_router(state), &engagement_id, &grant_id).await;

    assert_eq!(response.status(), StatusCode::OK);
    let response: CredentialExecutionResponse = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("response");
    assert_eq!(
        response.usage.status,
        CredentialUseStatus::AuthenticationFailed
    );
    assert_eq!(response.execution.status, ExecutionStatus::Failed);
}

#[cfg(unix)]
#[tokio::test]
async fn kill_switch_cancels_an_active_credential_process() {
    let (_temp, state, engagement_id, grant_id) = fixture(Some(SECRET), FixtureTool::Hang).await;
    let app = build_router(state.clone());
    let execution_app = app.clone();
    let execution_engagement_id = engagement_id.clone();
    let execution_grant_id = grant_id.clone();
    let execution = tokio::spawn(async move {
        post_execution(execution_app, &execution_engagement_id, &execution_grant_id).await
    });
    tokio::time::timeout(Duration::from_secs(5), async {
        while state.credential_processes.read().await.is_empty() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("active credential process");
    let kill = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/system/kill")
                .header("content-type", "application/json")
                .body(Body::from("{}"))
                .expect("kill request"),
        )
        .await
        .expect("kill response");

    assert_eq!(kill.status(), StatusCode::OK);
    let response = execution.await.expect("execution task");
    assert_eq!(response.status(), StatusCode::OK);
    let response: CredentialExecutionResponse = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("response");
    assert_eq!(response.usage.status, CredentialUseStatus::Interrupted);
    assert_eq!(response.execution.status, ExecutionStatus::Interrupted);
    assert!(state.credential_processes.read().await.is_empty());
}

#[cfg(unix)]
#[tokio::test]
async fn authorization_deadline_terminates_an_active_credential_process_tree() {
    let (temp, state, engagement_id, grant_id) =
        fixture(Some(SECRET), FixtureTool::SpawnStubbornChild).await;
    let mut engagement = state
        .store
        .engagement(&engagement_id)
        .await
        .expect("engagement");
    engagement.authorization.window.expires_at = Some(unix_timestamp() + 2);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("engagement deadline");
    state.register_authorization_deadline(&engagement).await;
    let app = build_router(state.clone());
    let execution = tokio::spawn({
        let app = app.clone();
        let engagement_id = engagement_id.clone();
        let grant_id = grant_id.clone();
        async move { post_execution(app, &engagement_id, &grant_id).await }
    });
    let child_pid_path = temp.path().join("tools/child.pid");
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if !state.credential_processes.read().await.is_empty()
                && tokio::fs::try_exists(&child_pid_path)
                    .await
                    .unwrap_or(false)
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("active credential process tree");

    let response = tokio::time::timeout(Duration::from_secs(5), execution)
        .await
        .expect("deadline cancellation")
        .expect("execution task");
    assert_eq!(response.status(), StatusCode::OK);
    let response: CredentialExecutionResponse = serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes(),
    )
    .expect("response");
    assert_eq!(response.usage.status, CredentialUseStatus::Interrupted);
    assert_eq!(response.execution.status, ExecutionStatus::Interrupted);
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if state
                .store
                .engagement(&engagement_id)
                .await
                .expect("expired engagement")
                .status
                == EngagementStatus::Expired
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("expired engagement state");

    tokio::time::sleep(Duration::from_secs(3)).await;
    assert!(
        !tokio::fs::try_exists(temp.path().join("tools/child-survived"))
            .await
            .expect("child marker")
    );
}

#[cfg(unix)]
async fn fixture(
    secret: Option<&'static str>,
    tool: FixtureTool,
) -> (TempDir, GatewayState, String, String) {
    let temp = TempDir::new().expect("temp dir");
    let tools_root = temp.path().join("tools");
    tokio::fs::create_dir_all(&tools_root).await.expect("tools");
    let tool_path = tools_root.join("credential-probe");
    tokio::fs::write(&tool_path, tool.script())
        .await
        .expect("tool");
    tokio::fs::set_permissions(&tool_path, std::fs::Permissions::from_mode(0o700))
        .await
        .expect("permissions");
    tokio::fs::write(
        tools_root.join("credential-probe.riftx.toml"),
        concat!(
            "schema_version = 1\n",
            "capabilities = [\"network.discovery\"]\n",
            "[credential]\n",
            "capability = \"network.discovery\"\n",
            "injection = \"stdin\"\n",
            "arguments = [\"{target}\"]\n",
            "authentication_failure_exit_codes = [3]\n",
        ),
    )
    .await
    .expect("metadata");
    let inventory = ToolScanner::new(ToolScanConfig {
        directories: vec![tools_root],
        extra_paths: Vec::new(),
    })
    .scan()
    .await;
    let mut state = test_state(&temp).await;
    state.tools = Arc::new(inventory);
    state = state.with_assessment_credentials(Arc::new(TestSecretProvider(secret)));
    let mut engagement =
        native_engagement(&state, "credential-execution", EngagementStatus::Active);
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("initial engagement");
    let reference = CredentialReference {
        id: "credential-1".to_string(),
        engagement_id: engagement.id.clone(),
        label: "Lab credential".to_string(),
        kind: CredentialKind::Password,
        storage_key: "engagement/credential-execution/credential/credential-1".to_string(),
        username: Some("operator".to_string()),
        domain: Some("LAB".to_string()),
        configured: true,
        created_at: unix_timestamp(),
    };
    state
        .store
        .put_credential_reference(&reference)
        .await
        .expect("reference");
    let grant = CredentialGrant {
        id: "grant-1".to_string(),
        engagement_id: engagement.id.clone(),
        credential_id: reference.id,
        allowed_targets: engagement.authorization.network.clone(),
        allowed_capabilities: vec!["network.discovery".to_string()],
        max_uses: 3,
        max_failures_per_identity: 2,
        starts_at: None,
        expires_at: 2_000_000_000,
        created_at: unix_timestamp(),
        revoked_at: None,
    };
    state
        .store
        .put_credential_grant(&grant)
        .await
        .expect("grant");
    engagement.policy_revision =
        crate::credential_api::resolve_engagement_policy(&state, &engagement, engagement.mode)
            .await
            .expect("policy")
            .revision;
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("engagement");
    (temp, state, engagement.id, grant.id)
}

async fn post_execution(
    app: axum::Router,
    engagement_id: &str,
    grant_id: &str,
) -> axum::response::Response {
    app.oneshot(
        Request::builder()
            .method("POST")
            .uri(format!(
                "/v1/engagements/{engagement_id}/credential-executions"
            ))
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::json!({
                    "grantId": grant_id,
                    "tool": "credential-probe",
                    "target": {"host": "10.10.0.10", "port": null},
                })
                .to_string(),
            ))
            .expect("request"),
    )
    .await
    .expect("response")
}
