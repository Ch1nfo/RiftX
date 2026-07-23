use anyhow::Context;
use clap::Parser;
use codex_riftx_app_server_adapter::RiftxAppServerAdapter;
use codex_riftx_app_server_adapter::RiftxLlmRuntimeConfig;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::StateStore;
use codex_riftx_gateway::GatewayState;
use codex_riftx_gateway::build_router;
use std::net::IpAddr;
use std::net::SocketAddr;
use std::path::PathBuf;
use tokio::net::TcpListener;

#[derive(Debug, Parser)]
struct Args {
    #[arg(long, default_value = "riftx.toml")]
    config: PathBuf,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    let config = RiftxConfig::load(&args.config).await?;
    let listen = config
        .gateway
        .listen
        .parse::<SocketAddr>()
        .context("gateway.listen must be an IP socket address")?;
    anyhow::ensure!(
        matches!(listen.ip(), IpAddr::V4(ip) if ip.is_loopback())
            || matches!(listen.ip(), IpAddr::V6(ip) if ip.is_loopback()),
        "RiftX Gateway must listen on loopback"
    );
    let token = std::env::var(&config.gateway.operator_token_env)
        .with_context(|| format!("missing {}", config.gateway.operator_token_env))?;
    anyhow::ensure!(!token.is_empty(), "operator bearer token cannot be empty");
    let llm_api_key = std::env::var(&config.llm.api_key_env)
        .with_context(|| format!("missing {}", config.llm.api_key_env))?;
    anyhow::ensure!(
        !llm_api_key.trim().is_empty(),
        "{} cannot be empty",
        config.llm.api_key_env
    );
    drop(llm_api_key);
    if let Some(parent) = config.gateway.state_db.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    tokio::fs::create_dir_all(&config.gateway.workspace_root).await?;
    let store = StateStore::open(&config.gateway.state_db).await?;
    let runtime = RiftxLlmRuntimeConfig {
        runtime_home: config.gateway.runtime_home.clone(),
        model: config.llm.model.clone(),
        base_url: config.llm.base_url.clone(),
        api_key_env: config.llm.api_key_env.clone(),
    };
    let app_server = RiftxAppServerAdapter::start_embedded(runtime)
        .await
        .context("start RiftX model runtime")?;
    let app_server_handle = app_server.request_handle();
    let state = GatewayState::new(config, store).with_app_server(app_server_handle);
    state
        .reconcile_after_restart()
        .await
        .context("reconcile active engagements after Gateway restart")?;
    state.spawn_app_server_event_pump(app_server);
    let app = build_router(state, token);
    let listener = TcpListener::bind(listen).await?;
    println!("http://{}", listener.local_addr()?);
    axum::serve(listener, app).await?;
    Ok(())
}
