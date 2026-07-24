//! Hardened launch policy applied to tool child processes via `pre_exec`.

use std::io;
use std::path::Path;
use std::path::PathBuf;
use tokio::process::Command;

/// Filesystem and network isolation policy for one Hardened/Auto child.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GuardExecPolicy {
    pub work_root: PathBuf,
    pub readable_roots: Vec<PathBuf>,
}

impl GuardExecPolicy {
    /// Build a policy for a tool binary running inside an engagement workspace.
    pub fn for_tool(work_root: impl Into<PathBuf>, program: &Path) -> io::Result<Self> {
        let work_root = work_root.into();
        if !work_root.is_absolute() || !program.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Hardened launch requires absolute work_root and program paths",
            ));
        }
        let mut readable_roots = default_system_roots();
        if let Ok(canonical_program) = program.canonicalize() {
            if let Some(parent) = canonical_program.parent() {
                readable_roots.push(parent.to_path_buf());
            }
            readable_roots.push(canonical_program);
        } else {
            if let Some(parent) = program.parent() {
                readable_roots.push(parent.to_path_buf());
            }
            readable_roots.push(program.to_path_buf());
        }
        readable_roots.sort();
        readable_roots.dedup();
        Ok(Self {
            work_root,
            readable_roots,
        })
    }
}

fn default_system_roots() -> Vec<PathBuf> {
    [
        "/usr", "/lib", "/lib64", "/lib32", "/bin", "/sbin", "/etc", "/dev", "/proc", "/sys",
    ]
    .into_iter()
    .map(PathBuf::from)
    .filter(|path| path.exists())
    .collect()
}

/// Attach Landlock + network-namespace isolation to `command` before `spawn`.
///
/// On non-Linux platforms this returns an error so Hardened never silently
/// launches an unrestricted child.
pub fn apply_hardened_launch(command: &mut Command, policy: GuardExecPolicy) -> io::Result<()> {
    #[cfg(target_os = "linux")]
    {
        // Safety: pre_exec runs in the child after fork and before exec. The
        // closure only touches the child's namespaces and Landlock state.
        unsafe {
            command.pre_exec(move || crate::linux::apply_hardened_isolation(&policy));
        }
        Ok(())
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = (command, policy);
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "Hardened process launch requires the Linux RiftX Guard",
        ))
    }
}
