use super::*;
use pretty_assertions::assert_eq;
use std::path::PathBuf;
use tempfile::TempDir;

struct ReadyGuard;

impl PlatformGuard for ReadyGuard {
    fn preflight(&self, _work_root: &Path) -> GuardPreflightReport {
        GuardPreflightReport {
            status: GuardPreflightStatus::Ready,
            platform: "test",
            capabilities: GuardCapabilities {
                process_group: true,
                temp_workdir: true,
                resource_limits: true,
                file_rules: true,
                network_rules: true,
            },
            failures: Vec::new(),
        }
    }
}

#[test]
fn full_capabilities_allow_hardened() {
    let report = ReadyGuard.preflight(&PathBuf::from("/tmp"));
    assert!(report.allows_hardened());
    assert_eq!(
        report.capabilities.missing_for_hardened(),
        Vec::<&str>::new()
    );
}

#[test]
fn native_guard_stays_fail_closed_without_file_and_network_rules() {
    let temp = TempDir::new().expect("tempdir");
    let report = native_platform_guard().preflight(temp.path());
    assert!(!report.allows_hardened());
    assert!(!report.capabilities.file_rules);
    assert!(!report.capabilities.network_rules);
    #[cfg(target_os = "linux")]
    {
        assert_eq!(report.status, GuardPreflightStatus::Failed);
        assert_eq!(report.platform, "linux");
        assert!(report.capabilities.process_group);
        assert!(report.capabilities.temp_workdir);
        assert!(report.capabilities.resource_limits);
    }
    #[cfg(not(target_os = "linux"))]
    {
        assert_eq!(report.status, GuardPreflightStatus::UnsupportedPlatform);
    }
}

#[test]
fn refusal_message_lists_missing_capabilities() {
    let report = GuardPreflightReport {
        status: GuardPreflightStatus::Failed,
        platform: "linux",
        capabilities: GuardCapabilities {
            process_group: true,
            temp_workdir: true,
            resource_limits: true,
            file_rules: false,
            network_rules: false,
        },
        failures: vec![
            "file_rules: not implemented".to_string(),
            "network_rules: not implemented".to_string(),
        ],
    };
    let message = report.refusal_message("Hardened");
    assert!(message.contains("file_rules"));
    assert!(message.contains("network_rules"));
}
