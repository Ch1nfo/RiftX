use crate::GuardCapabilities;
use crate::GuardPreflightReport;
use crate::GuardPreflightStatus;
use crate::PlatformGuard;
use std::path::Path;

pub(crate) struct UnsupportedPlatformGuard;

impl PlatformGuard for UnsupportedPlatformGuard {
    fn preflight(&self, _work_root: &Path) -> GuardPreflightReport {
        GuardPreflightReport {
            status: GuardPreflightStatus::UnsupportedPlatform,
            platform: current_platform(),
            capabilities: GuardCapabilities::default(),
            failures: vec![format!(
                "RiftX Guard is not implemented for {}",
                current_platform()
            )],
        }
    }
}

fn current_platform() -> &'static str {
    if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(target_os = "windows") {
        "windows"
    } else {
        "unknown"
    }
}
