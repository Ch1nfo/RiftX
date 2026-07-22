use anyhow::Context;
use clap::Parser;
use codex_riftx_app_server_adapter::RiftxAppServerAdapter;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::StateStore;
use codex_riftx_gateway::GatewayState;
use codex_riftx_gateway::build_router;
use codex_riftx_manager_client::ManagerClient;
use std::net::IpAddr;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::time::Duration;
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
    if let Some(parent) = config.gateway.state_db.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let store = StateStore::open(&config.gateway.state_db).await?;
    let manager = ManagerClient::new(
        &config.manager.socket,
        Duration::from_millis(config.manager.request_timeout_ms),
    )?;
    let app_server = RiftxAppServerAdapter::start_embedded()
        .await
        .context("start embedded Codex App Server")?;
    let app_server_handle = app_server.request_handle();
    let state = GatewayState::new(config, store, manager).with_app_server(app_server_handle);
    state
        .reconcile_after_restart()
        .await
        .context("reconcile active engagements after Gateway restart")?;
    state.spawn_app_server_event_pump(app_server);
    state.spawn_manager_event_pump();
    let app = build_router(state, token);
    let listener = TcpListener::bind(listen).await?;
    println!("http://{}", listener.local_addr()?);
    axum::serve(listener, app).await?;
    Ok(())
}
