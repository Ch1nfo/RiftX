use serde::Deserialize;
use serde::Serialize;
use sha2::Digest;
use sha2::Sha256;
use std::path::PathBuf;

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SkillDirectoryConfig {
    pub directory: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SkillCatalog {
    pub root: PathBuf,
    pub skills: Vec<DiscoveredSkill>,
    pub snapshot_sha256: String,
    pub diagnostics: Vec<SkillDiagnostic>,
}

impl SkillCatalog {
    pub fn empty(root: PathBuf) -> Self {
        Self {
            root,
            skills: Vec::new(),
            snapshot_sha256: hex_digest(Sha256::digest([])),
            diagnostics: Vec::new(),
        }
    }

    pub fn is_healthy(&self) -> bool {
        !self
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.level == SkillDiagnosticLevel::Error)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DiscoveredSkill {
    pub name: String,
    pub description: String,
    pub path: PathBuf,
    pub source: SkillSource,
    pub enabled: bool,
    pub sha256: String,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum SkillSource {
    BuiltIn,
    User,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SkillDiagnostic {
    pub level: SkillDiagnosticLevel,
    pub code: String,
    pub path: Option<PathBuf>,
    pub message: String,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum SkillDiagnosticLevel {
    Info,
    Warning,
    Error,
}

pub(crate) fn hex_digest(digest: impl AsRef<[u8]>) -> String {
    digest
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
