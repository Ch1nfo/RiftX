mod common;

use anyhow::Context;
use axum::http::StatusCode;
use codex_riftx_core::LlmApiKeySource;
use codex_riftx_ipc::LlmProfileList;
use codex_riftx_ipc::LlmProfileState;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use common::DaemonGuard;
use common::TEST_EPHEMERAL_ENGAGEMENT_KEYS_ENV;
use common::test_config;
use common::wait_for_daemon;
use pretty_assertions::assert_eq;
use std::collections::BTreeMap;
use std::io::Write;
use std::process::Command;
use std::process::Stdio;

#[tokio::test]
async fn starts_headless_daemon_with_profile_keys_from_json_stdin() -> anyhow::Result<()> {
    let temp = tempfile::tempdir()?;
    let mut config = test_config(temp.path(), "http://127.0.0.1:9/v1".to_string());
    for (profile_name, profile) in &mut config.llm.profiles {
        profile.api_key = LlmApiKeySource::Keyring {
            credential: format!("riftx.acceptance.{profile_name}"),
        };
    }
    let config_path = temp.path().join("riftx.toml");
    tokio::fs::write(&config_path, toml::to_string(&config)?)
        .await
        .context("write headless acceptance config")?;

    let mut child = Command::new(codex_utils_cargo_bin::cargo_bin("riftxd")?)
        .arg("--config")
        .arg(&config_path)
        .arg("--llm-api-key-stdin-json")
        .env(TEST_EPHEMERAL_ENGAGEMENT_KEYS_ENV, "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()
        .context("start headless riftxd")?;
    child
        .stdin
        .take()
        .context("headless riftxd stdin is unavailable")?
        .write_all(&serde_json::to_vec(&BTreeMap::from([
            ("default", "headless-default-secret"),
            ("secondary", "headless-secondary-secret"),
        ]))?)
        .context("write headless LLM API keys")?;
    let mut daemon = DaemonGuard { child };

    let client = LocalIpcClient::new(LocalIpcEndpoint::new(&config.daemon.ipc_dir));
    wait_for_daemon(&client, &mut daemon.child).await?;
    let response = client.get("/v1/llm/profiles").await?;
    assert_eq!(response.status(), StatusCode::OK);
    let profiles: LlmProfileList = serde_json::from_slice(&response.bytes().await?)?;
    assert_eq!(
        profiles
            .profiles
            .into_iter()
            .map(|profile| (
                profile.name,
                profile.state,
                profile.configured,
                profile.runtime_ready,
            ))
            .collect::<Vec<_>>(),
        vec![
            ("default".to_string(), LlmProfileState::Ready, true, false,),
            ("secondary".to_string(), LlmProfileState::Ready, true, false,),
        ]
    );

    Ok(())
}
