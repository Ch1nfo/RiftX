//! Hardened launch policy applied to tool child processes via `pre_exec`.

use std::collections::HashMap;
use std::io;
use std::path::Path;
use std::path::PathBuf;
use tokio::process::Command;

/// Environment variable injected into Hardened/Auto tool process environments.
pub const RIFTX_GUARD_WORK_ROOT_ENV: &str = "RIFTX_GUARD_WORK_ROOT";

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

    /// Build a policy from a spawn environment and program path or name.
    ///
    /// Returns `Ok(None)` when the Guard env marker is absent (Native Mode).
    pub fn from_spawn_env(
        env: &HashMap<String, String>,
        program: &str,
    ) -> io::Result<Option<Self>> {
        let Some(work_root) = env.get(RIFTX_GUARD_WORK_ROOT_ENV) else {
            return Ok(None);
        };
        let work_root = PathBuf::from(work_root);
        if !work_root.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("{RIFTX_GUARD_WORK_ROOT_ENV} must be an absolute path"),
            ));
        }
        let program_path = PathBuf::from(program);
        if program_path.is_absolute() {
            return Self::for_tool(work_root, &program_path).map(Some);
        }
        // Shell wrappers often pass bare names; keep system roots readable so
        // PATH lookup targets and the dynamic linker remain available.
        Ok(Some(Self {
            work_root,
            readable_roots: default_system_roots(),
        }))
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

/// Same as [`apply_hardened_launch`], for `std::process::Command` (PTY path).
pub fn apply_hardened_launch_std(
    command: &mut std::process::Command,
    policy: GuardExecPolicy,
) -> io::Result<()> {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        // Safety: pre_exec runs in the child after fork and before exec.
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
