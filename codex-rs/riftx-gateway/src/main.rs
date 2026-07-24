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
#[cfg(debug_assertions)]
use codex_riftx_crypto::KeyringEngagementCipher;
use codex_riftx_gateway::GatewayState;
use codex_riftx_gateway::build_router;
use codex_riftx_ipc::LocalIpcEndpoint;
use codex_riftx_ipc::LocalIpcListener;
use codex_riftx_skills::SkillCatalogBuilder;
use codex_riftx_skills::default_skills_root;
use codex_riftx_tools::ToolScanner;
use std::collections::BTreeMap;
use std::io::Read;
use std::path::PathBuf;
#[cfg(debug_assertions)]
use std::sync::Arc;

const MAX_STDIN_API_KEY_BUNDLE_BYTES: usize = 2 * 1024 * 1024;
#[cfg(debug_assertions)]
const TEST_EPHEMERAL_ENGAGEMENT_KEYS_ENV: &str = "RIFTX_TEST_EPHEMERAL_ENGAGEMENT_KEYS";

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
    let mut stdin_api_keys = args
        .llm_api_key_stdin
        .then(read_llm_api_keys_stdin)
        .transpose()?;
    let stdin_bundle_supplied = stdin_api_keys.is_some();
    let default_profile_name = config.llm.default_profile.clone();
    if let Some(parent) = config.daemon.state_db.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    tokio::fs::create_dir_all(&config.daemon.workspace_root).await?;
    let skills_root = resolve_skills_root(config.skills.directory.as_deref())?;
    tokio::fs::create_dir_all(&skills_root)
        .await
        .with_context(|| format!("create Skills Directory {}", skills_root.display()))?;
    #[cfg(debug_assertions)]
    let store = if std::env::var(TEST_EPHEMERAL_ENGAGEMENT_KEYS_ENV).as_deref() == Ok("1") {
        StateStore::open_with_cipher(
            &config.daemon.state_db,
            Arc::new(KeyringEngagementCipher::new(
                codex_keyring_store::tests::MockKeyringStore::default(),
            )),
        )
        .await?
    } else {
        StateStore::open(&config.daemon.state_db).await?
    };
    #[cfg(not(debug_assertions))]
    let store = StateStore::open(&config.daemon.state_db).await?;
    let tools = ToolScanner::new(config.tools.clone()).scan().await;
    let process_path = tools
        .process_path(std::env::var_os("PATH"))?
        .into_string()
        .map_err(|_| anyhow::anyhow!("the effective tool PATH is not valid UTF-8"))?;
    let excluded_api_key_envs = config
        .llm
        .profiles
        .values()
        .filter_map(|profile| match &profile.api_key {
            LlmApiKeySource::Environment { variable } => Some(variable.clone()),
            LlmApiKeySource::Keyring { .. } => None,
        })
        .collect::<Vec<_>>();
    let mut runtimes = Vec::with_capacity(config.llm.profiles.len());
    let mut skills_entry = None;
    for (profile_name, profile) in &config.llm.profiles {
        let injected_api_key = stdin_api_keys
            .as_mut()
            .and_then(|api_keys| api_keys.remove(profile_name));
        let api_key = load_llm_api_key(
            profile_name,
            &profile.api_key,
            injected_api_key,
            stdin_bundle_supplied,
        )
        .await?;
        let runtime = RiftxLlmRuntimeConfig {
            runtime_home: config
                .daemon
                .runtime_home
                .join("profiles")
                .join(profile_name),
            model: profile.model.clone(),
            base_url: profile.base_url.clone(),
            excluded_api_key_envs: excluded_api_key_envs.clone(),
            api_key,
            process_path: process_path.clone(),
        };
        let app_server = RiftxAppServerAdapter::start_embedded(runtime, arg0_paths.clone())
            .await
            .with_context(|| format!("start LLM profile {profile_name:?} runtime"))?;
        let app_server_handle = app_server.request_handle();
        app_server_handle
            .set_exclusive_skill_root(&skills_root)
            .await
            .with_context(|| format!("configure LLM profile {profile_name:?} Skills Directory"))?;
        if profile_name == &default_profile_name {
            skills_entry = Some(
                app_server_handle
                    .list_skills(&config.daemon.workspace_root, /*force_reload*/ true)
                    .await
                    .context("load RiftX Skills Directory")?,
            );
        }
        runtimes.push((profile_name.clone(), app_server_handle, app_server));
    }
    if let Some(api_keys) = stdin_api_keys {
        anyhow::ensure!(
            api_keys.is_empty(),
            "stdin LLM API key bundle contains unknown profiles"
        );
    }
    let skills = SkillCatalogBuilder::new(skills_root)
        .build(skills_entry.context("default LLM profile did not provide a skill catalog")?)
        .await;
    let mut state = GatewayState::new(config, store, skills, tools);
    for (profile_name, app_server_handle, _) in &runtimes {
        state = state.with_app_server(profile_name.clone(), app_server_handle.clone());
    }
    state
        .reconcile_after_restart()
        .await
        .context("reconcile active engagements after Gateway restart")?;
    for (profile_name, _, app_server) in runtimes {
        state.spawn_app_server_event_pump(profile_name, app_server);
    }
    let endpoint = LocalIpcEndpoint::new(&state.config.daemon.ipc_dir);
    let app = build_router(state);
    let listener = LocalIpcListener::bind(endpoint.clone()).await?;
    println!("{endpoint}");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn load_llm_api_key(
    profile_name: &str,
    source: &LlmApiKeySource,
    stdin_api_key: Option<LlmApiKey>,
    stdin_bundle_supplied: bool,
) -> anyhow::Result<RiftxApiKey> {
    if let Some(api_key) = stdin_api_key {
        anyhow::ensure!(
            matches!(source, LlmApiKeySource::Keyring { .. }),
            "stdin LLM API key injection for profile {profile_name:?} requires a keyring-backed configuration"
        );
        return Ok(RiftxApiKey::new(api_key.into_inner())?);
    }
    let api_key = match source {
        LlmApiKeySource::Keyring { credential } => {
            anyhow::ensure!(
                !stdin_bundle_supplied,
                "stdin LLM API key bundle is missing profile {profile_name:?}"
            );
            let credential = credential.clone();
            let missing_credential = credential.clone();

            tokio::task::spawn_blocking(move || LlmCredentialStore::default().load(&credential))
                .await
                .context("join operating system credential store task")??
                .with_context(|| {
                    format!("LLM API key credential {missing_credential:?} is not configured")
                })?
        }
        LlmApiKeySource::Environment { variable } => {
            let value = std::env::var(variable).with_context(|| format!("missing {variable}"))?;
            LlmApiKey::new(value)?
        }
    };
    Ok(RiftxApiKey::new(api_key.into_inner())?)
}

fn read_llm_api_keys_stdin() -> anyhow::Result<BTreeMap<String, LlmApiKey>> {
    read_llm_api_keys(&mut std::io::stdin())
}

fn read_llm_api_keys(reader: &mut impl Read) -> anyhow::Result<BTreeMap<String, LlmApiKey>> {
    let mut length = [0_u8; 4];
    reader
        .read_exact(&mut length)
        .context("read LLM API key bundle frame length from stdin")?;
    let length = u32::from_be_bytes(length) as usize;
    anyhow::ensure!(
        (1..=MAX_STDIN_API_KEY_BUNDLE_BYTES).contains(&length),
        "invalid LLM API key bundle frame length"
    );
    let mut secret = vec![0_u8; length];
    reader
        .read_exact(&mut secret)
        .context("read LLM API key bundle frame from stdin")?;
    let api_keys = serde_json::from_slice::<BTreeMap<String, String>>(&secret)
        .context("decode LLM API key bundle");
    secret.fill(0);
    let api_keys = api_keys?;
    anyhow::ensure!(!api_keys.is_empty(), "LLM API key bundle cannot be empty");
    api_keys
        .into_iter()
        .map(|(profile_name, api_key)| Ok((profile_name, LlmApiKey::new(api_key)?)))
        .collect()
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
