use anyhow::Context;
use axum::http::StatusCode;
use codex_riftx_core::ArtifactConfig;
use codex_riftx_core::AuditConfig;
use codex_riftx_core::DaemonConfig;
use codex_riftx_core::LlmApiKeySource;
use codex_riftx_core::LlmConfig;
use codex_riftx_core::LlmProfileConfig;
use codex_riftx_core::LlmReasoningLevel;
use codex_riftx_core::ManagedPolicyConfig;
use codex_riftx_core::RiftxConfig;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_skills::SkillDirectoryConfig;
use codex_riftx_tools::ToolScanConfig;
use std::collections::BTreeMap;
use std::path::Path;
use std::process::Child;
use std::process::Command;
use std::process::Stdio;
use std::time::Duration;

pub(crate) const API_KEY_ENV: &str = "RIFTX_NATIVE_ACCEPTANCE_API_KEY";
pub(crate) const API_KEY: &str = "native-acceptance-secret";
pub(crate) const SECONDARY_API_KEY_ENV: &str = "RIFTX_NATIVE_ACCEPTANCE_SECONDARY_API_KEY";
pub(crate) const SECONDARY_API_KEY: &str = "native-acceptance-secondary-secret";
pub(crate) const TEST_EPHEMERAL_ENGAGEMENT_KEYS_ENV: &str = "RIFTX_TEST_EPHEMERAL_ENGAGEMENT_KEYS";
pub(crate) const STARTUP_ATTEMPTS: usize = 200;
pub(crate) const POLL_INTERVAL: Duration = Duration::from_millis(100);

pub(crate) struct DaemonGuard {
    pub(crate) child: Child,
}

impl Drop for DaemonGuard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub(crate) fn spawn_daemon(config_path: &Path) -> anyhow::Result<DaemonGuard> {
    Ok(DaemonGuard {
        child: Command::new(codex_utils_cargo_bin::cargo_bin("riftxd")?)
            .arg("--config")
            .arg(config_path)
            .env(API_KEY_ENV, API_KEY)
            .env(SECONDARY_API_KEY_ENV, SECONDARY_API_KEY)
            .env(TEST_EPHEMERAL_ENGAGEMENT_KEYS_ENV, "1")
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .spawn()
            .context("start riftxd")?,
    })
}

pub(crate) fn test_config(root: &Path, base_url: String) -> RiftxConfig {
    RiftxConfig {
        daemon: DaemonConfig {
            ipc_dir: root.join("ipc"),
            state_db: root.join("state.sqlite"),
            runtime_home: root.join("runtime"),
            workspace_root: root.join("workspaces"),
        },
        llm: LlmConfig {
            default_profile: "default".to_string(),
            profiles: BTreeMap::from([
                (
                    "default".to_string(),
                    LlmProfileConfig {
                        model: "gpt-5.2".to_string(),
                        base_url: base_url.clone(),
                        api_key: LlmApiKeySource::Environment {
                            variable: API_KEY_ENV.to_string(),
                        },
                        timeout_seconds: 300,
                        reasoning_level: LlmReasoningLevel::High,
                        context_budget: 200_000,
                    },
                ),
                (
                    "secondary".to_string(),
                    LlmProfileConfig {
                        model: "gpt-5.2-secondary".to_string(),
                        base_url,
                        api_key: LlmApiKeySource::Environment {
                            variable: SECONDARY_API_KEY_ENV.to_string(),
                        },
                        timeout_seconds: 120,
                        reasoning_level: LlmReasoningLevel::Medium,
                        context_budget: 100_000,
                    },
                ),
            ]),
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

pub(crate) async fn wait_for_daemon(
    client: &LocalIpcClient,
    child: &mut Child,
) -> anyhow::Result<()> {
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

pub(crate) async fn ensure_status(
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
