use serde::Deserialize;
use serde::Serialize;
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
    #[error("invalid RiftX config: {0}")]
    Invalid(String),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RiftxConfig {
    pub daemon: DaemonConfig,
    pub llm: LlmConfig,
    pub policy: ManagedPolicyConfig,
    pub audit: AuditConfig,
    pub artifacts: ArtifactConfig,
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
        let config: Self = toml::from_str(&content)?;
        config.validate()?;
        Ok(config)
    }

    pub fn validate(&self) -> Result<(), ConfigError> {
        self.llm.validate()?;
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DaemonConfig {
    pub ipc_dir: PathBuf,
    pub state_db: PathBuf,
    pub runtime_home: PathBuf,
    pub workspace_root: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LlmConfig {
    pub model: String,
    pub base_url: String,
    pub api_key_env: String,
}

impl LlmConfig {
    pub fn validate(&self) -> Result<(), ConfigError> {
        if self.model.trim().is_empty()
            || self.model.len() > 256
            || self.model.chars().any(char::is_control)
        {
            return Err(ConfigError::Invalid(
                "llm.model must be non-empty, contain no control characters, and be at most 256 bytes"
                    .to_string(),
            ));
        }
        if !valid_env_name(&self.api_key_env) {
            return Err(ConfigError::Invalid(
                "llm.api_key_env must be a valid environment variable name".to_string(),
            ));
        }
        let base_url = url::Url::parse(&self.base_url)
            .map_err(|error| ConfigError::Invalid(format!("llm.base_url is invalid: {error}")))?;
        let loopback = match base_url.host() {
            Some(url::Host::Domain(host)) => host.eq_ignore_ascii_case("localhost"),
            Some(url::Host::Ipv4(address)) => address.is_loopback(),
            Some(url::Host::Ipv6(address)) => address.is_loopback(),
            None => {
                return Err(ConfigError::Invalid(
                    "llm.base_url must include a host".to_string(),
                ));
            }
        };
        if base_url.scheme() != "https" && !(base_url.scheme() == "http" && loopback) {
            return Err(ConfigError::Invalid(
                "llm.base_url must use HTTPS; HTTP is allowed only for loopback development endpoints"
                    .to_string(),
            ));
        }
        if !base_url.username().is_empty()
            || base_url.password().is_some()
            || base_url.query().is_some()
            || base_url.fragment().is_some()
        {
            return Err(ConfigError::Invalid(
                "llm.base_url cannot contain credentials, query parameters, or fragments"
                    .to_string(),
            ));
        }
        Ok(())
    }
}

fn valid_env_name(value: &str) -> bool {
    let mut characters = value.chars();
    matches!(characters.next(), Some(first) if first == '_' || first.is_ascii_alphabetic())
        && characters.all(|character| character == '_' || character.is_ascii_alphanumeric())
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ManagedPolicyConfig {
    #[serde(default)]
    pub allowed_capabilities: Vec<String>,
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
