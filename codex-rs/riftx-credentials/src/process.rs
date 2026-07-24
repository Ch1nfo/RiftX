use crate::AssessmentSecret;
use sha2::Digest;
use sha2::Sha256;
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::io::Write;
use std::path::Path;
use std::path::PathBuf;
use std::process::ExitStatus;
use std::process::Stdio;
use std::time::Duration;
use tempfile::NamedTempFile;
use thiserror::Error;
use tokio::io::AsyncRead;
use tokio::io::AsyncReadExt;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;
use tokio_util::sync::CancellationToken;
use zeroize::Zeroize;
use zeroize::Zeroizing;

const REDACTED: &[u8] = b"[REDACTED]";
const MAX_ARGUMENTS: usize = 256;
const MAX_ARGUMENT_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CredentialInjection {
    Stdin,
    Environment { variable: String },
    FileEnvironment { variable: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CredentialProcessRequest {
    pub program: PathBuf,
    pub expected_sha256: String,
    pub args: Vec<OsString>,
    pub cwd: PathBuf,
    pub environment: BTreeMap<String, OsString>,
    pub injection: CredentialInjection,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialProcessTermination {
    Exited { code: Option<i32> },
    TimedOut,
    Cancelled,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CredentialProcessOutput {
    pub termination: CredentialProcessTermination,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub stdout_sha256: String,
    pub stderr_sha256: String,
    pub stdout_truncated: bool,
    pub stderr_truncated: bool,
}

#[derive(Debug, Clone)]
pub struct CredentialProcessRunner {
    timeout: Duration,
    max_output_bytes: usize,
}

impl CredentialProcessRunner {
    pub fn new(timeout: Duration, max_output_bytes: usize) -> Result<Self, CredentialProcessError> {
        if timeout.is_zero() {
            return Err(CredentialProcessError::InvalidTimeout);
        }
        if max_output_bytes == 0 {
            return Err(CredentialProcessError::InvalidOutputLimit);
        }
        Ok(Self {
            timeout,
            max_output_bytes,
        })
    }

    pub async fn run(
        &self,
        request: CredentialProcessRequest,
        secret: AssessmentSecret,
    ) -> Result<CredentialProcessOutput, CredentialProcessError> {
        self.run_cancellable(request, secret, CancellationToken::new())
            .await
    }

    pub async fn run_cancellable(
        &self,
        request: CredentialProcessRequest,
        secret: AssessmentSecret,
        cancellation: CancellationToken,
    ) -> Result<CredentialProcessOutput, CredentialProcessError> {
        validate_request(&request, &secret)?;
        verify_program(&request.program, &request.expected_sha256).await?;
        let credential_file = prepare_credential_file(&request.injection, &secret)?;
        let mut command = Command::new(&request.program);
        command
            .args(&request.args)
            .current_dir(&request.cwd)
            .env_clear()
            .envs(&request.environment)
            .stdin(match &request.injection {
                CredentialInjection::Stdin => Stdio::piped(),
                CredentialInjection::Environment { .. }
                | CredentialInjection::FileEnvironment { .. } => Stdio::null(),
            })
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        match &request.injection {
            CredentialInjection::Stdin => {}
            CredentialInjection::Environment { variable } => {
                command.env(variable, &secret.0);
            }
            CredentialInjection::FileEnvironment { variable } => {
                let file = credential_file
                    .as_ref()
                    .ok_or(CredentialProcessError::MissingCredentialFile)?;
                command.env(variable, file.path());
            }
        }
        let mut child = command.spawn().map_err(CredentialProcessError::Spawn)?;
        let stdout = child
            .stdout
            .take()
            .ok_or(CredentialProcessError::MissingOutputPipe)?;
        let stderr = child
            .stderr
            .take()
            .ok_or(CredentialProcessError::MissingOutputPipe)?;
        let stdout_task = tokio::spawn(capture(stdout, self.max_output_bytes));
        let stderr_task = tokio::spawn(capture(stderr, self.max_output_bytes));
        let stdin_task = if matches!(request.injection, CredentialInjection::Stdin) {
            let mut stdin = child
                .stdin
                .take()
                .ok_or(CredentialProcessError::MissingInputPipe)?;
            let bytes = Zeroizing::new(secret.0.as_bytes().to_vec());
            Some(tokio::spawn(async move {
                stdin.write_all(&bytes).await?;
                stdin.shutdown().await
            }))
        } else {
            None
        };
        let wait_outcome = tokio::select! {
            status = child.wait() => WaitOutcome::Exited(
                status.map_err(CredentialProcessError::Wait)?
            ),
            () = tokio::time::sleep(self.timeout) => WaitOutcome::Stopped(
                CredentialProcessTermination::TimedOut
            ),
            () = cancellation.cancelled() => WaitOutcome::Stopped(
                CredentialProcessTermination::Cancelled
            ),
        };
        let status = match wait_outcome {
            WaitOutcome::Exited(status) => status,
            WaitOutcome::Stopped(termination) => {
                let _ = child.kill().await;
                let _ = child.wait().await;
                if let Some(stdin_task) = stdin_task {
                    stdin_task.abort();
                    let _ = stdin_task.await;
                }
                stdout_task.abort();
                stderr_task.abort();
                let _ = stdout_task.await;
                let _ = stderr_task.await;
                return Ok(stopped_output(termination));
            }
        };
        if let Some(stdin_task) = stdin_task {
            stdin_task
                .await
                .map_err(CredentialProcessError::Join)?
                .map_err(CredentialProcessError::SecretDelivery)?;
        }
        let stdout = stdout_task.await.map_err(CredentialProcessError::Join)??;
        let stderr = stderr_task.await.map_err(CredentialProcessError::Join)??;
        let safe_stdout = redact(&stdout.bytes, secret.0.as_bytes());
        let safe_stderr = redact(&stderr.bytes, secret.0.as_bytes());
        drop(credential_file);
        Ok(CredentialProcessOutput {
            termination: termination(status),
            stdout_sha256: digest(&safe_stdout),
            stderr_sha256: digest(&safe_stderr),
            stdout: safe_stdout,
            stderr: safe_stderr,
            stdout_truncated: stdout.truncated,
            stderr_truncated: stderr.truncated,
        })
    }
}

#[derive(Debug, Error)]
pub enum CredentialProcessError {
    #[error("credential process timeout must be greater than zero")]
    InvalidTimeout,
    #[error("credential process output limit must be greater than zero")]
    InvalidOutputLimit,
    #[error("credential process program and working directory must be absolute")]
    RelativePath,
    #[error("credential process program must be a regular non-symlink file")]
    InvalidProgram,
    #[error("credential process expected SHA-256 is invalid")]
    InvalidDigest,
    #[error("credential process executable changed after inventory snapshot")]
    DigestMismatch,
    #[error("credential process arguments exceed the safety limit")]
    ArgumentsTooLarge,
    #[error("credential process environment variable is invalid or process-controlling")]
    InvalidEnvironmentVariable,
    #[error("credential process input already contains the secret")]
    SecretInProcessInput,
    #[error("credential process program inspection failed: {0}")]
    Inspect(#[source] std::io::Error),
    #[error("credential process temporary file failed: {0}")]
    TemporaryFile(#[source] std::io::Error),
    #[error("credential process failed to start: {0}")]
    Spawn(#[source] std::io::Error),
    #[error("credential process output pipe was unavailable")]
    MissingOutputPipe,
    #[error("credential process input pipe was unavailable")]
    MissingInputPipe,
    #[error("credential process temporary credential file was unavailable")]
    MissingCredentialFile,
    #[error("credential process wait failed: {0}")]
    Wait(#[source] std::io::Error),
    #[error("credential process secret delivery failed: {0}")]
    SecretDelivery(#[source] std::io::Error),
    #[error("credential process output failed: {0}")]
    Output(#[source] std::io::Error),
    #[error("credential process task failed: {0}")]
    Join(#[source] tokio::task::JoinError),
}

fn validate_request(
    request: &CredentialProcessRequest,
    secret: &AssessmentSecret,
) -> Result<(), CredentialProcessError> {
    if !request.program.is_absolute() || !request.cwd.is_absolute() {
        return Err(CredentialProcessError::RelativePath);
    }
    if !valid_digest(&request.expected_sha256) {
        return Err(CredentialProcessError::InvalidDigest);
    }
    if request.args.len() > MAX_ARGUMENTS
        || request
            .args
            .iter()
            .map(|argument| argument.to_string_lossy().len())
            .sum::<usize>()
            > MAX_ARGUMENT_BYTES
    {
        return Err(CredentialProcessError::ArgumentsTooLarge);
    }
    if request
        .args
        .iter()
        .any(|argument| contains_secret(argument, secret))
        || request
            .environment
            .values()
            .any(|value| contains_secret(value, secret))
    {
        return Err(CredentialProcessError::SecretInProcessInput);
    }
    for variable in request.environment.keys() {
        if !valid_environment_variable(variable) {
            return Err(CredentialProcessError::InvalidEnvironmentVariable);
        }
    }
    if let CredentialInjection::Environment { variable }
    | CredentialInjection::FileEnvironment { variable } = &request.injection
        && (!valid_credential_variable(variable) || request.environment.contains_key(variable))
    {
        return Err(CredentialProcessError::InvalidEnvironmentVariable);
    }
    Ok(())
}

async fn verify_program(path: &Path, expected: &str) -> Result<(), CredentialProcessError> {
    let metadata = tokio::fs::symlink_metadata(path)
        .await
        .map_err(CredentialProcessError::Inspect)?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err(CredentialProcessError::InvalidProgram);
    }
    if hash_file(path).await? != expected {
        return Err(CredentialProcessError::DigestMismatch);
    }
    Ok(())
}

async fn hash_file(path: &Path) -> Result<String, CredentialProcessError> {
    let mut file = tokio::fs::File::open(path)
        .await
        .map_err(CredentialProcessError::Inspect)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .await
            .map_err(CredentialProcessError::Inspect)?;
        if count == 0 {
            return Ok(digest_value(hasher.finalize()));
        }
        hasher.update(&buffer[..count]);
    }
}

fn prepare_credential_file(
    injection: &CredentialInjection,
    secret: &AssessmentSecret,
) -> Result<Option<NamedTempFile>, CredentialProcessError> {
    if !matches!(injection, CredentialInjection::FileEnvironment { .. }) {
        return Ok(None);
    }
    let mut file = tempfile::Builder::new()
        .prefix("riftx-credential-")
        .tempfile()
        .map_err(CredentialProcessError::TemporaryFile)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.as_file()
            .set_permissions(std::fs::Permissions::from_mode(0o600))
            .map_err(CredentialProcessError::TemporaryFile)?;
    }
    file.write_all(secret.0.as_bytes())
        .and_then(|()| file.flush())
        .map_err(CredentialProcessError::TemporaryFile)?;
    Ok(Some(file))
}

struct CapturedOutput {
    bytes: Zeroizing<Vec<u8>>,
    truncated: bool,
}

enum WaitOutcome {
    Exited(ExitStatus),
    Stopped(CredentialProcessTermination),
}

async fn capture(
    mut stream: impl AsyncRead + Unpin,
    limit: usize,
) -> Result<CapturedOutput, CredentialProcessError> {
    let mut captured = Zeroizing::new(Vec::with_capacity(limit.min(8 * 1024)));
    let mut buffer = Zeroizing::new(vec![0_u8; 8 * 1024]);
    let mut truncated = false;
    loop {
        let count = stream
            .read(&mut buffer)
            .await
            .map_err(CredentialProcessError::Output)?;
        if count == 0 {
            break;
        }
        let available = limit.saturating_sub(captured.len());
        let retained = available.min(count);
        captured.extend_from_slice(&buffer[..retained]);
        truncated |= retained < count;
        buffer[..count].zeroize();
    }
    Ok(CapturedOutput {
        bytes: captured,
        truncated,
    })
}

fn stopped_output(termination: CredentialProcessTermination) -> CredentialProcessOutput {
    let empty_digest = digest(&[]);
    CredentialProcessOutput {
        termination,
        stdout: Vec::new(),
        stderr: Vec::new(),
        stdout_sha256: empty_digest.clone(),
        stderr_sha256: empty_digest,
        stdout_truncated: true,
        stderr_truncated: true,
    }
}

fn termination(status: ExitStatus) -> CredentialProcessTermination {
    CredentialProcessTermination::Exited {
        code: status.code(),
    }
}

fn redact(input: &[u8], secret: &[u8]) -> Vec<u8> {
    let mut output = Vec::with_capacity(input.len());
    let mut remaining = input;
    while let Some(position) = remaining
        .windows(secret.len())
        .position(|window| window == secret)
    {
        output.extend_from_slice(&remaining[..position]);
        output.extend_from_slice(REDACTED);
        remaining = &remaining[position + secret.len()..];
    }
    output.extend_from_slice(remaining);
    output
}

fn contains_secret(value: &std::ffi::OsStr, secret: &AssessmentSecret) -> bool {
    value.to_string_lossy().contains(&secret.0)
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn valid_environment_variable(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
        && value
            .bytes()
            .next()
            .is_some_and(|byte| !byte.is_ascii_digit())
}

fn valid_credential_variable(value: &str) -> bool {
    valid_environment_variable(value)
        && !matches!(
            value.to_ascii_uppercase().as_str(),
            "PATH"
                | "PATHEXT"
                | "HOME"
                | "USERPROFILE"
                | "TMP"
                | "TEMP"
                | "TMPDIR"
                | "SHELL"
                | "COMSPEC"
        )
        && !value.to_ascii_uppercase().starts_with("LD_")
        && !value.to_ascii_uppercase().starts_with("DYLD_")
}

fn digest(bytes: &[u8]) -> String {
    digest_value(Sha256::digest(bytes))
}

fn digest_value(bytes: impl AsRef<[u8]>) -> String {
    bytes
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
#[path = "process_tests.rs"]
mod tests;
