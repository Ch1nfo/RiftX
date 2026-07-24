//! Hardened launch policy applied to tool child processes via `pre_exec`.

use ipnet::IpNet;
use std::collections::HashMap;
use std::io;
use std::net::IpAddr;
use std::path::Path;
use std::path::PathBuf;
use tokio::process::Command;

/// Environment variable injected into Hardened/Auto tool process environments.
pub const RIFTX_GUARD_WORK_ROOT_ENV: &str = "RIFTX_GUARD_WORK_ROOT";

/// Compact Network Scope allowlist for Hardened/Auto child processes.
///
/// Format: `cidr1,cidr2;ports=22,443` — either half may be empty.
/// Domains are intentionally omitted (application-layer only for this slice).
pub const RIFTX_GUARD_NET_ENV: &str = "RIFTX_GUARD_NET";

/// CIDR + port egress allowlist applied inside the child network namespace.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct GuardNetworkPolicy {
    pub cidrs: Vec<IpNet>,
    pub ports: Vec<u16>,
}

impl GuardNetworkPolicy {
    pub fn is_empty(&self) -> bool {
        self.cidrs.is_empty() && self.ports.is_empty()
    }

    /// Build from authorization Network Scope fields (domains ignored).
    pub fn from_cidrs_and_ports(cidrs: Vec<IpNet>, ports: Vec<u16>) -> Self {
        let mut ports = ports;
        ports.sort_unstable();
        ports.dedup();
        Self { cidrs, ports }
    }

    /// Encode for injection into the tool process environment.
    pub fn encode_env(&self) -> String {
        let cidrs = self
            .cidrs
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join(",");
        if self.ports.is_empty() {
            cidrs
        } else {
            let ports = self
                .ports
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>()
                .join(",");
            format!("{cidrs};ports={ports}")
        }
    }

    /// Parse the compact env encoding. Empty string yields an empty policy.
    pub fn decode_env(value: &str) -> io::Result<Self> {
        let value = value.trim();
        if value.is_empty() {
            return Ok(Self::default());
        }
        let (cidrs_part, ports_part) = match value.split_once(";ports=") {
            Some((cidrs, ports)) => (cidrs, Some(ports)),
            None => {
                if let Some(ports) = value.strip_prefix("ports=") {
                    ("", Some(ports))
                } else {
                    (value, None)
                }
            }
        };
        let mut cidrs = Vec::new();
        for token in cidrs_part.split(',').filter(|token| !token.is_empty()) {
            let net: IpNet = token.parse().map_err(|error| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("invalid Guard network CIDR `{token}`: {error}"),
                )
            })?;
            cidrs.push(net);
        }
        let mut ports = Vec::new();
        if let Some(ports_part) = ports_part {
            for token in ports_part.split(',').filter(|token| !token.is_empty()) {
                let port: u16 = token.parse().map_err(|error| {
                    io::Error::new(
                        io::ErrorKind::InvalidInput,
                        format!("invalid Guard network port `{token}`: {error}"),
                    )
                })?;
                ports.push(port);
            }
        }
        Ok(Self::from_cidrs_and_ports(cidrs, ports))
    }

    /// Render an nftables script for the current network namespace.
    ///
    /// Empty policies produce no script (caller keeps the empty netns deny-all).
    pub fn nftables_script(&self) -> Option<String> {
        if self.is_empty() {
            return None;
        }
        let mut lines = vec![
            "table inet riftx_guard {".to_string(),
            "  chain output {".to_string(),
            "    type filter hook output priority 0; policy drop;".to_string(),
            "    oifname \"lo\" accept".to_string(),
        ];
        let port_list = if self.ports.is_empty() {
            None
        } else {
            Some(
                self.ports
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join(", "),
            )
        };
        if self.cidrs.is_empty() {
            if let Some(ports) = &port_list {
                lines.push(format!(
                    "    meta l4proto {{ tcp, udp }} th dport {{ {ports} }} accept"
                ));
            }
        } else {
            for cidr in &self.cidrs {
                match cidr.addr() {
                    IpAddr::V4(_) => {
                        if let Some(ports) = &port_list {
                            lines.push(format!(
                                "    ip daddr {cidr} meta l4proto {{ tcp, udp }} th dport {{ {ports} }} accept"
                            ));
                        } else {
                            lines.push(format!("    ip daddr {cidr} accept"));
                        }
                    }
                    IpAddr::V6(_) => {
                        if let Some(ports) = &port_list {
                            lines.push(format!(
                                "    ip6 daddr {cidr} meta l4proto {{ tcp, udp }} th dport {{ {ports} }} accept"
                            ));
                        } else {
                            lines.push(format!("    ip6 daddr {cidr} accept"));
                        }
                    }
                }
            }
        }
        lines.push("  }".to_string());
        lines.push("}".to_string());
        Some(lines.join("\n"))
    }
}

/// Filesystem and network isolation policy for one Hardened/Auto child.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GuardExecPolicy {
    pub work_root: PathBuf,
    pub readable_roots: Vec<PathBuf>,
    pub network: GuardNetworkPolicy,
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
            network: GuardNetworkPolicy::default(),
        })
    }

    pub fn with_network(mut self, network: GuardNetworkPolicy) -> Self {
        self.network = network;
        self
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
        let network = match env.get(RIFTX_GUARD_NET_ENV) {
            Some(value) => GuardNetworkPolicy::decode_env(value)?,
            None => GuardNetworkPolicy::default(),
        };
        let program_path = PathBuf::from(program);
        if program_path.is_absolute() {
            return Self::for_tool(work_root, &program_path)
                .map(|policy| Some(policy.with_network(network)));
        }
        // Shell wrappers often pass bare names; keep system roots readable so
        // PATH lookup targets and the dynamic linker remain available.
        Ok(Some(Self {
            work_root,
            readable_roots: default_system_roots(),
            network,
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
