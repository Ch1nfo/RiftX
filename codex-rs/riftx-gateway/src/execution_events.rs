use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use codex_riftx_app_server_adapter::CommandExecutionOutputStream;
use codex_riftx_app_server_adapter::CommandExecutionStatus;
use codex_riftx_app_server_adapter::ItemCompletedNotification;
use codex_riftx_app_server_adapter::ItemStartedNotification;
use codex_riftx_app_server_adapter::ServerNotification;
use codex_riftx_app_server_adapter::ThreadItem;
use codex_riftx_core::Execution;
use codex_riftx_core::ExecutionStatus;
use codex_riftx_core::ExecutionTool;
use sha2::Digest;
use sha2::Sha256;
use std::path::Path;
use std::path::PathBuf;
use tokio::io::AsyncReadExt;

pub(crate) type ExecutionKey = (String, String);

pub(crate) struct ActiveExecution {
    execution: Execution,
    stdout: OutputDigest,
    stderr: OutputDigest,
    stdin: OutputDigest,
}

#[derive(Default)]
struct OutputDigest {
    hasher: Sha256,
    bytes: u64,
}

impl OutputDigest {
    fn update(&mut self, bytes: &[u8]) {
        self.hasher.update(bytes);
        self.bytes = self
            .bytes
            .saturating_add(u64::try_from(bytes.len()).unwrap_or(u64::MAX));
    }

    fn finish(self) -> (Option<String>, u64) {
        let digest = (self.bytes > 0).then(|| hex_digest(self.hasher.finalize()));
        (digest, self.bytes)
    }
}

pub(crate) async fn process_notification(state: &GatewayState, notification: &ServerNotification) {
    match notification {
        ServerNotification::ItemStarted(payload) => start(state, payload).await,
        ServerNotification::CommandExecutionOutputDelta(payload) => {
            let key = (payload.thread_id.clone(), payload.item_id.clone());
            let mut executions = state.active_executions.write().await;
            let Some(execution) = executions.get_mut(&key) else {
                return;
            };
            match payload.stream {
                CommandExecutionOutputStream::Stdout => {
                    execution.stdout.update(payload.delta.as_bytes());
                }
                CommandExecutionOutputStream::Stderr => {
                    execution.stderr.update(payload.delta.as_bytes());
                }
            }
        }
        ServerNotification::TerminalInteraction(payload) => {
            let key = (payload.thread_id.clone(), payload.item_id.clone());
            if let Some(execution) = state.active_executions.write().await.get_mut(&key) {
                execution.stdin.update(payload.stdin.as_bytes());
            }
        }
        ServerNotification::ItemCompleted(payload) => complete(state, payload).await,
        _ => {}
    }
}

async fn start(state: &GatewayState, payload: &ItemStartedNotification) {
    let ThreadItem::CommandExecution {
        id,
        command,
        cwd,
        process_id,
        ..
    } = &payload.item
    else {
        return;
    };
    let key = (payload.thread_id.clone(), id.clone());
    if state.active_executions.read().await.contains_key(&key) {
        return;
    }

    let raw_argv = effective_argv(command);
    let requested_name = raw_argv
        .first()
        .cloned()
        .unwrap_or_else(|| "<unparsed>".to_string());
    let cwd = cwd.to_string();
    let tool = resolve_tool(state, &requested_name, Path::new(&cwd)).await;
    let runner = format!("local:{requested_name}");
    let Some(engagement_id) = state
        .thread_engagements
        .read()
        .await
        .get(&payload.thread_id)
        .cloned()
    else {
        return;
    };
    let task_id = state
        .store
        .task_for_turn(&engagement_id, &payload.turn_id)
        .await
        .ok()
        .flatten()
        .map(|task| task.id);
    let execution = Execution {
        id: id.clone(),
        engagement_id: engagement_id.clone(),
        test_case_id: None,
        task_id,
        turn_id: payload.turn_id.clone(),
        runner,
        status: ExecutionStatus::Running,
        started_at: timestamp_seconds(payload.started_at_ms),
        completed_at: None,
        exit_code: None,
        duration_ms: None,
        argv: redact_argv(&raw_argv),
        command_sha256: digest(command.as_bytes()),
        cwd,
        process_id: process_id.clone(),
        tool,
        tool_inventory_sha256: state.tools.snapshot_sha256.clone(),
        stdout_sha256: None,
        stderr_sha256: None,
        stdin_sha256: None,
        stdout_bytes: 0,
        stderr_bytes: 0,
        stdin_bytes: 0,
    };
    if state.store.put_execution(&execution).await.is_err() {
        return;
    }
    state.active_executions.write().await.insert(
        key,
        ActiveExecution {
            execution: execution.clone(),
            stdout: OutputDigest::default(),
            stderr: OutputDigest::default(),
            stdin: OutputDigest::default(),
        },
    );
    state
        .publish(
            &engagement_id,
            "execution/started",
            serde_json::to_value(execution).unwrap_or_default(),
        )
        .await;
}

async fn complete(state: &GatewayState, payload: &ItemCompletedNotification) {
    let ThreadItem::CommandExecution {
        id,
        process_id,
        status,
        exit_code,
        duration_ms,
        ..
    } = &payload.item
    else {
        return;
    };
    let key = (payload.thread_id.clone(), id.clone());
    let Some(active) = state.active_executions.write().await.remove(&key) else {
        return;
    };
    let mut execution = active.execution;
    execution.status = terminal_status(status, *exit_code);
    execution.completed_at = Some(timestamp_seconds(payload.completed_at_ms));
    execution.exit_code = *exit_code;
    execution.duration_ms = *duration_ms;
    execution.process_id = process_id.clone();
    let (stdout_sha256, stdout_bytes) = active.stdout.finish();
    let (stderr_sha256, stderr_bytes) = active.stderr.finish();
    let (stdin_sha256, stdin_bytes) = active.stdin.finish();
    execution.stdout_sha256 = stdout_sha256;
    execution.stderr_sha256 = stderr_sha256;
    execution.stdin_sha256 = stdin_sha256;
    execution.stdout_bytes = stdout_bytes;
    execution.stderr_bytes = stderr_bytes;
    execution.stdin_bytes = stdin_bytes;
    if state.store.put_execution(&execution).await.is_err() {
        return;
    }
    let engagement_id = execution.engagement_id.clone();
    state
        .publish(
            &engagement_id,
            "execution/completed",
            serde_json::to_value(execution).unwrap_or_default(),
        )
        .await;
}

pub(crate) async fn finish_turn(
    state: &GatewayState,
    engagement_id: &str,
    turn_id: &str,
    status: ExecutionStatus,
) {
    let keys = state
        .active_executions
        .read()
        .await
        .iter()
        .filter(|(_, active)| {
            active.execution.engagement_id == engagement_id && active.execution.turn_id == turn_id
        })
        .map(|(key, _)| key.clone())
        .collect::<Vec<_>>();
    for key in keys {
        let Some(active) = state.active_executions.write().await.remove(&key) else {
            continue;
        };
        let mut execution = active.execution;
        execution.status = status;
        execution.completed_at = Some(unix_timestamp());
        let (stdout_sha256, stdout_bytes) = active.stdout.finish();
        let (stderr_sha256, stderr_bytes) = active.stderr.finish();
        let (stdin_sha256, stdin_bytes) = active.stdin.finish();
        execution.stdout_sha256 = stdout_sha256;
        execution.stderr_sha256 = stderr_sha256;
        execution.stdin_sha256 = stdin_sha256;
        execution.stdout_bytes = stdout_bytes;
        execution.stderr_bytes = stderr_bytes;
        execution.stdin_bytes = stdin_bytes;
        let _ = state.store.put_execution(&execution).await;
        state
            .publish(
                engagement_id,
                match status {
                    ExecutionStatus::Interrupted => "execution/interrupted",
                    ExecutionStatus::Failed => "execution/failed",
                    ExecutionStatus::Pending
                    | ExecutionStatus::Running
                    | ExecutionStatus::Completed => "execution/aborted",
                },
                serde_json::to_value(execution).unwrap_or_default(),
            )
            .await;
    }
}

fn terminal_status(status: &CommandExecutionStatus, exit_code: Option<i32>) -> ExecutionStatus {
    match status {
        CommandExecutionStatus::Completed if exit_code == Some(0) => ExecutionStatus::Completed,
        CommandExecutionStatus::Completed
        | CommandExecutionStatus::Failed
        | CommandExecutionStatus::Declined
        | CommandExecutionStatus::InProgress => ExecutionStatus::Failed,
    }
}

fn effective_argv(command: &str) -> Vec<String> {
    let outer = shlex::split(command).unwrap_or_else(|| vec![command.to_string()]);
    let Some(executable) = outer.first().and_then(|value| {
        Path::new(value)
            .file_name()
            .and_then(|name| name.to_str())
            .map(str::to_ascii_lowercase)
    }) else {
        return outer;
    };
    let shell_argument = if matches!(executable.as_str(), "sh" | "bash" | "zsh" | "fish" | "dash") {
        outer
            .windows(2)
            .find(|pair| pair[0].starts_with('-') && pair[0].contains('c'))
            .map(|pair| pair[1].as_str())
    } else if matches!(executable.as_str(), "cmd" | "cmd.exe") {
        outer
            .windows(2)
            .find(|pair| pair[0].eq_ignore_ascii_case("/c"))
            .map(|pair| pair[1].as_str())
    } else if matches!(
        executable.as_str(),
        "powershell" | "powershell.exe" | "pwsh" | "pwsh.exe"
    ) {
        outer
            .windows(2)
            .find(|pair| {
                pair[0].eq_ignore_ascii_case("-command") || pair[0].eq_ignore_ascii_case("-c")
            })
            .map(|pair| pair[1].as_str())
    } else {
        None
    };
    shell_argument
        .and_then(shlex::split)
        .filter(|argv| !argv.is_empty())
        .unwrap_or(outer)
}

fn redact_argv(argv: &[String]) -> Vec<String> {
    let mut redact_next = false;
    argv.iter()
        .map(|argument| {
            if redact_next {
                redact_next = false;
                return "[REDACTED]".to_string();
            }
            if let Some((key, _)) = argument.split_once('=')
                && is_secret_name(key)
            {
                return format!("{key}=[REDACTED]");
            }
            if let Some((key, _)) = argument.split_once(':')
                && is_secret_name(key)
            {
                return format!("{key}: [REDACTED]");
            }
            if argument.contains("://")
                && argument
                    .split_once("://")
                    .and_then(|(_, authority)| authority.split_once('@'))
                    .is_some_and(|(user_info, _)| user_info.contains(':'))
            {
                return "[REDACTED_URL]".to_string();
            }
            if argument
                .to_ascii_lowercase()
                .trim_start()
                .starts_with("bearer ")
            {
                return "[REDACTED]".to_string();
            }
            if is_secret_name(argument.trim_start_matches('-')) {
                redact_next = true;
            }
            argument.clone()
        })
        .collect()
}

fn is_secret_name(value: &str) -> bool {
    let normalized = value.to_ascii_lowercase().replace('_', "-");
    [
        "password",
        "passwd",
        "passphrase",
        "token",
        "secret",
        "api-key",
        "authorization",
        "credential",
        "cookie",
    ]
    .iter()
    .any(|marker| normalized.contains(marker))
}

async fn resolve_tool(
    state: &GatewayState,
    requested_name: &str,
    cwd: &Path,
) -> Option<ExecutionTool> {
    if requested_name == "<unparsed>" {
        return None;
    }
    let resolved_path = resolve_path(requested_name, cwd, &state.tool_search_path).await;
    let Some(path) = resolved_path else {
        return Some(unresolved_tool(requested_name));
    };
    let sha256 = hash_file(&path).await.ok();
    let mut managed_metadata_sha256 = None;
    let mut managed = false;
    for tool in &state.tools.tools {
        let same_path = tool.path == path
            || tokio::fs::canonicalize(&tool.path)
                .await
                .is_ok_and(|candidate| candidate == path);
        if same_path {
            managed = true;
            managed_metadata_sha256.clone_from(&tool.metadata_sha256);
            break;
        }
    }
    Some(ExecutionTool {
        requested_name: requested_name.to_string(),
        resolved_path: Some(path.to_string_lossy().into_owned()),
        sha256,
        metadata_sha256: managed_metadata_sha256,
        version: None,
        managed,
    })
}

async fn resolve_path(
    requested_name: &str,
    cwd: &Path,
    search_path: &[PathBuf],
) -> Option<PathBuf> {
    let requested = Path::new(requested_name);
    if requested.is_absolute() || requested.components().count() > 1 {
        let candidate = if requested.is_absolute() {
            requested.to_path_buf()
        } else {
            cwd.join(requested)
        };
        return executable_file(candidate).await;
    }
    for directory in search_path {
        for candidate in executable_candidates(directory.join(requested_name)) {
            if let Some(path) = executable_file(candidate).await {
                return Some(path);
            }
        }
    }
    None
}

fn executable_candidates(path: PathBuf) -> Vec<PathBuf> {
    #[cfg(windows)]
    {
        if path.extension().is_some() {
            return vec![path];
        }
        return std::env::var("PATHEXT")
            .unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".to_string())
            .split(';')
            .map(|extension| {
                let extension = extension.trim().trim_start_matches('.');
                path.with_extension(extension)
            })
            .collect();
    }
    #[cfg(not(windows))]
    {
        vec![path]
    }
}

async fn executable_file(path: PathBuf) -> Option<PathBuf> {
    let metadata = tokio::fs::metadata(&path).await.ok()?;
    if !metadata.is_file() {
        return None;
    }
    tokio::fs::canonicalize(path).await.ok()
}

async fn hash_file(path: &Path) -> std::io::Result<String> {
    let mut file = tokio::fs::File::open(path).await?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file.read(&mut buffer).await?;
        if count == 0 {
            return Ok(hex_digest(hasher.finalize()));
        }
        hasher.update(&buffer[..count]);
    }
}

fn unresolved_tool(requested_name: &str) -> ExecutionTool {
    ExecutionTool {
        requested_name: requested_name.to_string(),
        resolved_path: None,
        sha256: None,
        metadata_sha256: None,
        version: None,
        managed: false,
    }
}

fn timestamp_seconds(milliseconds: i64) -> i64 {
    if milliseconds > 0 {
        milliseconds / 1_000
    } else {
        unix_timestamp()
    }
}

fn digest(bytes: &[u8]) -> String {
    hex_digest(Sha256::digest(bytes))
}

fn hex_digest(bytes: impl AsRef<[u8]>) -> String {
    bytes
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
#[path = "execution_events_tests.rs"]
mod tests;
