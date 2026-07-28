use codex_riftx_skills::SkillDirectoryConfig;
use codex_riftx_tools::ToolScanConfig;
use serde::Deserialize;
use serde::Serialize;
use std::collections::BTreeMap;
use std::io::ErrorKind;
use std::path::Path;
use std::path::PathBuf;
use thiserror::Error;
use tokio::io::AsyncWriteExt;

const MAX_LLM_PROFILES: usize = 16;
const PRE_V1_BACKUP_SUFFIX: &str = ".pre-1.0.bak";

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("failed to read RiftX config {path}: {source}")]
    Read {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("failed to write RiftX config {path}: {source}")]
    Write {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("failed to back up RiftX config to {path}: {source}")]
    Backup {
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
    #[serde(default)]
    pub skills: SkillDirectoryConfig,
    pub tools: ToolScanConfig,
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

    pub async fn load_resolved(path: &Path) -> Result<Self, ConfigError> {
        let path = std::path::absolute(path)
            .map_err(|error| ConfigError::Invalid(format!("resolve config path: {error}")))?;
        let mut config = Self::load_migrating(&path).await?;
        let base = path.parent().ok_or_else(|| {
            ConfigError::Invalid(format!("config path has no parent: {}", path.display()))
        })?;
        config.resolve_paths(base);
        Ok(config)
    }

    /// Load config and persist a one-shot protocol migration when needed.
    pub async fn load_migrating(path: &Path) -> Result<Self, ConfigError> {
        let mut config = Self::load(path).await?;
        if config.llm.apply_migrations() {
            backup_config_once(path).await?;
            config.write_atomic(path).await?;
        }
        Ok(config)
    }

    /// Serialize and replace `path` via temp file + fsync + rename.
    pub async fn write_atomic(&self, path: &Path) -> Result<(), ConfigError> {
        self.validate()?;
        let content = toml::to_string(self)
            .map_err(|error| ConfigError::Invalid(format!("serialize config: {error}")))?;
        let mut tmp = path.as_os_str().to_owned();
        tmp.push(".tmp");
        let tmp = PathBuf::from(tmp);
        tokio::fs::write(&tmp, &content)
            .await
            .map_err(|source| ConfigError::Write {
                path: tmp.clone(),
                source,
            })?;
        {
            let file = tokio::fs::File::open(&tmp)
                .await
                .map_err(|source| ConfigError::Write {
                    path: tmp.clone(),
                    source,
                })?;
            file.sync_all().await.map_err(|source| ConfigError::Write {
                path: tmp.clone(),
                source,
            })?;
        }
        tokio::fs::rename(&tmp, path)
            .await
            .map_err(|source| ConfigError::Write {
                path: path.to_path_buf(),
                source,
            })?;
        Ok(())
    }

    pub fn validate(&self) -> Result<(), ConfigError> {
        self.llm.validate()?;
        self.artifacts.validate()?;
        Ok(())
    }

    fn resolve_paths(&mut self, base: &Path) {
        resolve_path(base, &mut self.daemon.ipc_dir);
        resolve_path(base, &mut self.daemon.state_db);
        resolve_path(base, &mut self.daemon.runtime_home);
        resolve_path(base, &mut self.daemon.workspace_root);
        resolve_path(base, &mut self.audit.jsonl_path);
        resolve_path(base, &mut self.artifacts.root);
        if let Some(directory) = self.skills.directory.as_mut() {
            resolve_path(base, directory);
        }
        for directory in &mut self.tools.directories {
            resolve_path(base, directory);
        }
        for path in &mut self.tools.extra_paths {
            resolve_path(base, path);
        }
    }
}

async fn backup_config_once(path: &Path) -> Result<(), ConfigError> {
    let mut backup_name = path.as_os_str().to_owned();
    backup_name.push(PRE_V1_BACKUP_SUFFIX);
    let backup_path = PathBuf::from(backup_name);
    let content = tokio::fs::read(path)
        .await
        .map_err(|source| ConfigError::Backup {
            path: backup_path.clone(),
            source,
        })?;
    let permissions = tokio::fs::metadata(path)
        .await
        .map_err(|source| ConfigError::Backup {
            path: backup_path.clone(),
            source,
        })?
        .permissions();
    let mut backup = match tokio::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&backup_path)
        .await
    {
        Ok(backup) => backup,
        Err(source) if source.kind() == ErrorKind::AlreadyExists => {
            let metadata =
                tokio::fs::metadata(&backup_path)
                    .await
                    .map_err(|source| ConfigError::Backup {
                        path: backup_path.clone(),
                        source,
                    })?;
            if metadata.is_file() {
                validate_config_backup(&backup_path)
                    .await
                    .map_err(|source| ConfigError::Backup {
                        path: backup_path.clone(),
                        source,
                    })?;
                return Ok(());
            }
            return Err(ConfigError::Backup {
                path: backup_path,
                source: std::io::Error::new(
                    ErrorKind::AlreadyExists,
                    "backup path is not a regular file",
                ),
            });
        }
        Err(source) => {
            return Err(ConfigError::Backup {
                path: backup_path,
                source,
            });
        }
    };
    let result = async {
        tokio::fs::set_permissions(&backup_path, permissions).await?;
        backup.write_all(&content).await?;
        backup.sync_all().await?;
        drop(backup);
        validate_config_backup(&backup_path).await
    }
    .await;
    if let Err(source) = result {
        let _ = tokio::fs::remove_file(&backup_path).await;
        return Err(ConfigError::Backup {
            path: backup_path,
            source,
        });
    }
    Ok(())
}

async fn validate_config_backup(path: &Path) -> Result<(), std::io::Error> {
    let content = tokio::fs::read_to_string(path).await?;
    let config = toml::from_str::<RiftxConfig>(&content)
        .map_err(|error| std::io::Error::new(ErrorKind::InvalidData, error))?;
    config
        .validate()
        .map_err(|error| std::io::Error::new(ErrorKind::InvalidData, error))
}

fn resolve_path(base: &Path, path: &mut PathBuf) {
    if path.is_relative() {
        *path = base.join(&*path);
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
    /// Bumped by repeatable migrations. Missing/`0` means pre-protocol configs.
    #[serde(default)]
    pub config_version: u32,
    pub default_profile: String,
    pub profiles: BTreeMap<String, LlmProfileConfig>,
}

/// Wire protocol for an LLM profile. Unknown values must fail to deserialize.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum LlmProtocol {
    #[default]
    Responses,
    ChatCompletions,
}

impl LlmProtocol {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Responses => "responses",
            Self::ChatCompletions => "chat_completions",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LlmProfileConfig {
    /// Defaults to enabled so existing configurations preserve their behavior.
    #[serde(default = "enabled_by_default")]
    pub enabled: bool,
    /// Defaults to `responses` when omitted so pre-1.0 configs still load.
    #[serde(default)]
    pub protocol: LlmProtocol,
    pub model: String,
    pub base_url: String,
    pub api_key: LlmApiKeySource,
    pub timeout_seconds: u64,
    pub reasoning_level: LlmReasoningLevel,
    pub context_budget: u32,
}

/// Current LLM config schema version. `2` introduces explicit Profile enablement.
pub const LLM_CONFIG_VERSION: u32 = 2;

fn enabled_by_default() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "source", rename_all = "snake_case", deny_unknown_fields)]
pub enum LlmApiKeySource {
    Keyring { credential: String },
    Environment { variable: String },
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LlmReasoningLevel {
    Minimal,
    Low,
    Medium,
    High,
    XHigh,
}

impl LlmReasoningLevel {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Minimal => "minimal",
            Self::Low => "low",
            Self::Medium => "medium",
            Self::High => "high",
            Self::XHigh => "xhigh",
        }
    }
}

impl LlmConfig {
    /// Upgrade older LLM configs to the current repeatable schema version.
    ///
    /// Missing `protocol` and `enabled` fields deserialize to safe compatibility
    /// defaults. Returns `true` when the explicit fields and version should be
    /// persisted. Safe to call repeatedly.
    pub fn apply_migrations(&mut self) -> bool {
        if self.config_version >= LLM_CONFIG_VERSION {
            return false;
        }
        self.config_version = LLM_CONFIG_VERSION;
        true
    }

    pub fn validate(&self) -> Result<(), ConfigError> {
        if !valid_profile_name(&self.default_profile) {
            return Err(ConfigError::Invalid(
                "llm.default_profile must use 1-128 ASCII letters, digits, dots, hyphens, or underscores"
                    .to_string(),
            ));
        }
        if self.profiles.is_empty() || self.profiles.len() > MAX_LLM_PROFILES {
            return Err(ConfigError::Invalid(
                "llm.profiles must define between 1 and 16 profiles".to_string(),
            ));
        }
        if !self.profiles.contains_key(&self.default_profile) {
            return Err(ConfigError::Invalid(format!(
                "llm.default_profile {:?} does not exist in llm.profiles",
                self.default_profile
            )));
        }
        if self
            .profiles
            .get(&self.default_profile)
            .is_some_and(|profile| !profile.enabled)
        {
            return Err(ConfigError::Invalid(format!(
                "llm.default_profile {:?} cannot be disabled",
                self.default_profile
            )));
        }
        for (name, profile) in &self.profiles {
            if !valid_profile_name(name) {
                return Err(ConfigError::Invalid(format!(
                    "LLM profile name {name:?} must use 1-128 ASCII letters, digits, dots, hyphens, or underscores"
                )));
            }
            profile.validate(name)?;
        }
        Ok(())
    }

    pub fn default_profile(&self) -> Option<&LlmProfileConfig> {
        self.profiles.get(&self.default_profile)
    }
}

impl LlmProfileConfig {
    pub fn validate(&self, profile_name: &str) -> Result<(), ConfigError> {
        let prefix = format!("llm.profiles.{profile_name}");
        if self.model.trim().is_empty()
            || self.model.len() > 256
            || self.model.chars().any(char::is_control)
        {
            return Err(ConfigError::Invalid(format!(
                "{prefix}.model must be non-empty, contain no control characters, and be at most 256 bytes"
            )));
        }
        match &self.api_key {
            LlmApiKeySource::Keyring { credential } if !valid_profile_name(credential) => {
                return Err(ConfigError::Invalid(format!(
                    "{prefix}.api_key keyring credential must use 1-128 ASCII letters, digits, dots, hyphens, or underscores"
                )));
            }
            LlmApiKeySource::Environment { variable } if !valid_env_name(variable) => {
                return Err(ConfigError::Invalid(format!(
                    "{prefix}.api_key environment variable must be a valid name"
                )));
            }
            LlmApiKeySource::Keyring { .. } | LlmApiKeySource::Environment { .. } => {}
        }
        let base_url = url::Url::parse(&self.base_url).map_err(|error| {
            ConfigError::Invalid(format!("{prefix}.base_url is invalid: {error}"))
        })?;
        let loopback = match base_url.host() {
            Some(url::Host::Domain(host)) => host.eq_ignore_ascii_case("localhost"),
            Some(url::Host::Ipv4(address)) => address.is_loopback(),
            Some(url::Host::Ipv6(address)) => address.is_loopback(),
            None => {
                return Err(ConfigError::Invalid(format!(
                    "{prefix}.base_url must include a host"
                )));
            }
        };
        if base_url.scheme() != "https" && !(base_url.scheme() == "http" && loopback) {
            return Err(ConfigError::Invalid(format!(
                "{prefix}.base_url must use HTTPS; HTTP is allowed only for loopback development endpoints"
            )));
        }
        if !base_url.username().is_empty()
            || base_url.password().is_some()
            || base_url.query().is_some()
            || base_url.fragment().is_some()
        {
            return Err(ConfigError::Invalid(format!(
                "{prefix}.base_url cannot contain credentials, query parameters, or fragments"
            )));
        }
        if !(1..=3_600).contains(&self.timeout_seconds) {
            return Err(ConfigError::Invalid(format!(
                "{prefix}.timeout_seconds must be between 1 and 3600"
            )));
        }
        if !(1_024..=2_000_000).contains(&self.context_budget) {
            return Err(ConfigError::Invalid(format!(
                "{prefix}.context_budget must be between 1024 and 2000000"
            )));
        }
        Ok(())
    }
}

fn valid_profile_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_'))
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

impl ArtifactConfig {
    fn validate(&self) -> Result<(), ConfigError> {
        if self.root.as_os_str().is_empty() {
            return Err(ConfigError::Invalid(
                "artifacts.root must not be empty".to_string(),
            ));
        }
        if self.max_bytes_per_engagement == 0 {
            return Err(ConfigError::Invalid(
                "artifacts.max_bytes_per_engagement must be greater than zero".to_string(),
            ));
        }
        Ok(())
    }
}
