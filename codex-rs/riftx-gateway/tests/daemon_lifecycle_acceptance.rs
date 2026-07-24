mod common;

use anyhow::Context;
use axum::http::StatusCode;
use codex_riftx_core::Engagement;
use codex_riftx_ipc::DaemonControlStatus;
use codex_riftx_ipc::DaemonPauseReason;
use codex_riftx_ipc::DaemonRunState;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use common::ensure_status;
use common::spawn_daemon;
use common::test_config;
use common::wait_for_daemon;
use core_test_support::responses;
use pretty_assertions::assert_eq;
use serde_json::json;
use tempfile::TempDir;

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn operator_pause_survives_daemon_process_restart() -> anyhow::Result<()> {
    let harness = LifecycleHarness::start().await?;

    let paused = harness.pause().await?;
    assert_eq!(
        (paused.state, paused.reason),
        (
            DaemonRunState::Paused,
            Some(DaemonPauseReason::OperatorPause),
        )
    );

    let harness = harness.restart().await?;
    let restored = harness.status().await?;
    assert_eq!(
        (restored.state, restored.reason),
        (
            DaemonRunState::Paused,
            Some(DaemonPauseReason::OperatorPause),
        )
    );
    // Create after restart so ephemeral engagement keys from the first process
    // are not required to boot; activate must still be blocked while paused.
    let engagement = harness.create_engagement("pause-lifecycle").await?;
    harness
        .assert_activate_blocked(&engagement.id, "paused activate")
        .await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn kill_switch_survives_daemon_process_restart() -> anyhow::Result<()> {
    let harness = LifecycleHarness::start().await?;

    let killed = harness.kill_switch().await?;
    assert_eq!(
        (killed.state, killed.reason),
        (DaemonRunState::Paused, Some(DaemonPauseReason::KillSwitch))
    );

    let harness = harness.restart().await?;
    let restored = harness.status().await?;
    assert_eq!(
        (restored.state, restored.reason),
        (DaemonRunState::Paused, Some(DaemonPauseReason::KillSwitch))
    );
    let engagement = harness.create_engagement("kill-lifecycle").await?;
    harness
        .assert_activate_blocked(&engagement.id, "kill-switch activate")
        .await?;
    Ok(())
}

struct LifecycleHarness {
    _temp: TempDir,
    _server: wiremock::MockServer,
    config_path: std::path::PathBuf,
    ipc_dir: std::path::PathBuf,
    daemon: common::DaemonGuard,
    client: LocalIpcClient,
}

impl LifecycleHarness {
    async fn start() -> anyhow::Result<Self> {
        let temp = TempDir::new().context("create lifecycle directory")?;
        let server = responses::start_mock_server().await;
        let config = test_config(temp.path(), format!("{}/v1", server.uri()));
        let config_path = temp.path().join("riftx.toml");
        tokio::fs::write(&config_path, toml::to_string(&config)?)
            .await
            .context("write lifecycle config")?;
        let ipc_dir = config.daemon.ipc_dir.clone();
        let mut daemon = spawn_daemon(&config_path)?;
        let client = LocalIpcClient::new(LocalIpcEndpoint::new(&ipc_dir));
        wait_for_daemon(&client, &mut daemon.child).await?;
        Ok(Self {
            _temp: temp,
            _server: server,
            config_path,
            ipc_dir,
            daemon,
            client,
        })
    }

    async fn restart(mut self) -> anyhow::Result<Self> {
        self.daemon
            .child
            .kill()
            .context("kill riftxd for restart")?;
        self.daemon.child.wait().context("wait for riftxd exit")?;
        // Brief pause so the OS releases the UDS / Named Pipe endpoint.
        tokio::time::sleep(common::POLL_INTERVAL).await;
        let mut daemon = spawn_daemon(&self.config_path)?;
        let client = LocalIpcClient::new(LocalIpcEndpoint::new(&self.ipc_dir));
        wait_for_daemon(&client, &mut daemon.child).await?;
        self.daemon = daemon;
        self.client = client;
        Ok(self)
    }

    async fn create_engagement(&self, name: &str) -> anyhow::Result<Engagement> {
        let response = self
            .client
            .post_json(
                "/v1/engagements",
                serde_json::to_vec(&json!({
                    "name": name,
                    "objective": {
                        "summary": "Lifecycle control-plane acceptance",
                        "successCriteria": ["Control state survives restart"],
                        "structuredCriteria": [],
                    },
                    "entryPoints": ["10.10.10.1"],
                    "mode": "native",
                    "llmProfile": "default",
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
        let status = response.status();
        let body = response.bytes().await?;
        if status != StatusCode::CREATED {
            anyhow::bail!(
                "create engagement returned {status}: {}",
                String::from_utf8_lossy(&body)
            );
        }
        Ok(serde_json::from_slice(&body)?)
    }

    async fn pause(&self) -> anyhow::Result<DaemonControlStatus> {
        let response = self.client.post("/v1/system/pause").await?;
        parse_control_status(response, "pause").await
    }

    async fn kill_switch(&self) -> anyhow::Result<DaemonControlStatus> {
        let response = self.client.post("/v1/system/kill").await?;
        parse_control_status(response, "kill").await
    }

    async fn status(&self) -> anyhow::Result<DaemonControlStatus> {
        let response = self.client.get("/v1/system/status").await?;
        parse_control_status(response, "status").await
    }

    async fn assert_activate_blocked(
        &self,
        engagement_id: &str,
        operation: &str,
    ) -> anyhow::Result<()> {
        let response = self
            .client
            .post(&format!("/v1/engagements/{engagement_id}/activate"))
            .await?;
        ensure_status(response, StatusCode::CONFLICT, operation).await
    }
}

async fn parse_control_status(
    response: codex_riftx_ipc::LocalIpcResponse,
    operation: &str,
) -> anyhow::Result<DaemonControlStatus> {
    let status = response.status();
    let body = response.bytes().await?;
    if status != StatusCode::OK {
        anyhow::bail!(
            "{operation} returned {status}: {}",
            String::from_utf8_lossy(&body)
        );
    }
    Ok(serde_json::from_slice(&body)?)
}
