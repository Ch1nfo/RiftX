use crate::Scope;
use serde::Deserialize;
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::Path;
use std::path::PathBuf;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("failed to read RiftX config {path}: {source}")]
    Read {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("invalid RiftX config: {0}")]
    Parse(#[from] toml::de::Error),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RiftxConfig {
    pub gateway: GatewayConfig,
    pub manager: ManagerConfig,
    pub sandbox: SandboxConfig,
    pub policy: ManagedPolicyConfig,
    pub audit: AuditConfig,
    pub artifacts: ArtifactConfig,
    pub tool_profiles: BTreeMap<String, ToolProfileConfig>,
}

impl RiftxConfig {
    pub async fn load(path: &Path) -> Result<Self, ConfigError> {
        let content =
            tokio::fs::read_to_string(path)
                .await
                .map_err(|source| ConfigError::Read {
                    path: path.to_path_buf(),
                    source,
                })?;
        Ok(toml::from_str(&content)?)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct GatewayConfig {
    pub listen: String,
    pub operator_token_env: String,
    pub state_db: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ManagerConfig {
    pub socket: PathBuf,
    pub request_timeout_ms: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SandboxConfig {
    pub image: String,
    pub cpu_limit: u16,
    pub memory_mib: u32,
    pub pids_limit: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ManagedPolicyConfig {
    #[serde(default)]
    pub allowed_tools: Vec<String>,
    #[serde(default)]
    pub denied_cidrs: Vec<ipnet::IpNet>,
    #[serde(default)]
    pub denied_domains: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AuditConfig {
    pub jsonl_path: PathBuf,
    pub fsync: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactConfig {
    pub root: PathBuf,
    pub max_bytes_per_engagement: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ToolProfileConfig {
    pub allowed_tools: Vec<String>,
    pub scope: Scope,
    pub approval: ApprovalMode,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalMode {
    Always,
    HighRisk,
    Never,
}
