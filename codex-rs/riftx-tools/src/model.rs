use serde::Deserialize;
use serde::Serialize;
use sha2::Digest;
use sha2::Sha256;
use std::ffi::OsString;
use std::path::PathBuf;
use thiserror::Error;

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
    #[serde(default)]
    pub capabilities: Vec<String>,
    pub risk: Option<ToolRisk>,
    #[serde(default)]
    pub help_args: Vec<String>,
    #[serde(default)]
    pub version_args: Vec<String>,
    #[serde(default)]
    pub health_check_args: Vec<String>,
    pub input_target_field: Option<String>,
    pub output_format: Option<String>,
    pub parser: Option<String>,
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

pub(crate) fn hex_digest(digest: impl AsRef<[u8]>) -> String {
    digest
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
