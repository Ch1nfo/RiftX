use crate::GuardCapabilities;
use crate::GuardPreflightReport;
use crate::GuardPreflightStatus;
use crate::PlatformGuard;
use landlock::ABI;
use landlock::Access;
use landlock::AccessFs;
use landlock::CompatLevel;
use landlock::Compatible;
use landlock::Ruleset;
use landlock::RulesetAttr;
use landlock::RulesetCreatedAttr;
use landlock::RulesetStatus;
use landlock::path_beneath_rules;
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
        match probe_file_rules(work_root) {
            Ok(()) => capabilities.file_rules = true,
            Err(error) => failures.push(format!("file_rules: {error}")),
        }
        match probe_network_rules() {
            Ok(()) => capabilities.network_rules = true,
            Err(error) => failures.push(format!("network_rules: {error}")),
        }

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

fn probe_file_rules(work_root: &Path) -> io::Result<()> {
    fs::create_dir_all(work_root)?;
    let allowed = work_root
        .canonicalize()
        .unwrap_or_else(|_| work_root.to_path_buf());
    let denied_parent = allowed
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "work root has no parent"))?;
    let denied = denied_parent.join(format!("riftx-guard-denied-{}", std::process::id()));
    fs::write(&denied, b"secret")?;

    let allowed_for_child = allowed.clone();
    let denied_for_child = denied.clone();
    let marker = allowed.join("landlock-ok");
    let result = run_in_child(move || {
        if let Err(error) = enforce_landlock_work_root(&allowed_for_child) {
            return ChildProbeExit::Failed(error);
        }
        if fs::read(&denied_for_child).is_ok() {
            return ChildProbeExit::Failed(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "Landlock did not deny access outside the work root",
            ));
        }
        if let Err(error) = fs::write(&marker, b"ok") {
            return ChildProbeExit::Failed(error);
        }
        ChildProbeExit::Ok
    });
    let _ = fs::remove_file(&denied);
    let _ = fs::remove_file(&marker);
    result
}

fn enforce_landlock_work_root(work_root: &Path) -> io::Result<()> {
    let abi = ABI::V3;
    let access_all = AccessFs::from_all(abi);
    let paths: [&Path; 1] = [work_root];
    let status = Ruleset::default()
        .set_compatibility(CompatLevel::HardRequirement)
        .handle_access(access_all)
        .map_err(landlock_io_error)?
        .create()
        .map_err(landlock_io_error)?
        .add_rules(path_beneath_rules(paths, access_all))
        .map_err(landlock_io_error)?
        .restrict_self()
        .map_err(landlock_io_error)?;
    match status.ruleset {
        RulesetStatus::FullyEnforced => Ok(()),
        RulesetStatus::PartiallyEnforced => Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "Landlock is only partially enforced",
        )),
        RulesetStatus::NotEnforced => Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "Landlock is not enforced by the kernel",
        )),
    }
}

fn probe_network_rules() -> io::Result<()> {
    run_in_child(|| {
        // Prefer an unprivileged user+net namespace pair so Lab hosts without
        // CAP_NET_ADMIN can still prove network isolation is available.
        let flags = libc::CLONE_NEWUSER | libc::CLONE_NEWNET;
        let result = unsafe { libc::unshare(flags) };
        if result != 0 {
            return ChildProbeExit::Failed(io::Error::last_os_error());
        }
        ChildProbeExit::Ok
    })
}

enum ChildProbeExit {
    Ok,
    Failed(io::Error),
}

fn run_in_child(child_body: impl FnOnce() -> ChildProbeExit) -> io::Result<()> {
    let pid = unsafe { libc::fork() };
    if pid < 0 {
        return Err(io::Error::last_os_error());
    }
    if pid == 0 {
        let code = match child_body() {
            ChildProbeExit::Ok => 0,
            ChildProbeExit::Failed(_) => 1,
        };
        unsafe { libc::_exit(code) };
    }

    let mut status = 0;
    let waited = unsafe { libc::waitpid(pid, &mut status, 0) };
    if waited < 0 {
        return Err(io::Error::last_os_error());
    }
    if libc::WIFEXITED(status) && libc::WEXITSTATUS(status) == 0 {
        return Ok(());
    }
    if libc::WIFSIGNALED(status) {
        return Err(io::Error::new(
            io::ErrorKind::Other,
            format!(
                "guard probe child terminated by signal {}",
                libc::WTERMSIG(status)
            ),
        ));
    }
    Err(io::Error::new(
        io::ErrorKind::PermissionDenied,
        format!(
            "guard probe child exited with status {}",
            libc::WEXITSTATUS(status)
        ),
    ))
}

fn landlock_io_error(error: impl std::fmt::Display) -> io::Error {
    io::Error::other(error.to_string())
}
