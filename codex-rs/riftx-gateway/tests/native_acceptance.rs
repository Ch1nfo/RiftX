use anyhow::Context;
use axum::http::StatusCode;
use codex_riftx_core::ArtifactConfig;
use codex_riftx_core::AuditConfig;
use codex_riftx_core::DaemonConfig;
use codex_riftx_core::Engagement;
use codex_riftx_core::LlmConfig;
use codex_riftx_core::ManagedPolicyConfig;
use codex_riftx_core::RiftxConfig;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use codex_riftx_skills::SkillDirectoryConfig;
use codex_riftx_tools::ToolScanConfig;
use core_test_support::responses;
use serde_json::Value;
use serde_json::json;
use std::process::Child;
use std::process::Command;
use std::process::Stdio;
use std::time::Duration;
use tempfile::TempDir;

const API_KEY_ENV: &str = "RIFTX_NATIVE_ACCEPTANCE_API_KEY";
const API_KEY: &str = "native-acceptance-secret";
const STARTUP_ATTEMPTS: usize = 200;
const COMPLETION_ATTEMPTS: usize = 100;
const POLL_INTERVAL: Duration = Duration::from_millis(100);

struct DaemonGuard {
    child: Child,
}

impl Drop for DaemonGuard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn native_mode_executes_and_audits_a_local_command() -> anyhow::Result<()> {
    let temp = TempDir::new().context("create acceptance directory")?;
    let server = responses::start_mock_server().await;
    let command = native_command();
    let arguments = serde_json::to_string(&json!({
        "cmd": command,
        "yield_time_ms": 1_000,
        "max_output_tokens": 10_000,
    }))?;
    let response_mock = responses::mount_sse_sequence(
        &server,
        vec![
            responses::sse(vec![
                responses::ev_response_created("native-response-1"),
                responses::ev_function_call("native-command-1", "exec_command", &arguments),
                responses::ev_completed("native-response-1"),
            ]),
            responses::sse(vec![
                responses::ev_response_created("native-response-2"),
                responses::ev_assistant_message("native-message-1", "Native execution complete."),
                responses::ev_completed("native-response-2"),
            ]),
        ],
    )
    .await;

    let config = test_config(temp.path(), format!("{}/v1", server.uri()));
    let config_path = temp.path().join("riftx.toml");
    tokio::fs::write(&config_path, toml::to_string(&config)?)
        .await
        .context("write acceptance config")?;
    let mut daemon = DaemonGuard {
        child: Command::new(codex_utils_cargo_bin::cargo_bin("riftxd")?)
            .arg("--config")
            .arg(&config_path)
            .env(API_KEY_ENV, API_KEY)
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .spawn()
            .context("start riftxd")?,
    };
    let client = LocalIpcClient::new(LocalIpcEndpoint::new(&config.daemon.ipc_dir));
    wait_for_daemon(&client, &mut daemon.child).await?;

    let response = client
        .post_json(
            "/v1/engagements",
            serde_json::to_vec(&json!({
                "name": "Native acceptance",
                "objective": {
                    "summary": "Execute a deterministic local command",
                    "successCriteria": ["Preserve a hashed local artifact"],
                    "structuredCriteria": [],
                },
                "entryPoints": ["10.10.10.1"],
                "mode": "native",
                "authorization": {
                    "network": {
                        "cidrs": ["10.10.10.0/24"],
                        "domains": [],
                        "ports": [],
                    },
                    "identities": [],
                    "capabilities": ["evidence.capture"],
                    "environment": "lab",
                    "window": {
                        "startsAt": null,
                        "expiresAt": 4_000_000_000_i64,
                    },
                },
            }))?,
        )
        .await?;
    anyhow::ensure!(
        response.status() == StatusCode::CREATED,
        "create engagement returned {}: {}",
        response.status(),
        String::from_utf8_lossy(&response.bytes().await?)
    );
    let engagement: Engagement = serde_json::from_slice(&response.bytes().await?)?;

    let response = client
        .post(&format!("/v1/engagements/{}/activate", engagement.id))
        .await?;
    ensure_status(response, StatusCode::OK, "activate engagement").await?;
    let event_response = client
        .get(&format!("/v1/engagements/{}/events", engagement.id))
        .await?;
    anyhow::ensure!(
        event_response.status() == StatusCode::OK,
        "subscribe to engagement events returned {}",
        event_response.status()
    );
    let event_collector = tokio::spawn(collect_events_until_turn_completion(
        event_response.into_sse_stream(),
    ));
    let response = client
        .post_json(
            &format!("/v1/engagements/{}/turns", engagement.id),
            serde_json::to_vec(&json!({"input": "Run the deterministic acceptance command."}))?,
        )
        .await?;
    ensure_status(response, StatusCode::ACCEPTED, "start Native turn").await?;

    let report = wait_for_completed_report(&client, &engagement.id)
        .await
        .with_context(|| {
            let requests = response_mock
                .requests()
                .into_iter()
                .map(|request| {
                    let body = request.body_json();
                    let tools = body["tools"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .map(|tool| {
                            json!({
                                "type": tool["type"],
                                "name": tool["name"],
                                "children": tool["tools"].as_array().map(|children| {
                                    children
                                        .iter()
                                        .map(|child| child["name"].clone())
                                        .collect::<Vec<_>>()
                                }),
                            })
                        })
                        .collect::<Vec<_>>();
                    let call_items = body["input"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter(|item| {
                            item["type"]
                                .as_str()
                                .is_some_and(|kind| kind.contains("call"))
                        })
                        .cloned()
                        .collect::<Vec<_>>();
                    json!({"tools": tools, "callItems": call_items})
                })
                .collect::<Vec<_>>();
            format!("captured model requests: {requests:?}")
        })?;
    let execution = report["executions"]
        .as_array()
        .and_then(|executions| {
            executions
                .iter()
                .find(|execution| execution["status"] == "completed")
        })
        .context("completed execution missing from report")?;
    let audit = tokio::fs::read_to_string(&config.audit.jsonl_path)
        .await
        .context("read Native audit")?;
    let stdout_bytes = execution["stdoutBytes"].as_u64().unwrap_or_default();
    let stderr_bytes = execution["stderrBytes"].as_u64().unwrap_or_default();
    anyhow::ensure!(
        stdout_bytes.saturating_add(stderr_bytes)
            >= ("stdout-marker".len() + "stderr-marker".len()) as u64,
        "command output was not captured: {execution}"
    );
    anyhow::ensure!(
        execution["stdoutSha256"].is_string(),
        "stdout hash was not captured: {execution}"
    );
    if stderr_bytes > 0 {
        anyhow::ensure!(
            execution["stderrSha256"].is_string(),
            "stderr hash was not captured: {execution}"
        );
    }
    anyhow::ensure!(
        execution["exitCode"] == 0,
        "Native command did not exit successfully: {execution}"
    );

    let artifact = report["artifacts"]
        .as_array()
        .and_then(|artifacts| {
            artifacts
                .iter()
                .find(|artifact| artifact["path"] == "artifacts/native.txt")
        })
        .context("captured Native artifact missing from report")?;
    anyhow::ensure!(
        artifact["sizeBytes"] == "native-artifact".len() as u64,
        "artifact size mismatch: {artifact}"
    );
    let conversation_response = client
        .get(&format!(
            "/v1/engagements/{}/conversation?limit=200",
            engagement.id
        ))
        .await?;
    anyhow::ensure!(
        conversation_response.status() == StatusCode::OK,
        "conversation query returned {}",
        conversation_response.status()
    );
    let conversation: Value = serde_json::from_slice(&conversation_response.bytes().await?)?;
    let entries = conversation["data"]
        .as_array()
        .context("conversation data missing")?;
    anyhow::ensure!(
        entries.iter().any(|entry| {
            entry["role"] == "operator"
                && entry["text"] == "Run the deterministic acceptance command."
        }),
        "operator message missing from conversation: {conversation}"
    );
    anyhow::ensure!(
        entries.iter().any(|entry| {
            entry["role"] == "agent" && entry["text"] == "Native execution complete."
        }),
        "agent message missing from conversation: {conversation}"
    );
    anyhow::ensure!(audit.contains("execution/completed"));
    anyhow::ensure!(audit.contains("artifact/captured"));
    anyhow::ensure!(!audit.contains(API_KEY));
    let event_kinds = tokio::time::timeout(Duration::from_secs(10), event_collector)
        .await
        .context("event stream did not observe turn completion")?
        .context("event collector task failed")??;
    anyhow::ensure!(event_kinds.iter().any(|kind| kind == "operator/message"));
    anyhow::ensure!(event_kinds.iter().any(|kind| kind == "turn/completed"));
    let requests = response_mock.requests();
    anyhow::ensure!(requests.len() == 2);
    anyhow::ensure!(
        requests
            .iter()
            .all(|request| request.header("authorization") == Some(format!("Bearer {API_KEY}"))),
        "model requests did not use the in-memory API key"
    );
    Ok(())
}

fn test_config(root: &std::path::Path, base_url: String) -> RiftxConfig {
    RiftxConfig {
        daemon: DaemonConfig {
            ipc_dir: root.join("ipc"),
            state_db: root.join("state.sqlite"),
            runtime_home: root.join("runtime"),
            workspace_root: root.join("workspaces"),
        },
        llm: LlmConfig {
            model: "gpt-5.2".to_string(),
            base_url,
            api_key_env: API_KEY_ENV.to_string(),
        },
        policy: ManagedPolicyConfig {
            allowed_capabilities: vec!["evidence.capture".to_string()],
            denied_cidrs: Vec::new(),
            denied_domains: Vec::new(),
        },
        audit: AuditConfig {
            jsonl_path: root.join("audit.jsonl"),
            fsync: true,
        },
        artifacts: ArtifactConfig {
            root: root.join("artifacts"),
            max_bytes_per_engagement: 1024 * 1024,
        },
        skills: SkillDirectoryConfig {
            directory: Some(root.join("skills")),
        },
        tools: ToolScanConfig {
            directories: Vec::new(),
            extra_paths: Vec::new(),
        },
    }
}

async fn wait_for_daemon(client: &LocalIpcClient, child: &mut Child) -> anyhow::Result<()> {
    for _ in 0..STARTUP_ATTEMPTS {
        if let Some(status) = child.try_wait().context("check riftxd status")? {
            anyhow::bail!("riftxd exited during startup with {status}");
        }
        if client
            .get("/v1/system/info")
            .await
            .is_ok_and(|response| response.status() == StatusCode::OK)
        {
            return Ok(());
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("riftxd did not become ready")
}

async fn wait_for_completed_report(
    client: &LocalIpcClient,
    engagement_id: &str,
) -> anyhow::Result<Value> {
    let path = format!("/v1/engagements/{engagement_id}/report?format=json");
    let mut last_report = Value::Null;
    for _ in 0..COMPLETION_ATTEMPTS {
        let response = client.get(&path).await?;
        if response.status() == StatusCode::OK {
            last_report = serde_json::from_slice(&response.bytes().await?)?;
            let execution_completed =
                last_report["executions"]
                    .as_array()
                    .is_some_and(|executions| {
                        executions
                            .iter()
                            .any(|execution| execution["status"] == "completed")
                    });
            let artifact_captured = last_report["artifacts"]
                .as_array()
                .is_some_and(|artifacts| {
                    artifacts
                        .iter()
                        .any(|artifact| artifact["path"] == "artifacts/native.txt")
                });
            if execution_completed && artifact_captured {
                return Ok(last_report);
            }
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    anyhow::bail!("Native turn did not complete with an artifact: {last_report}")
}

async fn collect_events_until_turn_completion(
    mut stream: codex_riftx_ipc::LocalSseStream,
) -> anyhow::Result<Vec<String>> {
    let mut kinds = Vec::new();
    while let Some(frame) = stream.next_event().await? {
        if frame.data.is_empty() {
            continue;
        }
        let event: codex_riftx_ipc::EngagementEvent = serde_json::from_str(&frame.data)?;
        let completed = event.kind == "turn/completed";
        kinds.push(event.kind);
        if completed {
            return Ok(kinds);
        }
    }
    anyhow::bail!("engagement event stream closed before turn completion")
}

async fn ensure_status(
    response: codex_riftx_ipc::LocalIpcResponse,
    expected: StatusCode,
    operation: &str,
) -> anyhow::Result<()> {
    let status = response.status();
    if status == expected {
        return Ok(());
    }
    let body = response.bytes().await?;
    anyhow::bail!(
        "{operation} returned {status}: {}",
        String::from_utf8_lossy(&body)
    )
}

#[cfg(not(windows))]
fn native_command() -> &'static str {
    "test -z \"${RIFTX_NATIVE_ACCEPTANCE_API_KEY+x}\" || exit 97; printf 'stdout-marker'; printf 'stderr-marker' >&2; printf 'native-artifact' > artifacts/native.txt"
}

#[cfg(windows)]
fn native_command() -> &'static str {
    "if (Test-Path Env:RIFTX_NATIVE_ACCEPTANCE_API_KEY) { exit 97 }; [Console]::Out.Write('stdout-marker'); [Console]::Error.Write('stderr-marker'); [IO.File]::WriteAllText('artifacts/native.txt', 'native-artifact')"
}
