//! Content-addressed artifact capture for local RiftX engagement workspaces.

use codex_riftx_core::Artifact;
use codex_riftx_core::ArtifactConfig;
use codex_riftx_crypto::CryptoError;
use codex_riftx_crypto::EngagementRecordCipher;
use sha2::Digest;
use sha2::Sha256;
use std::collections::BTreeSet;
use std::path::Component;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use thiserror::Error;
use tokio::io::AsyncReadExt;
use uuid::Uuid;
use walkdir::WalkDir;

mod encrypted_blob;

pub use encrypted_blob::DecryptedArtifact;

const MAX_DISCOVERED_ARTIFACTS: usize = 256;
const MAX_DISCOVERY_DEPTH: usize = 8;

#[derive(Debug, Error)]
pub enum ArtifactError {
    #[error("engagement id is not a safe path component")]
    InvalidEngagementId,
    #[error("artifact path must be a non-empty relative path without traversal")]
    InvalidRelativePath,
    #[error("artifact digest is invalid")]
    InvalidDigest,
    #[error("stored artifact digest does not match its manifest")]
    DigestMismatch,
    #[error("artifact path contains a symbolic link: {0}")]
    SymbolicLink(PathBuf),
    #[error("artifact source is not a regular file: {0}")]
    SourceNotRegular(PathBuf),
    #[error("artifact media type is invalid")]
    InvalidMediaType,
    #[error("artifact capacity of {limit} bytes would be exceeded")]
    CapacityExceeded { limit: u64 },
    #[error("artifact source changed while it was captured: {0}")]
    SourceChanged(PathBuf),
    #[error("too many files were found in the workspace artifacts directory")]
    TooManyArtifacts,
    #[error("artifact I/O failed for {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("artifact discovery failed: {0}")]
    Discovery(#[from] walkdir::Error),
    #[error(transparent)]
    Crypto(#[from] CryptoError),
    #[error("artifact encryption task failed: {0}")]
    CryptoTask(String),
    #[error("stored artifact uses an invalid encrypted format")]
    InvalidFormat,
}

impl ArtifactError {
    pub fn is_request_error(&self) -> bool {
        matches!(
            self,
            Self::InvalidEngagementId
                | Self::InvalidRelativePath
                | Self::InvalidDigest
                | Self::SymbolicLink(_)
                | Self::SourceNotRegular(_)
                | Self::InvalidMediaType
                | Self::CapacityExceeded { .. }
                | Self::SourceChanged(_)
                | Self::TooManyArtifacts
        ) || matches!(
            self,
            Self::Io { source, .. } if source.kind() == std::io::ErrorKind::NotFound
        )
    }
}

pub struct CaptureArtifact<'a> {
    pub engagement_id: &'a str,
    pub workspace: &'a Path,
    pub relative_path: &'a Path,
    pub media_type: Option<&'a str>,
    pub execution_id: Option<&'a str>,
    pub existing: &'a [Artifact],
    pub created_at: i64,
}

#[derive(Clone)]
pub struct ArtifactStore {
    root: PathBuf,
    max_bytes_per_engagement: u64,
    cipher: Arc<dyn EngagementRecordCipher>,
}

impl ArtifactStore {
    pub fn new(config: &ArtifactConfig, cipher: Arc<dyn EngagementRecordCipher>) -> Self {
        Self {
            root: config.root.clone(),
            max_bytes_per_engagement: config.max_bytes_per_engagement,
            cipher,
        }
    }

    pub async fn capture(&self, request: CaptureArtifact<'_>) -> Result<Artifact, ArtifactError> {
        validate_component(request.engagement_id)?;
        validate_relative_path(request.relative_path)?;
        validate_media_type(request.media_type)?;

        let source = checked_source(request.workspace, request.relative_path).await?;
        let source_metadata = metadata(&source).await?;
        if !source_metadata.is_file() {
            return Err(ArtifactError::SourceNotRegular(source));
        }
        if source_metadata.len() > self.max_bytes_per_engagement {
            return Err(ArtifactError::CapacityExceeded {
                limit: self.max_bytes_per_engagement,
            });
        }

        let engagement_root = self.root.join(request.engagement_id);
        create_dir_all(&engagement_root).await?;
        let (sha256, size_bytes) = hash_file(&source, self.max_bytes_per_engagement).await?;
        let temporary = engagement_root.join(format!(".capture-{}", Uuid::new_v4()));
        let encrypted = encrypted_blob::encrypt_source(
            self.cipher.clone(),
            request.engagement_id,
            &sha256,
            &source,
            &temporary,
            self.max_bytes_per_engagement,
        )
        .await;
        let (encrypted_sha256, encrypted_size) = match encrypted {
            Ok(captured) => captured,
            Err(error) => {
                remove_if_exists(&temporary).await;
                return Err(error);
            }
        };
        let final_metadata = metadata(&source).await?;
        if final_metadata.len() != source_metadata.len()
            || encrypted_size != size_bytes
            || encrypted_sha256 != sha256
        {
            remove_if_exists(&temporary).await;
            return Err(ArtifactError::SourceChanged(source));
        }

        let used_bytes = unique_stored_bytes(request.existing);
        let already_stored = request
            .existing
            .iter()
            .any(|artifact| artifact.sha256 == sha256);
        if !already_stored
            && used_bytes
                .checked_add(size_bytes)
                .is_none_or(|total| total > self.max_bytes_per_engagement)
        {
            remove_if_exists(&temporary).await;
            return Err(ArtifactError::CapacityExceeded {
                limit: self.max_bytes_per_engagement,
            });
        }

        let destination = engagement_root.join(&sha256);
        commit_blob(&temporary, &destination).await?;
        let media_type = request.media_type.map(str::to_string).unwrap_or_else(|| {
            mime_guess::from_path(request.relative_path)
                .first_or_octet_stream()
                .essence_str()
                .to_string()
        });
        Ok(Artifact {
            id: Uuid::new_v4().to_string(),
            engagement_id: request.engagement_id.to_string(),
            execution_id: request.execution_id.map(str::to_string),
            path: request.relative_path.to_string_lossy().into_owned(),
            media_type,
            sha256,
            size_bytes,
            created_at: request.created_at,
        })
    }

    pub async fn open(&self, artifact: &Artifact) -> Result<DecryptedArtifact, ArtifactError> {
        validate_component(&artifact.engagement_id)?;
        if artifact.sha256.len() != 64
            || !artifact.sha256.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(ArtifactError::InvalidDigest);
        }
        let engagement_root = self.root.join(&artifact.engagement_id);
        let path = engagement_root.join(&artifact.sha256);
        let metadata = symlink_metadata(&path).await?;
        if metadata.file_type().is_symlink() {
            return Err(ArtifactError::SymbolicLink(path));
        }
        if !metadata.is_file() {
            return Err(ArtifactError::SourceNotRegular(path));
        }
        encrypted_blob::decrypt_to_temporary(
            self.cipher.clone(),
            &artifact.engagement_id,
            &artifact.sha256,
            &path,
            &engagement_root,
            artifact.size_bytes,
        )
        .await
    }

    pub fn discover(&self, workspace: &Path) -> Result<Vec<PathBuf>, ArtifactError> {
        let directory = workspace.join("artifacts");
        if !directory.exists() {
            return Ok(Vec::new());
        }
        let mut paths = Vec::new();
        for entry in WalkDir::new(&directory)
            .follow_links(false)
            .max_depth(MAX_DISCOVERY_DEPTH)
        {
            let entry = entry?;
            if entry.file_type().is_symlink() || !entry.file_type().is_file() {
                continue;
            }
            let relative = entry
                .path()
                .strip_prefix(workspace)
                .map_err(|_| ArtifactError::InvalidRelativePath)?;
            paths.push(relative.to_path_buf());
            if paths.len() > MAX_DISCOVERED_ARTIFACTS {
                return Err(ArtifactError::TooManyArtifacts);
            }
        }
        paths.sort();
        Ok(paths)
    }
}

fn validate_component(value: &str) -> Result<(), ArtifactError> {
    if value.is_empty()
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err(ArtifactError::InvalidEngagementId);
    }
    Ok(())
}

fn validate_relative_path(path: &Path) -> Result<(), ArtifactError> {
    if path.as_os_str().is_empty()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(ArtifactError::InvalidRelativePath);
    }
    Ok(())
}

fn validate_media_type(media_type: Option<&str>) -> Result<(), ArtifactError> {
    let Some(media_type) = media_type else {
        return Ok(());
    };
    let valid = media_type.len() <= 128
        && media_type.split_once('/').is_some_and(|(kind, subtype)| {
            !kind.is_empty()
                && !subtype.is_empty()
                && media_type.bytes().all(|byte| {
                    byte.is_ascii_alphanumeric() || matches!(byte, b'/' | b'-' | b'+' | b'.' | b'_')
                })
        });
    if valid {
        Ok(())
    } else {
        Err(ArtifactError::InvalidMediaType)
    }
}

async fn checked_source(workspace: &Path, relative_path: &Path) -> Result<PathBuf, ArtifactError> {
    let workspace =
        tokio::fs::canonicalize(workspace)
            .await
            .map_err(|source| ArtifactError::Io {
                path: workspace.to_path_buf(),
                source,
            })?;
    let mut source = workspace.clone();
    for component in relative_path.components() {
        let Component::Normal(component) = component else {
            return Err(ArtifactError::InvalidRelativePath);
        };
        source.push(component);
        let metadata = symlink_metadata(&source).await?;
        if metadata.file_type().is_symlink() {
            return Err(ArtifactError::SymbolicLink(source));
        }
    }
    if !source.starts_with(workspace) {
        return Err(ArtifactError::InvalidRelativePath);
    }
    Ok(source)
}

async fn hash_file(source: &Path, limit: u64) -> Result<(String, u64), ArtifactError> {
    let mut input = tokio::fs::File::open(source)
        .await
        .map_err(|source_error| ArtifactError::Io {
            path: source.to_path_buf(),
            source: source_error,
        })?;
    let mut hasher = Sha256::new();
    let mut size = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = input
            .read(&mut buffer)
            .await
            .map_err(|error| ArtifactError::Io {
                path: source.to_path_buf(),
                source: error,
            })?;
        if count == 0 {
            return Ok((hex_digest(hasher.finalize()), size));
        }
        size = size.saturating_add(u64::try_from(count).unwrap_or(u64::MAX));
        if size > limit {
            return Err(ArtifactError::CapacityExceeded { limit });
        }
        hasher.update(&buffer[..count]);
    }
}

async fn commit_blob(temporary: &Path, destination: &Path) -> Result<(), ArtifactError> {
    match symlink_metadata(destination).await {
        Ok(metadata) if metadata.is_file() && !metadata.file_type().is_symlink() => {
            remove_if_exists(temporary).await;
            return Ok(());
        }
        Ok(_) => {
            remove_if_exists(temporary).await;
            return Err(ArtifactError::SourceNotRegular(destination.to_path_buf()));
        }
        Err(ArtifactError::Io { source, .. }) if source.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    if let Err(source) = tokio::fs::rename(temporary, destination).await {
        if source.kind() == std::io::ErrorKind::AlreadyExists
            && symlink_metadata(destination)
                .await
                .is_ok_and(|metadata| metadata.is_file() && !metadata.file_type().is_symlink())
        {
            remove_if_exists(temporary).await;
            return Ok(());
        }
        return Err(ArtifactError::Io {
            path: destination.to_path_buf(),
            source,
        });
    }
    let metadata = metadata(destination).await?;
    let mut permissions = metadata.permissions();
    permissions.set_readonly(true);
    tokio::fs::set_permissions(destination, permissions)
        .await
        .map_err(|source| ArtifactError::Io {
            path: destination.to_path_buf(),
            source,
        })
}

fn unique_stored_bytes(existing: &[Artifact]) -> u64 {
    let mut seen = BTreeSet::new();
    existing
        .iter()
        .filter(|artifact| seen.insert(artifact.sha256.as_str()))
        .fold(0_u64, |total, artifact| {
            total.saturating_add(artifact.size_bytes)
        })
}

async fn metadata(path: &Path) -> Result<std::fs::Metadata, ArtifactError> {
    tokio::fs::metadata(path)
        .await
        .map_err(|source| ArtifactError::Io {
            path: path.to_path_buf(),
            source,
        })
}

async fn symlink_metadata(path: &Path) -> Result<std::fs::Metadata, ArtifactError> {
    tokio::fs::symlink_metadata(path)
        .await
        .map_err(|source| ArtifactError::Io {
            path: path.to_path_buf(),
            source,
        })
}

async fn create_dir_all(path: &Path) -> Result<(), ArtifactError> {
    tokio::fs::create_dir_all(path)
        .await
        .map_err(|source| ArtifactError::Io {
            path: path.to_path_buf(),
            source,
        })
}

async fn remove_if_exists(path: &Path) {
    let _ = tokio::fs::remove_file(path).await;
}

fn hex_digest(bytes: impl AsRef<[u8]>) -> String {
    bytes
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
