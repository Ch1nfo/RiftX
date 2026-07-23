use anyhow::Context;
use clap::Parser;
use codex_arg0::Arg0DispatchPaths;
use codex_arg0::arg0_dispatch_or_else;
use codex_riftx_app_server_adapter::RiftxAppServerAdapter;
use codex_riftx_app_server_adapter::RiftxLlmRuntimeConfig;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::StateStore;
use codex_riftx_gateway::GatewayState;
use codex_riftx_gateway::build_router;
use codex_riftx_ipc::LocalIpcEndpoint;
use codex_riftx_ipc::LocalIpcListener;
use codex_riftx_skills::SkillCatalogBuilder;
use codex_riftx_skills::default_skills_root;
use codex_riftx_tools::ToolScanner;
use std::path::PathBuf;

#[derive(Debug, Parser)]
struct Args {
    #[arg(long, default_value = "riftx.toml")]
    config: PathBuf,
}

fn main() -> anyhow::Result<()> {
    arg0_dispatch_or_else(run)
}

async fn run(arg0_paths: Arg0DispatchPaths) -> anyhow::Result<()> {
    let args = Args::parse();
    let config = RiftxConfig::load_resolved(&args.config).await?;
    let llm_api_key = std::env::var(&config.llm.api_key_env)
        .with_context(|| format!("missing {}", config.llm.api_key_env))?;
    anyhow::ensure!(
        !llm_api_key.trim().is_empty(),
        "{} cannot be empty",
        config.llm.api_key_env
    );
    drop(llm_api_key);
    if let Some(parent) = config.daemon.state_db.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    tokio::fs::create_dir_all(&config.daemon.workspace_root).await?;
    let skills_root = resolve_skills_root(config.skills.directory.as_deref())?;
    tokio::fs::create_dir_all(&skills_root)
        .await
        .with_context(|| format!("create Skills Directory {}", skills_root.display()))?;
    let store = StateStore::open(&config.daemon.state_db).await?;
    let tools = ToolScanner::new(config.tools.clone()).scan().await;
    let process_path = tools
        .process_path(std::env::var_os("PATH"))?
        .into_string()
        .map_err(|_| anyhow::anyhow!("the effective tool PATH is not valid UTF-8"))?;
    let runtime = RiftxLlmRuntimeConfig {
        runtime_home: config.daemon.runtime_home.clone(),
        model: config.llm.model.clone(),
        base_url: config.llm.base_url.clone(),
        api_key_env: config.llm.api_key_env.clone(),
        process_path,
    };
    let app_server = RiftxAppServerAdapter::start_embedded(runtime, arg0_paths)
        .await
        .context("start RiftX model runtime")?;
    let app_server_handle = app_server.request_handle();
    app_server_handle
        .set_exclusive_skill_root(&skills_root)
        .await
        .context("configure exclusive RiftX Skills Directory")?;
    let skills_entry = app_server_handle
        .list_skills(&config.daemon.workspace_root, /*force_reload*/ true)
        .await
        .context("load RiftX Skills Directory")?;
    let skills = SkillCatalogBuilder::new(skills_root)
        .build(skills_entry)
        .await;
    let state = GatewayState::new(config, store, skills, tools).with_app_server(app_server_handle);
    state
        .reconcile_after_restart()
        .await
        .context("reconcile active engagements after Gateway restart")?;
    state.spawn_app_server_event_pump(app_server);
    let endpoint = LocalIpcEndpoint::new(&state.config.daemon.ipc_dir);
    let app = build_router(state);
    let listener = LocalIpcListener::bind(endpoint.clone()).await?;
    println!("{endpoint}");
    axum::serve(listener, app).await?;
    Ok(())
}

fn resolve_skills_root(configured: Option<&std::path::Path>) -> anyhow::Result<PathBuf> {
    let root = configured
        .map(PathBuf::from)
        .or_else(default_skills_root)
        .context("the platform Skills Directory could not be determined")?;
    if root.is_absolute() {
        return Ok(root);
    }
    Ok(std::env::current_dir()?.join(root))
}
