#[cfg(windows)]
use sha2::Digest;
#[cfg(windows)]
use sha2::Sha256;
use std::fmt;
use std::path::Path;
use std::path::PathBuf;

/// A platform-native endpoint for the local RiftX daemon.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LocalIpcEndpoint {
    runtime_dir: PathBuf,
}

impl LocalIpcEndpoint {
    pub fn new(runtime_dir: impl Into<PathBuf>) -> Self {
        Self {
            runtime_dir: runtime_dir.into(),
        }
    }

    pub fn runtime_dir(&self) -> &Path {
        &self.runtime_dir
    }

    #[cfg(unix)]
    pub fn socket_path(&self) -> PathBuf {
        self.runtime_dir.join("riftxd.sock")
    }

    #[cfg(windows)]
    pub fn pipe_name(&self) -> String {
        let absolute = if self.runtime_dir.is_absolute() {
            self.runtime_dir.clone()
        } else {
            std::env::current_dir()
                .unwrap_or_default()
                .join(&self.runtime_dir)
        };
        let digest = Sha256::digest(absolute.to_string_lossy().as_bytes());
        let suffix = digest[..12]
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        format!(r"\\.\pipe\riftx-{suffix}")
    }
}

impl fmt::Display for LocalIpcEndpoint {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        #[cfg(unix)]
        {
            return self.socket_path().display().fmt(formatter);
        }
        #[cfg(windows)]
        {
            return self.pipe_name().fmt(formatter);
        }
        #[allow(unreachable_code)]
        self.runtime_dir.display().fmt(formatter)
    }
}
