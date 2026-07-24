//! Platform Guard preflight for Hardened and Auto modes.
//!
//! This crate probes OS-native enforcement capabilities. Hardened/Auto must
//! refuse to start unless every required capability is present. Missing file or
//! network rules keep the mode fail-closed even when lighter probes succeed.

mod exec;
#[cfg(target_os = "linux")]
mod linux;
#[cfg(not(target_os = "linux"))]
mod unsupported;

pub use exec::GuardExecPolicy;
pub use exec::apply_hardened_launch;

use std::path::Path;
use std::sync::Arc;

/// Capability checklist required before Hardened or Auto may start.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct GuardCapabilities {
    pub process_group: bool,
    pub temp_workdir: bool,
    pub resource_limits: bool,
    pub file_rules: bool,
    pub network_rules: bool,
}

impl GuardCapabilities {
    pub fn hardened_ready(&self) -> bool {
        self.process_group
            && self.temp_workdir
            && self.resource_limits
            && self.file_rules
            && self.network_rules
    }

    pub fn missing_for_hardened(&self) -> Vec<&'static str> {
        let mut missing = Vec::new();
        if !self.process_group {
            missing.push("process_group");
        }
        if !self.temp_workdir {
            missing.push("temp_workdir");
        }
        if !self.resource_limits {
            missing.push("resource_limits");
        }
        if !self.file_rules {
            missing.push("file_rules");
        }
        if !self.network_rules {
            missing.push("network_rules");
        }
        missing
    }
}

/// Outcome of a Guard preflight probe.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GuardPreflightStatus {
    Ready,
    Failed,
    UnsupportedPlatform,
}

/// Structured preflight result consumed by `riftxd`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GuardPreflightReport {
    pub status: GuardPreflightStatus,
    pub platform: &'static str,
    pub capabilities: GuardCapabilities,
    pub failures: Vec<String>,
}

impl GuardPreflightReport {
    pub fn allows_hardened(&self) -> bool {
        self.status == GuardPreflightStatus::Ready && self.capabilities.hardened_ready()
    }

    pub fn refusal_message(&self, mode_label: &str) -> String {
        if self.status == GuardPreflightStatus::UnsupportedPlatform {
            return format!(
                "{mode_label} Mode cannot start until the platform RiftX Guard is available on {}",
                self.platform
            );
        }
        let missing = self.capabilities.missing_for_hardened();
        if missing.is_empty() && self.failures.is_empty() {
            return format!("{mode_label} Mode cannot start until RiftX Guard preflight succeeds");
        }
        let mut parts = Vec::new();
        if !missing.is_empty() {
            parts.push(format!("missing capabilities: {}", missing.join(", ")));
        }
        if !self.failures.is_empty() {
            parts.push(format!("failures: {}", self.failures.join("; ")));
        }
        format!(
            "{mode_label} Mode cannot start until RiftX Guard preflight succeeds ({})",
            parts.join("; ")
        )
    }
}

/// Platform-specific Guard implementation.
pub trait PlatformGuard: Send + Sync {
    fn preflight(&self, work_root: &Path) -> GuardPreflightReport;
}

/// Construct the native Guard for the current OS.
pub fn native_platform_guard() -> Arc<dyn PlatformGuard> {
    #[cfg(target_os = "linux")]
    {
        Arc::new(linux::LinuxPlatformGuard)
    }
    #[cfg(not(target_os = "linux"))]
    {
        Arc::new(unsupported::UnsupportedPlatformGuard)
    }
}

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
