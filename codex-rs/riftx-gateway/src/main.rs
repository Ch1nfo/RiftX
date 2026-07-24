use anyhow::Context;
use clap::Parser;
use codex_arg0::Arg0DispatchPaths;
use codex_arg0::arg0_dispatch_or_else;
use codex_riftx_app_server_adapter::RiftxApiKey;
use codex_riftx_app_server_adapter::RiftxAppServerAdapter;
use codex_riftx_app_server_adapter::RiftxLlmRuntimeConfig;
use codex_riftx_core::LlmApiKeySource;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::StateStore;
use codex_riftx_credentials::LlmApiKey;
use codex_riftx_credentials::LlmCredentialStore;
use codex_riftx_gateway::GatewayState;
use codex_riftx_gateway::build_router;
use codex_riftx_ipc::LocalIpcEndpoint;
use codex_riftx_ipc::LocalIpcListener;
use codex_riftx_skills::SkillCatalogBuilder;
use codex_riftx_skills::default_skills_root;
use codex_riftx_tools::ToolScanner;
use std::io::Read;
use std::path::PathBuf;

const MAX_STDIN_API_KEY_BYTES: usize = 64 * 1024;

#[derive(Debug, Parser)]
struct Args {
    #[arg(long, default_value = "riftx.toml")]
    config: PathBuf,
    #[arg(long, hide = true)]
    llm_api_key_stdin: bool,
}

fn main() -> anyhow::Result<()> {
    arg0_dispatch_or_else(run)
}

async fn run(arg0_paths: Arg0DispatchPaths) -> anyhow::Result<()> {
    let args = Args::parse();
    let config = RiftxConfig::load_resolved(&args.config).await?;
    let stdin_api_key = args
        .llm_api_key_stdin
        .then(read_llm_api_key_stdin)
        .transpose()?;
    let llm_profile = config
        .llm
        .default_profile()
        .context("default LLM profile is missing after config validation")?
        .clone();
    let default_profile_name = config.llm.default_profile.clone();
    let (llm_api_key, excluded_api_key_env) =
        load_llm_api_key(&llm_profile.api_key, stdin_api_key).await?;
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
        model: llm_profile.model,
        base_url: llm_profile.base_url,
        excluded_api_key_env,
        api_key: llm_api_key,
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
    let state = GatewayState::new(config, store, skills, tools)
        .with_app_server(default_profile_name.clone(), app_server_handle);
    state
        .reconcile_after_restart()
        .await
        .context("reconcile active engagements after Gateway restart")?;
    state.spawn_app_server_event_pump(default_profile_name, app_server);
    let endpoint = LocalIpcEndpoint::new(&state.config.daemon.ipc_dir);
    let app = build_router(state);
    let listener = LocalIpcListener::bind(endpoint.clone()).await?;
    println!("{endpoint}");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn load_llm_api_key(
    source: &LlmApiKeySource,
    stdin_api_key: Option<LlmApiKey>,
) -> anyhow::Result<(RiftxApiKey, Option<String>)> {
    if let Some(api_key) = stdin_api_key {
        anyhow::ensure!(
            matches!(source, LlmApiKeySource::Keyring { .. }),
            "stdin LLM API key injection requires a keyring-backed configuration"
        );
        return Ok((RiftxApiKey::new(api_key.into_inner())?, None));
    }
    let (api_key, excluded_variable) = match source {
        LlmApiKeySource::Keyring { credential } => {
            let credential = credential.clone();
            let missing_credential = credential.clone();
            let api_key = tokio::task::spawn_blocking(move || {
                LlmCredentialStore::default().load(&credential)
            })
            .await
            .context("join operating system credential store task")??
            .with_context(|| {
                format!("LLM API key credential {missing_credential:?} is not configured")
            })?;
            (api_key, None)
        }
        LlmApiKeySource::Environment { variable } => {
            let value = std::env::var(variable).with_context(|| format!("missing {variable}"))?;
            (LlmApiKey::new(value)?, Some(variable.clone()))
        }
    };
    Ok((RiftxApiKey::new(api_key.into_inner())?, excluded_variable))
}

fn read_llm_api_key_stdin() -> anyhow::Result<LlmApiKey> {
    read_llm_api_key(&mut std::io::stdin())
}

fn read_llm_api_key(reader: &mut impl Read) -> anyhow::Result<LlmApiKey> {
    let mut length = [0_u8; 4];
    reader
        .read_exact(&mut length)
        .context("read LLM API key frame length from stdin")?;
    let length = u32::from_be_bytes(length) as usize;
    anyhow::ensure!(
        (1..=MAX_STDIN_API_KEY_BYTES).contains(&length),
        "invalid LLM API key frame length"
    );
    let mut secret = vec![0_u8; length];
    reader
        .read_exact(&mut secret)
        .context("read LLM API key frame from stdin")?;
    LlmApiKey::new(String::from_utf8(secret).context("LLM API key frame is not UTF-8")?)
        .map_err(Into::into)
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

#[cfg(test)]
#[path = "main_tests.rs"]
mod tests;
