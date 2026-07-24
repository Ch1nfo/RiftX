use crate::bridge::DesktopError;
use crate::bridge::DesktopState;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;
use std::time::Duration;
use tauri::Runtime;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::process::CommandEvent;

const STARTUP_ATTEMPTS: usize = 100;
const STARTUP_RETRY_DELAY: Duration = Duration::from_millis(100);
const SHUTDOWN_ATTEMPTS: usize = 50;

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
        let api_key = crate::settings::sidecar_api_key(&config_path).await?;
        let mut command = app
            .shell()
            .sidecar("riftxd")
            .map_err(start_error)?
            .arg("--config")
            .arg(config_path);
        if api_key.is_some() {
            command = command.arg("--llm-api-key-stdin");
        }
        let (mut events, mut child) = command.spawn().map_err(start_error)?;
        if let Some(api_key) = api_key
            && let Err(error) = write_api_key(&mut child, api_key)
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

    pub(crate) async fn stop_after_api_key_delete(
        &self,
        state: &DesktopState,
    ) -> Result<bool, DesktopError> {
        let _transition = self.transition.lock().await;
        if self.stop_owned() {
            wait_until_stopped(state).await?;
            return Ok(false);
        }
        probe(state).await
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

fn write_api_key(
    child: &mut CommandChild,
    api_key: codex_riftx_credentials::LlmApiKey,
) -> Result<(), DesktopError> {
    let mut frame = frame_api_key(api_key)?;
    let result = child.write(&frame).map_err(start_error);
    frame.fill(0);
    result
}

fn frame_api_key(api_key: codex_riftx_credentials::LlmApiKey) -> Result<Vec<u8>, DesktopError> {
    let mut frame = api_key.into_bytes();
    let length = u32::try_from(frame.len())
        .map_err(|_| DesktopError::new("credential_store", "LLM API key is too large"))?;
    frame.reserve(4);
    frame.resize(frame.len() + 4, 0);
    frame.copy_within(0..length as usize, 4);
    frame[..4].copy_from_slice(&length.to_be_bytes());
    Ok(frame)
}

#[cfg(test)]
#[path = "daemon_tests.rs"]
mod tests;
