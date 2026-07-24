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

#[test]
fn spawn_env_policy_is_absent_without_marker() {
    let env = std::collections::HashMap::new();
    assert_eq!(
        GuardExecPolicy::from_spawn_env(&env, "/usr/bin/true").expect("no guard"),
        None
    );
}

#[test]
fn spawn_env_policy_requires_absolute_work_root() {
    let env = std::collections::HashMap::from([(
        RIFTX_GUARD_WORK_ROOT_ENV.to_string(),
        "relative-work".to_string(),
    )]);
    let err = GuardExecPolicy::from_spawn_env(&env, "/usr/bin/true").expect_err("relative");
    assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput);
}

#[test]
fn spawn_env_policy_allows_bare_program_names() {
    let env = std::collections::HashMap::from([(
        RIFTX_GUARD_WORK_ROOT_ENV.to_string(),
        "/tmp/riftx-work".to_string(),
    )]);
    let policy = GuardExecPolicy::from_spawn_env(&env, "bash")
        .expect("policy")
        .expect("present");
    assert_eq!(policy.work_root, PathBuf::from("/tmp/riftx-work"));
    assert!(!policy.readable_roots.is_empty());
    assert!(policy.network.is_empty());
}

#[test]
fn network_policy_round_trips_env_encoding() {
    assert!(GuardNetworkPolicy::default().is_empty());
    assert_eq!(GuardNetworkPolicy::default().encode_env(), "");
    assert_eq!(
        GuardNetworkPolicy::decode_env("").expect("empty"),
        GuardNetworkPolicy::default()
    );

    let policy = GuardNetworkPolicy::from_cidrs_and_ports(
        vec![
            "10.0.0.0/8".parse().expect("cidr"),
            "192.168.1.0/24".parse().expect("cidr"),
        ],
        vec![443, 22, 22],
    );
    assert_eq!(policy.ports, vec![22, 443]);
    let encoded = policy.encode_env();
    assert_eq!(encoded, "10.0.0.0/8,192.168.1.0/24;ports=22,443");
    assert_eq!(
        GuardNetworkPolicy::decode_env(&encoded).expect("decode"),
        policy
    );
}

#[test]
fn network_policy_rejects_invalid_cidr() {
    let err = GuardNetworkPolicy::decode_env("not-a-cidr").expect_err("invalid");
    assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput);
}

#[test]
fn empty_network_policy_renders_no_nft_script() {
    assert_eq!(GuardNetworkPolicy::default().nftables_script(), None);
}

#[test]
fn network_policy_renders_loopback_allowlist_script() {
    let policy = GuardNetworkPolicy::from_cidrs_and_ports(
        vec!["127.0.0.0/8".parse().expect("cidr")],
        vec![8080],
    );
    let script = policy.nftables_script().expect("script");
    assert!(script.contains("policy drop"));
    assert!(script.contains("oifname \"lo\" accept"));
    assert!(script.contains("ip daddr 127.0.0.0/8"));
    assert!(script.contains("8080"));
}

#[test]
fn spawn_env_policy_parses_network_allowlist() {
    let env = std::collections::HashMap::from([
        (
            RIFTX_GUARD_WORK_ROOT_ENV.to_string(),
            "/tmp/riftx-work".to_string(),
        ),
        (
            RIFTX_GUARD_NET_ENV.to_string(),
            "10.0.0.0/8;ports=443".to_string(),
        ),
    ]);
    let policy = GuardExecPolicy::from_spawn_env(&env, "/bin/true")
        .expect("policy")
        .expect("present");
    assert_eq!(
        policy.network,
        GuardNetworkPolicy::from_cidrs_and_ports(
            vec!["10.0.0.0/8".parse().expect("cidr")],
            vec![443],
        )
    );
}
