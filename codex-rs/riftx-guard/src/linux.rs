use crate::GuardCapabilities;
use crate::GuardPreflightReport;
use crate::GuardPreflightStatus;
use crate::PlatformGuard;
use std::fs;
use std::io;
use std::path::Path;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

pub(crate) struct LinuxPlatformGuard;

impl PlatformGuard for LinuxPlatformGuard {
    fn preflight(&self, work_root: &Path) -> GuardPreflightReport {
        let mut capabilities = GuardCapabilities::default();
        let mut failures = Vec::new();

        match probe_process_group() {
            Ok(()) => capabilities.process_group = true,
            Err(error) => failures.push(format!("process_group: {error}")),
        }
        match probe_temp_workdir(work_root) {
            Ok(()) => capabilities.temp_workdir = true,
            Err(error) => failures.push(format!("temp_workdir: {error}")),
        }
        match probe_resource_limits() {
            Ok(()) => capabilities.resource_limits = true,
            Err(error) => failures.push(format!("resource_limits: {error}")),
        }

        // File and network OS rules are not implemented in this slice. Keep
        // Hardened fail-closed until those capabilities exist.
        failures.push("file_rules: not implemented".to_string());
        failures.push("network_rules: not implemented".to_string());

        let status = if capabilities.hardened_ready() && failures.is_empty() {
            GuardPreflightStatus::Ready
        } else {
            GuardPreflightStatus::Failed
        };

        GuardPreflightReport {
            status,
            platform: "linux",
            capabilities,
            failures,
        }
    }
}

fn probe_process_group() -> io::Result<()> {
    let pgid = unsafe { libc::getpgid(0) };
    if pgid < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn probe_temp_workdir(work_root: &Path) -> io::Result<()> {
    fs::create_dir_all(work_root)?;
    let dir = work_root.join("riftx-guard-preflight");
    if dir.exists() {
        fs::remove_dir_all(&dir)?;
    }
    fs::create_dir(&dir)?;
    let mut permissions = fs::metadata(&dir)?.permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&dir, permissions)?;
    let marker = dir.join("probe");
    fs::write(&marker, b"ok")?;
    fs::remove_file(&marker)?;
    fs::remove_dir(&dir)?;
    Ok(())
}

fn probe_resource_limits() -> io::Result<()> {
    let mut current = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    let result = unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut current) };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    if current.rlim_cur == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "RLIMIT_NOFILE soft limit is zero",
        ));
    }
    Ok(())
}
