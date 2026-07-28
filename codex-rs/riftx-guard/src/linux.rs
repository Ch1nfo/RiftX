use crate::GuardCapabilities;
use crate::GuardExecPolicy;
use crate::GuardNetworkPolicy;
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
use std::os::fd::FromRawFd;
use std::os::fd::OwnedFd;
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

pub(crate) fn apply_hardened_isolation(policy: &GuardExecPolicy) -> io::Result<()> {
    enter_network_namespace()?;
    apply_network_scope(&policy.network)?;
    enforce_landlock_policy(policy)
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

    let policy = GuardExecPolicy {
        work_root: allowed.clone(),
        readable_roots: Vec::new(),
        network: GuardNetworkPolicy::default(),
    };
    let denied_for_child = denied.clone();
    let marker = allowed.join("landlock-ok");
    let result = run_in_child(move || {
        if let Err(error) = enforce_landlock_policy(&policy) {
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

fn probe_network_rules() -> io::Result<()> {
    run_in_child(|| {
        if let Err(error) = enter_network_namespace() {
            return ChildProbeExit::Failed(error);
        }
        // Preflight must prove nftables can be installed inside the netns so
        // non-empty Network Scope allowlists fail closed at mode start.
        let probe = GuardNetworkPolicy::from_cidrs_and_ports(
            vec!["127.0.0.0/8".parse().expect("loopback cidr")],
            vec![9],
        );
        match apply_network_scope(&probe) {
            Ok(()) => ChildProbeExit::Ok,
            Err(error) => ChildProbeExit::Failed(error),
        }
    })
}

fn enter_network_namespace() -> io::Result<()> {
    let uid = unsafe { libc::getuid() };
    let gid = unsafe { libc::getgid() };
    let flags = libc::CLONE_NEWUSER | libc::CLONE_NEWNET;
    if unsafe { libc::unshare(flags) } != 0 {
        return Err(io::Error::last_os_error());
    }
    fs::write("/proc/self/setgroups", b"deny")?;
    fs::write("/proc/self/uid_map", format!("0 {uid} 1\n"))?;
    fs::write("/proc/self/gid_map", format!("0 {gid} 1\n"))?;
    Ok(())
}

fn apply_network_scope(policy: &GuardNetworkPolicy) -> io::Result<()> {
    let Some(script) = policy.nftables_script() else {
        // Empty Scope: stay in the isolated netns with no allowlist (deny-all).
        return Ok(());
    };
    bring_up_loopback()?;
    run_nft_script(&script)
}

fn bring_up_loopback() -> io::Result<()> {
    // Prefer the portable `ip` tool when present; fall back to ioctl.
    let status = std::process::Command::new("ip")
        .args(["link", "set", "lo", "up"])
        .status();
    match status {
        Ok(status) if status.success() => Ok(()),
        Ok(status) => Err(io::Error::other(format!(
            "failed to bring up loopback (ip exited {status})"
        ))),
        Err(error) if error.kind() == io::ErrorKind::NotFound => bring_up_loopback_ioctl(),
        Err(error) => Err(error),
    }
}

fn bring_up_loopback_ioctl() -> io::Result<()> {
    use std::mem::MaybeUninit;
    use std::os::fd::AsRawFd;

    #[repr(C)]
    struct IfReq {
        ifr_name: [libc::c_char; libc::IFNAMSIZ],
        ifr_flags: libc::c_short,
    }

    let fd = unsafe { libc::socket(libc::AF_INET, libc::SOCK_DGRAM, 0) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let socket = unsafe { OwnedFd::from_raw_fd(fd) };

    let mut req = MaybeUninit::<IfReq>::zeroed();
    unsafe {
        let req = req.assume_init_mut();
        let name = b"lo\0";
        for (dst, src) in req.ifr_name.iter_mut().zip(name.iter()) {
            *dst = *src as libc::c_char;
        }
        if libc::ioctl(socket.as_raw_fd(), libc::SIOCGIFFLAGS, req as *mut IfReq) != 0 {
            return Err(io::Error::last_os_error());
        }
        req.ifr_flags |= libc::IFF_UP as libc::c_short;
        if libc::ioctl(socket.as_raw_fd(), libc::SIOCSIFFLAGS, req as *mut IfReq) != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

fn run_nft_script(script: &str) -> io::Result<()> {
    let nft = ["/usr/sbin/nft", "/sbin/nft", "/usr/bin/nft"]
        .into_iter()
        .find(|candidate| Path::new(candidate).exists())
        .unwrap_or("nft");
    let mut child = std::process::Command::new(nft)
        .args(["-f", "-"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .map_err(|error| {
            if error.kind() == io::ErrorKind::NotFound {
                io::Error::new(
                    io::ErrorKind::NotFound,
                    "nftables (`nft`) is required for Hardened Network Scope",
                )
            } else {
                error
            }
        })?;
    use std::io::Write;
    {
        let stdin = child
            .stdin
            .as_mut()
            .ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "nft stdin unavailable"))?;
        stdin.write_all(script.as_bytes())?;
    }
    let output = child.wait_with_output()?;
    if output.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    Err(io::Error::other(format!(
        "nftables rule install failed: {}",
        stderr.trim()
    )))
}

fn enforce_landlock_policy(policy: &GuardExecPolicy) -> io::Result<()> {
    let abi = ABI::V3;
    let access_all = AccessFs::from_all(abi);
    let access_read = AccessFs::from_read(abi);
    let work_root = policy.work_root.as_path();
    let readable: Vec<&Path> = policy
        .readable_roots
        .iter()
        .map(std::path::PathBuf::as_path)
        .filter(|path| path.exists())
        .collect();
    let status = Ruleset::default()
        .set_compatibility(CompatLevel::HardRequirement)
        .handle_access(access_all)
        .map_err(landlock_io_error)?
        .create()
        .map_err(landlock_io_error)?
        .add_rules(path_beneath_rules([work_root], access_all))
        .map_err(landlock_io_error)?
        .add_rules(path_beneath_rules(readable, access_read))
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
