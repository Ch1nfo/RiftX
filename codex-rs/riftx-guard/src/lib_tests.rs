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
fn native_guard_reports_linux_or_unsupported_preflight() {
    let temp = TempDir::new().expect("tempdir");
    let report = native_platform_guard().preflight(temp.path());
    #[cfg(target_os = "linux")]
    {
        assert_eq!(report.platform, "linux");
        assert!(report.capabilities.process_group);
        assert!(report.capabilities.temp_workdir);
        assert!(report.capabilities.resource_limits);
        if report.allows_hardened() {
            assert_eq!(report.status, GuardPreflightStatus::Ready);
            assert!(report.capabilities.file_rules);
            assert!(report.capabilities.network_rules);
            assert!(report.failures.is_empty());
        } else {
            assert_eq!(report.status, GuardPreflightStatus::Failed);
            assert!(!report.capabilities.hardened_ready());
            assert!(!report.failures.is_empty());
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        assert!(!report.allows_hardened());
        assert!(!report.capabilities.file_rules);
        assert!(!report.capabilities.network_rules);
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
            "file_rules: Landlock is not enforced by the kernel".to_string(),
            "network_rules: Operation not permitted".to_string(),
        ],
    };
    let message = report.refusal_message("Hardened");
    assert!(message.contains("file_rules"));
    assert!(message.contains("network_rules"));
}

#[test]
fn tool_exec_policy_requires_absolute_paths_and_includes_program_root() {
    let err = GuardExecPolicy::for_tool("relative-work", Path::new("/usr/bin/true"))
        .expect_err("relative work root");
    assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput);

    let policy =
        GuardExecPolicy::for_tool(PathBuf::from("/tmp/riftx-work"), Path::new("/usr/bin/true"))
            .expect("absolute policy");
    assert_eq!(policy.work_root, PathBuf::from("/tmp/riftx-work"));
    assert!(
        policy
            .readable_roots
            .iter()
            .any(|path| path.ends_with("bin")
                || path.as_path() == Path::new("/usr/bin/true")
                || path.ends_with("true"))
    );
}
