use crate::bridge::DesktopError;
use crate::bridge::DesktopState;
use codex_riftx_core::RiftxConfig;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::AtomicU64;
use std::sync::atomic::Ordering;
use std::time::Duration;
use tauri::Runtime;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::process::CommandEvent;

const STARTUP_ATTEMPTS: usize = 100;
const STARTUP_RETRY_DELAY: Duration = Duration::from_millis(100);
const SHUTDOWN_ATTEMPTS: usize = 50;
const CANDIDATE_TIMEOUT: Duration = Duration::from_secs(30);
static CANDIDATE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Default)]
pub(crate) struct DaemonSupervisor {
    transition: tokio::sync::Mutex<()>,
    child: Mutex<Option<OwnedDaemon>>,
}

struct OwnedDaemon {
    child: CommandChild,
    terminated: Arc<AtomicBool>,
}

impl DaemonSupervisor {
    pub(crate) async fn validate_candidate<R: Runtime>(
        &self,
        app: &tauri::AppHandle<R>,
        config: &RiftxConfig,
        profile_name: &str,
    ) -> Result<(), DesktopError> {
        let profile = config
            .llm
            .profiles
            .get(profile_name)
            .cloned()
            .ok_or_else(|| {
                DesktopError::new(
                    "invalid_config",
                    format!("LLM profile {profile_name:?} is not configured"),
                )
            })?;
        let candidate_root = candidate_root();
        let config_path = candidate_root.join("riftx.toml");
        let mut candidate = config.clone();
        candidate.llm.default_profile = profile_name.to_string();
        candidate.llm.profiles = BTreeMap::from([(profile_name.to_string(), profile)]);
        candidate.daemon.ipc_dir = candidate_root.join("ipc");
        candidate.daemon.state_db = candidate_root.join("state.sqlite");
        candidate.daemon.runtime_home = candidate_root.join("runtime");
        candidate.daemon.workspace_root = candidate_root.join("workspaces");
        candidate.audit.jsonl_path = candidate_root.join("audit.jsonl");
        candidate.artifacts.root = candidate_root.join("artifacts");
        tokio::fs::create_dir_all(&candidate_root)
            .await
            .map_err(start_error)?;
        candidate
            .write_atomic(&config_path)
            .await
            .map_err(start_error)?;

        let result = self.run_candidate(app, &config_path, profile_name).await;
        let _ = tokio::fs::remove_dir_all(&candidate_root).await;
        result
    }

    async fn run_candidate<R: Runtime>(
        &self,
        app: &tauri::AppHandle<R>,
        config_path: &PathBuf,
        profile_name: &str,
    ) -> Result<(), DesktopError> {
        let api_keys = crate::settings::sidecar_api_keys(config_path).await?;
        let mut command = app
            .shell()
            .sidecar("riftxd")
            .map_err(start_error)?
            .arg("--config")
            .arg(config_path)
            .arg("--validate-profile")
            .arg(profile_name);
        if !api_keys.is_empty() {
            command = command.arg("--llm-api-key-stdin");
        }
        let (mut events, mut child) = command.spawn().map_err(start_error)?;
        if !api_keys.is_empty()
            && let Err(error) = write_api_keys(&mut child, api_keys)
        {
            let _ = child.kill();
            return Err(error);
        }
        let outcome = tokio::time::timeout(CANDIDATE_TIMEOUT, async {
            while let Some(event) = events.recv().await {
                match event {
                    CommandEvent::Terminated(payload) => return Ok(payload.code == Some(0)),
                    CommandEvent::Error(_) => return Err(()),
                    CommandEvent::Stdout(_) | CommandEvent::Stderr(_) => {}
                    _ => {}
                }
            }
            Err(())
        })
        .await;
        match outcome {
            Ok(Ok(true)) => Ok(()),
            Ok(Ok(false) | Err(())) => Err(DesktopError::new(
                "profile_candidate_failed",
                format!("LLM profile {profile_name:?} candidate Runtime failed validation"),
            )),
            Err(_) => {
                let _ = child.kill();
                Err(DesktopError::new(
                    "profile_candidate_timeout",
                    format!("LLM profile {profile_name:?} candidate Runtime validation timed out"),
                ))
            }
        }
    }

    pub(crate) async fn ensure_running<R: Runtime>(
        &self,
        app: &tauri::AppHandle<R>,
        state: &DesktopState,
    ) -> Result<(), DesktopError> {
        if probe(state).await? {
            return Ok(());
        }

        let _transition = self.transition.lock().await;
        if probe(state).await? {
            return Ok(());
        }
        self.start_owned(app, state).await
    }

    async fn start_owned<R: Runtime>(
        &self,
        app: &tauri::AppHandle<R>,
        state: &DesktopState,
    ) -> Result<(), DesktopError> {
        self.stop_owned();

        let config_path = state.config_path()?.to_path_buf();
        let api_keys = crate::settings::sidecar_api_keys(&config_path).await?;
        let mut command = app
            .shell()
            .sidecar("riftxd")
            .map_err(start_error)?
            .arg("--config")
            .arg(config_path);
        if !api_keys.is_empty() {
            command = command.arg("--llm-api-key-stdin");
        }
        let (mut events, mut child) = command.spawn().map_err(start_error)?;
        if !api_keys.is_empty()
            && let Err(error) = write_api_keys(&mut child, api_keys)
        {
            let _ = child.kill();
            return Err(error);
        }
        let terminated = Arc::new(AtomicBool::new(false));
        self.replace_child(child, Arc::clone(&terminated))?;
        tauri::async_runtime::spawn(async move {
            while let Some(event) = events.recv().await {
                if matches!(event, CommandEvent::Terminated(_)) {
                    terminated.store(true, Ordering::Release);
                }
            }
        });

        for _ in 0..STARTUP_ATTEMPTS {
            if self.owned_terminated() {
                self.stop_owned();
                return Err(DesktopError::new(
                    "daemon_start_failed",
                    "RiftX daemon exited before it became ready; check the model credential and configuration",
                ));
            }
            match probe(state).await {
                Ok(true) => return Ok(()),
                Ok(false) => tokio::time::sleep(STARTUP_RETRY_DELAY).await,
                Err(error) => {
                    self.stop_owned();
                    return Err(error);
                }
            }
        }

        self.stop_owned();
        Err(DesktopError::new(
            "daemon_start_failed",
            "RiftX daemon did not become ready; check the model credential and configuration",
        ))
    }

    pub(crate) fn stop_owned(&self) -> bool {
        let owned = self.child.lock().ok().and_then(|mut child| child.take());
        if let Some(owned) = owned {
            let _ = owned.child.kill();
            return true;
        }
        false
    }

    pub(crate) async fn reload_after_api_key_save<R: Runtime>(
        &self,
        app: &tauri::AppHandle<R>,
        state: &DesktopState,
    ) -> Result<bool, DesktopError> {
        let _transition = self.transition.lock().await;
        let owned = self.stop_owned();
        if !owned && probe(state).await? {
            return Ok(true);
        }
        if owned {
            wait_until_stopped(state).await?;
        }
        self.start_owned(app, state).await?;
        Ok(false)
    }

    fn replace_child(
        &self,
        child: CommandChild,
        terminated: Arc<AtomicBool>,
    ) -> Result<(), DesktopError> {
        match self.child.lock() {
            Ok(mut current) => {
                *current = Some(OwnedDaemon { child, terminated });
                Ok(())
            }
            Err(_) => {
                let _ = child.kill();
                Err(DesktopError::new(
                    "daemon_start_failed",
                    "daemon process state is unavailable",
                ))
            }
        }
    }

    fn owned_terminated(&self) -> bool {
        self.child
            .lock()
            .ok()
            .and_then(|child| {
                child
                    .as_ref()
                    .map(|owned| owned.terminated.load(Ordering::Acquire))
            })
            .unwrap_or(true)
    }
}

fn candidate_root() -> PathBuf {
    let sequence = CANDIDATE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!(
        "riftx-profile-candidate-{}-{sequence}",
        std::process::id()
    ))
}

async fn probe(state: &DesktopState) -> Result<bool, DesktopError> {
    classify_probe(state.query_daemon_info().await.map(|_| ()))
}

async fn wait_until_stopped(state: &DesktopState) -> Result<(), DesktopError> {
    for _ in 0..SHUTDOWN_ATTEMPTS {
        if !probe(state).await? {
            return Ok(());
        }
        tokio::time::sleep(STARTUP_RETRY_DELAY).await;
    }
    Err(DesktopError::new(
        "daemon_stop_failed",
        "RiftX daemon did not stop in time",
    ))
}

fn classify_probe(result: Result<(), DesktopError>) -> Result<bool, DesktopError> {
    match result {
        Ok(()) => Ok(true),
        Err(error) if error.is_code("daemon_unavailable") => Ok(false),
        Err(error) => Err(error),
    }
}

fn start_error(error: impl std::fmt::Display) -> DesktopError {
    DesktopError::new("daemon_start_failed", error.to_string())
}

fn write_api_keys(
    child: &mut CommandChild,
    api_keys: BTreeMap<String, codex_riftx_credentials::LlmApiKey>,
) -> Result<(), DesktopError> {
    let mut frame = frame_api_keys(api_keys)?;
    let result = child.write(&frame).map_err(start_error);
    frame.fill(0);
    result
}

fn frame_api_keys(
    api_keys: BTreeMap<String, codex_riftx_credentials::LlmApiKey>,
) -> Result<Vec<u8>, DesktopError> {
    let api_keys = api_keys
        .into_iter()
        .map(|(profile_name, api_key)| (profile_name, api_key.into_inner()))
        .collect::<BTreeMap<_, _>>();
    let mut frame = serde_json::to_vec(&api_keys)
        .map_err(|error| DesktopError::new("credential_store", error.to_string()))?;
    let length = u32::try_from(frame.len())
        .map_err(|_| DesktopError::new("credential_store", "LLM API key bundle is too large"))?;
    frame.reserve(4);
    frame.resize(frame.len() + 4, 0);
    frame.copy_within(0..length as usize, 4);
    frame[..4].copy_from_slice(&length.to_be_bytes());
    Ok(frame)
}

#[cfg(test)]
#[path = "daemon_tests.rs"]
mod tests;
