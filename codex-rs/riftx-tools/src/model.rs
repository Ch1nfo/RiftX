use serde::Deserialize;
use serde::Serialize;
use sha2::Digest;
use sha2::Sha256;
use std::ffi::OsString;
use std::path::PathBuf;
use thiserror::Error;

/// Current supported version of the sidecar tool metadata contract.
pub const TOOL_METADATA_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ToolScanConfig {
    #[serde(default)]
    pub directories: Vec<PathBuf>,
    #[serde(default)]
    pub extra_paths: Vec<PathBuf>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ToolMetadata {
    #[serde(alias = "schema_version")]
    pub schema_version: u32,
    #[serde(default)]
    pub capabilities: Vec<String>,
    pub risk: Option<ToolRisk>,
    #[serde(default, alias = "help_args")]
    pub help_args: Vec<String>,
    #[serde(default, alias = "version_args")]
    pub version_args: Vec<String>,
    #[serde(default, alias = "health_check_args")]
    pub health_check_args: Vec<String>,
    #[serde(alias = "input_target_field")]
    pub input_target_field: Option<String>,
    #[serde(alias = "output_format")]
    pub output_format: Option<String>,
    pub parser: Option<String>,
    pub credential: Option<ToolCredentialMetadata>,
}

impl ToolMetadata {
    pub(crate) fn is_valid(&self) -> bool {
        self.schema_version == TOOL_METADATA_SCHEMA_VERSION
            && self
                .capabilities
                .iter()
                .all(|capability| valid_capability(capability))
            && bounded_arguments(&self.help_args)
            && bounded_arguments(&self.version_args)
            && bounded_arguments(&self.health_check_args)
            && [&self.input_target_field, &self.output_format, &self.parser]
                .into_iter()
                .flatten()
                .all(|value| valid_text(value, 128))
            && self.credential.as_ref().is_none_or(|credential| {
                valid_capability(&credential.capability)
                    && self.capabilities.contains(&credential.capability)
                    && credential.is_valid()
            })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ToolCredentialMetadata {
    pub capability: String,
    pub injection: ToolCredentialInjection,
    #[serde(alias = "environment_variable")]
    pub environment_variable: Option<String>,
    #[serde(default)]
    pub arguments: Vec<String>,
    #[serde(default, alias = "authentication_failure_exit_codes")]
    pub authentication_failure_exit_codes: Vec<i32>,
}

impl ToolCredentialMetadata {
    fn is_valid(&self) -> bool {
        let injection_is_valid = match self.injection {
            ToolCredentialInjection::Stdin => self.environment_variable.is_none(),
            ToolCredentialInjection::Environment | ToolCredentialInjection::FileEnvironment => self
                .environment_variable
                .as_deref()
                .is_some_and(valid_credential_variable),
        };
        injection_is_valid
            && !self.arguments.is_empty()
            && bounded_arguments(&self.arguments)
            && self
                .arguments
                .iter()
                .map(|argument| argument.matches("{target}").count())
                .sum::<usize>()
                == 1
            && self.arguments.iter().all(|argument| {
                let without_target = argument.replace("{target}", "");
                !without_target.replace("{port}", "").contains(['{', '}'])
            })
            && self.authentication_failure_exit_codes.len() <= 16
            && self
                .authentication_failure_exit_codes
                .iter()
                .all(|code| *code != 0)
    }

    pub fn render_arguments(&self, host: &str, port: Option<u16>) -> Option<Vec<String>> {
        (self.is_valid()
            && (port.is_some()
                || !self
                    .arguments
                    .iter()
                    .any(|argument| argument.contains("{port}"))))
        .then(|| {
            self.arguments
                .iter()
                .map(|argument| {
                    argument.replace("{target}", host).replace(
                        "{port}",
                        &port.map_or_else(String::new, |port| port.to_string()),
                    )
                })
                .collect()
        })
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ToolCredentialInjection {
    Stdin,
    Environment,
    FileEnvironment,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ToolRisk {
    Low,
    Medium,
    High,
    Critical,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DiscoveredTool {
    pub name: String,
    pub path: PathBuf,
    pub sha256: String,
    pub metadata_path: Option<PathBuf>,
    pub metadata_sha256: Option<String>,
    pub metadata: Option<ToolMetadata>,
    pub shadowed_by: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ToolInventory {
    pub roots: Vec<PathBuf>,
    pub path_entries: Vec<PathBuf>,
    pub tools: Vec<DiscoveredTool>,
    pub snapshot_sha256: String,
    pub diagnostics: Vec<ToolDiagnostic>,
}

impl ToolInventory {
    pub fn empty() -> Self {
        Self {
            roots: Vec::new(),
            path_entries: Vec::new(),
            tools: Vec::new(),
            snapshot_sha256: hex_digest(Sha256::digest([])),
            diagnostics: Vec::new(),
        }
    }

    pub fn is_healthy(&self) -> bool {
        !self
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.level == DiagnosticLevel::Error)
    }

    pub fn process_path(&self, system_path: Option<OsString>) -> Result<OsString, ToolPathError> {
        let mut entries = self.path_entries.clone();
        if let Some(system_path) = system_path {
            entries.extend(
                std::env::split_paths(&system_path).filter(|path| !path.as_os_str().is_empty()),
            );
        }
        std::env::join_paths(entries).map_err(ToolPathError::Join)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ToolDiagnostic {
    pub level: DiagnosticLevel,
    pub code: String,
    pub path: Option<PathBuf>,
    pub message: String,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum DiagnosticLevel {
    Info,
    Warning,
    Error,
}

#[derive(Debug, Error)]
pub enum ToolPathError {
    #[error("failed to construct process PATH: {0}")]
    Join(#[source] std::env::JoinPathsError),
}

fn bounded_arguments(arguments: &[String]) -> bool {
    arguments.len() <= 64
        && arguments
            .iter()
            .all(|argument| valid_text(argument, 4 * 1024))
}

fn valid_capability(value: &str) -> bool {
    valid_text(value, 128)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_'))
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

fn valid_text(value: &str, max_bytes: usize) -> bool {
    !value.trim().is_empty() && value.len() <= max_bytes && !value.chars().any(char::is_control)
}

pub(crate) fn hex_digest(digest: impl AsRef<[u8]>) -> String {
    digest
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
