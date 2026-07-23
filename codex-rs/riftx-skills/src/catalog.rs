use crate::DiscoveredSkill;
use crate::SkillCatalog;
use crate::SkillDiagnostic;
use crate::SkillDiagnosticLevel;
use crate::SkillSource;
use crate::hex_digest;
use codex_app_server_protocol::SkillsListEntry;
use sha2::Digest;
use sha2::Sha256;
use std::collections::VecDeque;
use std::path::Path;
use std::path::PathBuf;
use tokio::io::AsyncReadExt;

const MAX_SKILL_FILES: usize = 256;
const MAX_SKILL_BYTES: u64 = 16 * 1024 * 1024;
const MAX_FILE_BYTES: u64 = 1024 * 1024;
const MAX_DIRECTORY_DEPTH: usize = 6;

pub struct SkillCatalogBuilder {
    root: PathBuf,
}

impl SkillCatalogBuilder {
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    pub async fn build(&self, entry: SkillsListEntry) -> SkillCatalog {
        let mut diagnostics = entry
            .errors
            .into_iter()
            .map(|error| {
                diagnostic(
                    SkillDiagnosticLevel::Error,
                    "skillInvalid",
                    Some(error.path),
                    error.message,
                )
            })
            .collect::<Vec<_>>();
        let mut skills = Vec::new();
        for metadata in entry.skills {
            if !metadata.path.as_path().starts_with(&self.root) {
                diagnostics.push(diagnostic(
                    SkillDiagnosticLevel::Error,
                    "skillOutsideDirectory",
                    Some(metadata.path.to_path_buf()),
                    "the runtime returned a skill outside the exclusive Skills Directory",
                ));
                continue;
            }
            let Some(directory) = metadata.path.as_path().parent() else {
                continue;
            };
            let Some(sha256) = hash_skill_directory(directory, &mut diagnostics).await else {
                continue;
            };
            skills.push(DiscoveredSkill {
                name: metadata.name,
                description: metadata.description,
                path: metadata.path.to_path_buf(),
                source: SkillSource::User,
                enabled: metadata.enabled,
                sha256,
            });
        }
        skills.sort_by(|left, right| {
            left.name
                .cmp(&right.name)
                .then_with(|| left.path.cmp(&right.path))
        });
        let snapshot_sha256 = catalog_digest(&skills);
        SkillCatalog {
            root: self.root.clone(),
            skills,
            snapshot_sha256,
            diagnostics,
        }
    }
}

pub fn default_skills_root() -> Option<PathBuf> {
    #[cfg(target_os = "macos")]
    {
        return dirs::data_dir().map(|path| path.join("RiftX").join("skills"));
    }
    #[cfg(target_os = "windows")]
    {
        return dirs::data_local_dir().map(|path| path.join("RiftX").join("skills"));
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        return dirs::data_dir().map(|path| path.join("riftx").join("skills"));
    }
    #[allow(unreachable_code)]
    None
}

async fn hash_skill_directory(
    root: &Path,
    diagnostics: &mut Vec<SkillDiagnostic>,
) -> Option<String> {
    let mut queue = VecDeque::from([(root.to_path_buf(), 0usize)]);
    let mut files = Vec::new();
    while let Some((directory, depth)) = queue.pop_front() {
        if depth > MAX_DIRECTORY_DEPTH {
            diagnostics.push(diagnostic(
                SkillDiagnosticLevel::Error,
                "skillTooDeep",
                Some(directory),
                "skill directory exceeds the maximum scan depth",
            ));
            return None;
        }
        let mut entries = match read_directory(&directory).await {
            Ok(entries) => entries,
            Err(error) => {
                diagnostics.push(diagnostic(
                    SkillDiagnosticLevel::Error,
                    "skillUnreadable",
                    Some(directory),
                    error.to_string(),
                ));
                return None;
            }
        };
        entries.sort();
        for path in entries {
            let metadata = match tokio::fs::symlink_metadata(&path).await {
                Ok(metadata) => metadata,
                Err(error) => {
                    diagnostics.push(diagnostic(
                        SkillDiagnosticLevel::Error,
                        "skillUnreadable",
                        Some(path),
                        error.to_string(),
                    ));
                    return None;
                }
            };
            if metadata.file_type().is_symlink() {
                diagnostics.push(diagnostic(
                    SkillDiagnosticLevel::Error,
                    "skillSymlinkRejected",
                    Some(path),
                    "symbolic links are not allowed in RiftX skills",
                ));
                return None;
            }
            if metadata.is_dir() {
                queue.push_back((path, depth + 1));
            } else if metadata.is_file() {
                files.push((path, metadata.len()));
            }
        }
    }
    files.sort_by(|left, right| left.0.cmp(&right.0));
    if files.len() > MAX_SKILL_FILES {
        diagnostics.push(diagnostic(
            SkillDiagnosticLevel::Error,
            "skillTooManyFiles",
            Some(root.to_path_buf()),
            "skill directory exceeds the file count limit",
        ));
        return None;
    }
    let mut total_bytes = 0u64;
    let mut hasher = Sha256::new();
    for (path, size) in files {
        if size > MAX_FILE_BYTES || total_bytes.saturating_add(size) > MAX_SKILL_BYTES {
            diagnostics.push(diagnostic(
                SkillDiagnosticLevel::Error,
                "skillTooLarge",
                Some(path),
                "skill directory exceeds the content size limit",
            ));
            return None;
        }
        total_bytes += size;
        let relative = path.strip_prefix(root).unwrap_or(&path);
        hasher.update(portable_path(relative).as_bytes());
        hasher.update([0]);
        let mut file = match tokio::fs::File::open(&path).await {
            Ok(file) => file,
            Err(error) => {
                diagnostics.push(diagnostic(
                    SkillDiagnosticLevel::Error,
                    "skillUnreadable",
                    Some(path),
                    error.to_string(),
                ));
                return None;
            }
        };
        let mut remaining = size;
        let mut buffer = [0u8; 8192];
        while remaining > 0 {
            let read = match file.read(&mut buffer).await {
                Ok(read) => read,
                Err(error) => {
                    diagnostics.push(diagnostic(
                        SkillDiagnosticLevel::Error,
                        "skillUnreadable",
                        Some(path.clone()),
                        error.to_string(),
                    ));
                    return None;
                }
            };
            if read == 0 {
                break;
            }
            hasher.update(&buffer[..read]);
            remaining = remaining.saturating_sub(read as u64);
        }
        hasher.update([0]);
    }
    Some(hex_digest(hasher.finalize()))
}

async fn read_directory(path: &Path) -> std::io::Result<Vec<PathBuf>> {
    let mut directory = tokio::fs::read_dir(path).await?;
    let mut entries = Vec::new();
    while let Some(entry) = directory.next_entry().await? {
        entries.push(entry.path());
    }
    Ok(entries)
}

fn catalog_digest(skills: &[DiscoveredSkill]) -> String {
    let mut hasher = Sha256::new();
    for skill in skills {
        hasher.update(skill.name.as_bytes());
        hasher.update([0]);
        hasher.update(skill.sha256.as_bytes());
        hasher.update([0]);
    }
    hex_digest(hasher.finalize())
}

fn portable_path(path: &Path) -> String {
    path.components()
        .map(|component| component.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join("/")
}

fn diagnostic(
    level: SkillDiagnosticLevel,
    code: &str,
    path: Option<PathBuf>,
    message: impl Into<String>,
) -> SkillDiagnostic {
    SkillDiagnostic {
        level,
        code: code.to_string(),
        path,
        message: message.into(),
    }
}
