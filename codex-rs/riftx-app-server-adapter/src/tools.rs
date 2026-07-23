use codex_app_server_protocol::DynamicToolFunctionSpec;
use codex_app_server_protocol::DynamicToolSpec;
use codex_exec_server::EnvironmentManager;
use codex_exec_server::ExecParams;
use codex_exec_server::ExecProcessEvent;
use codex_exec_server::ProcessId;
use codex_exec_server_protocol::ExecOutputStream;
use codex_utils_path_uri::PathUri;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use serde_json::json;
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::AtomicU64;
use std::sync::atomic::Ordering;
use std::time::Duration;
use thiserror::Error;

const MAX_TARGETS: usize = 128;
const MAX_OUTPUT_BYTES: usize = 4 * 1024 * 1024;
const TOOL_TIMEOUT: Duration = Duration::from_secs(300);
static NEXT_PROCESS_ID: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Error)]
pub enum StructuredToolError {
    #[error("unknown structured tool {0}")]
    UnknownTool(String),
    #[error("invalid structured tool arguments: {0}")]
    InvalidArguments(String),
    #[error("environment {0} is not registered")]
    EnvironmentNotFound(String),
    #[error("structured tool failed to start: {0}")]
    Start(String),
    #[error("structured tool process failed: {0}")]
    Process(String),
    #[error("structured tool exceeded the five minute timeout")]
    Timeout,
    #[error("structured tool output exceeded {MAX_OUTPUT_BYTES} bytes")]
    OutputLimit,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StructuredToolRequest {
    Nmap(NmapArgs),
    Httpx(TargetsArgs),
    Nuclei(NucleiArgs),
    Ffuf(FfufArgs),
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct NmapArgs {
    pub targets: Vec<String>,
    #[serde(default)]
    pub ports: Vec<u16>,
    #[serde(default)]
    pub service_detection: bool,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TargetsArgs {
    pub targets: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct NucleiArgs {
    pub targets: Vec<String>,
    #[serde(default)]
    pub tags: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct FfufArgs {
    pub url: String,
    #[serde(default)]
    pub wordlist: Wordlist,
}

#[derive(Debug, Default, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum Wordlist {
    #[default]
    Common,
    DirectoriesMedium,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct StructuredToolOutput {
    pub tool: String,
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
}

impl StructuredToolRequest {
    pub fn parse(tool: &str, arguments: Value) -> Result<Self, StructuredToolError> {
        let request = match tool {
            "rt_nmap" => Self::Nmap(parse(arguments)?),
            "rt_httpx" => Self::Httpx(parse(arguments)?),
            "rt_nuclei" => Self::Nuclei(parse(arguments)?),
            "rt_ffuf" => Self::Ffuf(parse(arguments)?),
            _ => return Err(StructuredToolError::UnknownTool(tool.to_string())),
        };
        request.validate()?;
        Ok(request)
    }

    pub fn name(&self) -> &'static str {
        match self {
            Self::Nmap(_) => "rt_nmap",
            Self::Httpx(_) => "rt_httpx",
            Self::Nuclei(_) => "rt_nuclei",
            Self::Ffuf(_) => "rt_ffuf",
        }
    }

    pub fn targets(&self) -> Vec<&str> {
        match self {
            Self::Nmap(args) => args.targets.iter().map(String::as_str).collect(),
            Self::Httpx(args) => args.targets.iter().map(String::as_str).collect(),
            Self::Nuclei(args) => args.targets.iter().map(String::as_str).collect(),
            Self::Ffuf(args) => vec![args.url.as_str()],
        }
    }

    fn validate(&self) -> Result<(), StructuredToolError> {
        let targets = self.targets();
        if targets.is_empty() || targets.len() > MAX_TARGETS {
            return Err(StructuredToolError::InvalidArguments(format!(
                "target count must be between 1 and {MAX_TARGETS}"
            )));
        }
        if targets
            .iter()
            .any(|target| target.is_empty() || target.starts_with('-') || target.len() > 2048)
        {
            return Err(StructuredToolError::InvalidArguments(
                "targets must be non-empty and cannot start with '-'".to_string(),
            ));
        }
        if let Self::Nmap(args) = self
            && args.ports.len() > 128
        {
            return Err(StructuredToolError::InvalidArguments(
                "at most 128 ports may be requested".to_string(),
            ));
        }
        if let Self::Nuclei(args) = self
            && args.tags.iter().any(|tag| {
                tag.is_empty()
                    || tag.len() > 64
                    || !tag.chars().all(|character| {
                        character.is_ascii_alphanumeric() || "-_".contains(character)
                    })
            })
        {
            return Err(StructuredToolError::InvalidArguments(
                "nuclei tags may contain only letters, digits, '-' and '_'".to_string(),
            ));
        }
        if let Self::Ffuf(args) = self
            && !args.url.contains("FUZZ")
        {
            return Err(StructuredToolError::InvalidArguments(
                "ffuf URL must contain FUZZ".to_string(),
            ));
        }
        Ok(())
    }

    fn argv(&self) -> Vec<String> {
        match self {
            Self::Nmap(args) => {
                let mut argv = vec!["nmap".to_string(), "-oX".to_string(), "-".to_string()];
                if args.service_detection {
                    argv.push("-sV".to_string());
                }
                if !args.ports.is_empty() {
                    argv.extend(["-p".to_string(), join_values(&args.ports)]);
                }
                argv.extend(args.targets.clone());
                argv
            }
            Self::Httpx(args) => repeated_targets("httpx", &["-silent", "-json"], &args.targets),
            Self::Nuclei(args) => {
                let mut argv = repeated_targets(
                    "nuclei",
                    &[
                        "-silent",
                        "-jsonl",
                        "-duc",
                        "-t",
                        "/opt/riftx/nuclei-templates",
                    ],
                    &args.targets,
                );
                if !args.tags.is_empty() {
                    argv.extend(["-tags".to_string(), args.tags.join(",")]);
                }
                argv
            }
            Self::Ffuf(args) => vec![
                "ffuf".to_string(),
                "-s".to_string(),
                "-json".to_string(),
                "-u".to_string(),
                args.url.clone(),
                "-w".to_string(),
                match args.wordlist {
                    Wordlist::Common => "/opt/riftx/wordlists/common.txt",
                    Wordlist::DirectoriesMedium => {
                        "/opt/riftx/wordlists/directory-list-2.3-medium.txt"
                    }
                }
                .to_string(),
            ],
        }
    }
}

pub fn structured_tool_specs() -> Vec<DynamicToolSpec> {
    vec![
        function_spec(
            "rt_nmap",
            "Discover open ports and services on authorized targets.",
            json!({"type":"object","additionalProperties":false,"required":["targets"],"properties":{"targets":{"type":"array","minItems":1,"maxItems":128,"items":{"type":"string"}},"ports":{"type":"array","maxItems":128,"items":{"type":"integer","minimum":1,"maximum":65535}},"serviceDetection":{"type":"boolean"}}}),
        ),
        function_spec(
            "rt_httpx",
            "Probe authorized HTTP targets and return JSONL observations.",
            target_schema(),
        ),
        function_spec(
            "rt_nuclei",
            "Run allowlisted Nuclei templates against authorized targets.",
            json!({"type":"object","additionalProperties":false,"required":["targets"],"properties":{"targets":{"type":"array","minItems":1,"maxItems":128,"items":{"type":"string"}},"tags":{"type":"array","items":{"type":"string","pattern":"^[A-Za-z0-9_-]+$"}}}}),
        ),
        function_spec(
            "rt_ffuf",
            "Fuzz one authorized URL using a RiftX-managed wordlist.",
            json!({"type":"object","additionalProperties":false,"required":["url"],"properties":{"url":{"type":"string","pattern":"FUZZ"},"wordlist":{"type":"string","enum":["common","directoriesMedium"]}}}),
        ),
    ]
}

pub(crate) async fn execute_structured_tool(
    manager: &Arc<EnvironmentManager>,
    environment_id: &str,
    request: StructuredToolRequest,
) -> Result<StructuredToolOutput, StructuredToolError> {
    let environment = manager
        .get_environment(environment_id)
        .ok_or_else(|| StructuredToolError::EnvironmentNotFound(environment_id.to_string()))?;
    environment
        .wait_until_ready()
        .await
        .map_err(|error| StructuredToolError::Start(error.to_string()))?;
    let started = environment
        .get_exec_backend()
        .start(ExecParams {
            process_id: ProcessId::new(format!(
                "riftx-tool-{}",
                NEXT_PROCESS_ID.fetch_add(1, Ordering::Relaxed)
            )),
            argv: request.argv(),
            cwd: PathUri::parse("file:///workspace")
                .map_err(|error| StructuredToolError::Start(error.to_string()))?,
            env_policy: None,
            env: HashMap::new(),
            tty: false,
            pipe_stdin: false,
            arg0: None,
            sandbox: None,
            enforce_managed_network: false,
            managed_network: None,
            network_proxy: None,
        })
        .await
        .map_err(|error| StructuredToolError::Start(error.to_string()))?;
    let process = started.process;
    let result = tokio::time::timeout(TOOL_TIMEOUT, collect_output(Arc::clone(&process))).await;
    match result {
        Ok(result) => {
            let (exit_code, stdout, stderr) = result?;
            Ok(StructuredToolOutput {
                tool: request.name().to_string(),
                exit_code,
                stdout,
                stderr,
            })
        }
        Err(_) => {
            let _ = process.terminate().await;
            Err(StructuredToolError::Timeout)
        }
    }
}

async fn collect_output(
    process: Arc<dyn codex_exec_server::ExecProcess>,
) -> Result<(i32, String, String), StructuredToolError> {
    let mut events = process.subscribe_events();
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let mut exit_code = None;
    loop {
        match events
            .recv()
            .await
            .map_err(|error| StructuredToolError::Process(error.to_string()))?
        {
            ExecProcessEvent::Output(chunk) => {
                let destination = match chunk.stream {
                    ExecOutputStream::Stdout | ExecOutputStream::Pty => &mut stdout,
                    ExecOutputStream::Stderr => &mut stderr,
                };
                destination.extend(chunk.chunk.into_inner());
                if stdout.len() + stderr.len() > MAX_OUTPUT_BYTES {
                    let _ = process.terminate().await;
                    return Err(StructuredToolError::OutputLimit);
                }
            }
            ExecProcessEvent::Exited {
                exit_code: code, ..
            } => exit_code = Some(code),
            ExecProcessEvent::Closed { .. } => {
                return Ok((
                    exit_code.unwrap_or(-1),
                    String::from_utf8_lossy(&stdout).into_owned(),
                    String::from_utf8_lossy(&stderr).into_owned(),
                ));
            }
            ExecProcessEvent::Failed(message) => {
                return Err(StructuredToolError::Process(message));
            }
        }
    }
}

fn parse<T: for<'de> Deserialize<'de>>(arguments: Value) -> Result<T, StructuredToolError> {
    serde_json::from_value(arguments)
        .map_err(|error| StructuredToolError::InvalidArguments(error.to_string()))
}

fn function_spec(name: &str, description: &str, input_schema: Value) -> DynamicToolSpec {
    DynamicToolSpec::Function(DynamicToolFunctionSpec {
        name: name.to_string(),
        description: description.to_string(),
        input_schema,
        defer_loading: false,
    })
}

fn target_schema() -> Value {
    json!({"type":"object","additionalProperties":false,"required":["targets"],"properties":{"targets":{"type":"array","minItems":1,"maxItems":128,"items":{"type":"string"}}}})
}

fn repeated_targets(command: &str, flags: &[&str], targets: &[String]) -> Vec<String> {
    let mut argv = Vec::with_capacity(1 + flags.len() + targets.len() * 2);
    argv.push(command.to_string());
    argv.extend(flags.iter().map(ToString::to_string));
    for target in targets {
        argv.extend(["-u".to_string(), target.clone()]);
    }
    argv
}

fn join_values<T: ToString>(values: &[T]) -> String {
    values
        .iter()
        .map(ToString::to_string)
        .collect::<Vec<_>>()
        .join(",")
}

#[cfg(test)]
#[path = "tools_tests.rs"]
mod tests;
