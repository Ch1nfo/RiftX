use anyhow::Context;
use clap::Parser;
use codex_arg0::Arg0DispatchPaths;
use codex_arg0::arg0_dispatch_or_else;
use codex_riftx_app_server_adapter::RiftxApiKey;
use codex_riftx_core::LlmApiKeySource;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::StateStore;
use codex_riftx_credentials::LlmApiKey;
use codex_riftx_credentials::LlmCredentialStore;
#[cfg(debug_assertions)]
use codex_riftx_crypto::KeyringEngagementCipher;
use codex_riftx_gateway::GatewayState;
use codex_riftx_gateway::ProfileRuntimeManager;
use codex_riftx_gateway::ProfileRuntimeSpec;
use codex_riftx_gateway::build_router;
use codex_riftx_ipc::LocalIpcEndpoint;
use codex_riftx_ipc::LocalIpcListener;
use codex_riftx_skills::SkillCatalog;
use codex_riftx_skills::default_skills_root;
use codex_riftx_tools::ToolScanner;
use std::collections::BTreeMap;
use std::io::IsTerminal;
use std::io::Read;
use std::path::PathBuf;
#[cfg(debug_assertions)]
use std::sync::Arc;
use std::time::Duration;

const MAX_STDIN_API_KEY_BUNDLE_BYTES: usize = 2 * 1024 * 1024;
#[cfg(debug_assertions)]
const TEST_EPHEMERAL_ENGAGEMENT_KEYS_ENV: &str = "RIFTX_TEST_EPHEMERAL_ENGAGEMENT_KEYS";

#[derive(Debug, Parser)]
#[command(version)]
struct Args {
    #[arg(long, default_value = "riftx.toml")]
    config: PathBuf,
    #[arg(long, hide = true)]
    llm_api_key_stdin: bool,
    /// Read a bounded JSON object of Profile API keys from redirected stdin.
    #[arg(long, conflicts_with = "llm_api_key_stdin")]
    llm_api_key_stdin_json: bool,
    #[arg(long, hide = true)]
    validate_profile: Option<String>,
}

fn main() -> anyhow::Result<()> {
    arg0_dispatch_or_else(run)
}

async fn run(arg0_paths: Arg0DispatchPaths) -> anyhow::Result<()> {
    let args = Args::parse();
    let config = RiftxConfig::load_resolved(&args.config).await?;
    let mut stdin_api_keys = if args.llm_api_key_stdin {
        Some(read_llm_api_keys_stdin()?)
    } else if args.llm_api_key_stdin_json {
        Some(read_llm_api_keys_json_stdin()?)
    } else {
        None
    };
    let stdin_bundle_supplied = stdin_api_keys.is_some();
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
    let mut runtime_specs = BTreeMap::new();
    for (profile_name, profile) in &config.llm.profiles {
        let injected_api_key = stdin_api_keys
            .as_mut()
            .and_then(|api_keys| api_keys.remove(profile_name));
        if !profile.enabled {
            eprintln!("riftxd: LLM profile {profile_name:?} is disabled");
            continue;
        }
        match try_load_llm_api_key(
            profile_name,
            &profile.api_key,
            injected_api_key,
            stdin_bundle_supplied,
        )
        .await
        {
            Ok(Some(api_key)) => {
                runtime_specs.insert(
                    profile_name.clone(),
                    ProfileRuntimeSpec {
                        protocol: profile.protocol,
                        runtime_home: config
                            .daemon
                            .runtime_home
                            .join("profiles")
                            .join(profile_name),
                        model: profile.model.clone(),
                        reasoning_effort: profile.reasoning_level.as_str().to_string(),
                        context_window: profile.context_budget,
                        base_url: profile.base_url.clone(),
                        timeout: Duration::from_secs(profile.timeout_seconds),
                        excluded_api_key_envs: excluded_api_key_envs.clone(),
                        api_key,
                        process_path: process_path.clone(),
                        skills_root: skills_root.clone(),
                    },
                );
            }
            Ok(None) => {
                eprintln!(
                    "riftxd: LLM profile {profile_name:?} remains unconfigured until an API key is saved"
                );
            }
            Err(error) => {
                eprintln!("riftxd: LLM profile {profile_name:?} credential unavailable: {error:#}");
            }
        }
    }
    if let Some(api_keys) = stdin_api_keys {
        anyhow::ensure!(
            api_keys.is_empty(),
            "stdin LLM API key bundle contains unknown profiles"
        );
    }
    let runtime_manager = ProfileRuntimeManager::new(runtime_specs, arg0_paths);
    if let Some(profile_name) = args.validate_profile {
        runtime_manager.validate_profile(&profile_name).await?;
        return Ok(());
    }
    let skills = SkillCatalog::empty(skills_root);
    let state =
        GatewayState::new(config, store, skills, tools).with_runtime_manager(runtime_manager);
    state
        .reconcile_after_restart()
        .await
        .context("reconcile active engagements after Gateway restart")?;
    let endpoint = LocalIpcEndpoint::new(&state.config.daemon.ipc_dir);
    let app = build_router(state);
    let listener = LocalIpcListener::bind(endpoint.clone()).await?;
    println!("{endpoint}");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn try_load_llm_api_key(
    profile_name: &str,
    source: &LlmApiKeySource,
    stdin_api_key: Option<LlmApiKey>,
    stdin_bundle_supplied: bool,
) -> anyhow::Result<Option<RiftxApiKey>> {
    if let Some(api_key) = stdin_api_key {
        anyhow::ensure!(
            matches!(source, LlmApiKeySource::Keyring { .. }),
            "stdin LLM API key injection for profile {profile_name:?} requires a keyring-backed configuration"
        );
        return Ok(Some(RiftxApiKey::new(api_key.into_inner())?));
    }
    let api_key = match source {
        LlmApiKeySource::Keyring { credential } => {
            if stdin_bundle_supplied {
                return Ok(None);
            }
            let credential = credential.clone();
            tokio::task::spawn_blocking(move || LlmCredentialStore::default().load(&credential))
                .await
                .context("join operating system credential store task")??
        }
        LlmApiKeySource::Environment { variable } => match std::env::var(variable) {
            Ok(value) => Some(LlmApiKey::new(value)?),
            Err(_) => None,
        },
    };
    api_key
        .map(|api_key| RiftxApiKey::new(api_key.into_inner()))
        .transpose()
        .map_err(Into::into)
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
    decode_llm_api_keys(secret)
}

fn read_llm_api_keys_json_stdin() -> anyhow::Result<BTreeMap<String, LlmApiKey>> {
    anyhow::ensure!(
        !std::io::stdin().is_terminal(),
        "--llm-api-key-stdin-json requires redirected or piped stdin"
    );
    read_llm_api_keys_json(&mut std::io::stdin())
}

fn read_llm_api_keys_json(reader: &mut impl Read) -> anyhow::Result<BTreeMap<String, LlmApiKey>> {
    let mut secret = Vec::new();
    reader
        .take((MAX_STDIN_API_KEY_BUNDLE_BYTES + 1) as u64)
        .read_to_end(&mut secret)
        .context("read LLM API key JSON from stdin")?;
    anyhow::ensure!(
        secret.len() <= MAX_STDIN_API_KEY_BUNDLE_BYTES,
        "LLM API key JSON exceeds the maximum size"
    );
    decode_llm_api_keys(secret)
}

fn decode_llm_api_keys(mut secret: Vec<u8>) -> anyhow::Result<BTreeMap<String, LlmApiKey>> {
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
