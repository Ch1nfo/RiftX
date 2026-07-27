use super::*;
use codex_riftx_tools::ToolMetadata;
use pretty_assertions::assert_eq;
use std::fs;
use tempfile::TempDir;

#[test]
fn low_risk_tool_follows_three_mode_table() {
    let fixture = Fixture::new("scanner", Some(ToolRisk::Low), &["network.discovery"]);
    let authorized = ["network.discovery".to_string()];
    for (mode, expected) in [
        (
            ExecutionMode::RedTeam,
            ExecutionDisposition::RequireApproval,
        ),
        (ExecutionMode::Pentest, ExecutionDisposition::Allow),
        (ExecutionMode::Auto, ExecutionDisposition::Allow),
    ] {
        let command = [fixture.tool_name.clone()];
        assert_eq!(
            disposition(
                &fixture.intent(mode, CommandSpec::Argv(&command)),
                /*now*/ 100,
                &authorized
            ),
            expected,
        );
    }
}

#[test]
fn pipelines_and_chains_cannot_hide_managed_high_risk_tools() {
    let fixture = Fixture::new("dangerous", Some(ToolRisk::Critical), &[]);
    let line = format!(
        "first one | sh -lc '{} two'; third three",
        fixture.tool_name
    );
    let line_intent = fixture.intent(ExecutionMode::Pentest, CommandSpec::CommandLine(&line));
    assert_eq!(line_intent.parse_status, ExecutionParseStatus::Parsed);
    assert_eq!(line_intent.risk, ExecutionRisk::Critical);
    assert_eq!(
        names(&line_intent),
        vec!["first", fixture.tool_name.as_str(), "third"],
    );
    assert_eq!(
        disposition(&line_intent, /*now*/ 100, &[]),
        ExecutionDisposition::RequireApproval,
    );

    let shell = [
        "sh".to_string(),
        "-lc".to_string(),
        format!(
            "echo ready | {} --target host && echo done",
            fixture.tool_name
        ),
    ];
    let shell_intent = fixture.intent(ExecutionMode::Pentest, CommandSpec::Argv(&shell));
    assert_eq!(shell_intent.parse_status, ExecutionParseStatus::Parsed);
    assert!(shell_intent.executables.iter().any(|executable| {
        executable.requested_name == fixture.tool_name
            && executable.risk == ExecutionRisk::Critical
            && executable.managed
    }));
}

#[test]
fn resolves_absolute_relative_and_path_executables() {
    let fixture = Fixture::new("path-tool", Some(ToolRisk::Low), &[]);
    let local_path = fixture.write("local-tool", b"local");
    let absolute_path = fixture.write("absolute-tool", b"absolute");
    let path_command = [fixture.tool_name.clone()];
    let relative_command = [format!(
        ".{}{}",
        std::path::MAIN_SEPARATOR,
        local_path.file_name().unwrap().to_string_lossy()
    )];
    let absolute_command = [absolute_path.to_string_lossy().into_owned()];
    for (command, expected) in [
        (&path_command[..], fixture.tool_path.as_path()),
        (&relative_command[..], local_path.as_path()),
        (&absolute_command[..], absolute_path.as_path()),
    ] {
        let intent = fixture.intent(ExecutionMode::Pentest, CommandSpec::Argv(command));
        assert_eq!(
            intent.executables[0].resolved_path.as_deref(),
            Some(expected)
        );
    }
}

#[test]
fn replacement_and_argument_changes_invalidate_binding() {
    let fixture = Fixture::new("replaceable", Some(ToolRisk::Low), &[]);
    let first_command = [fixture.tool_name.clone(), "one".to_string()];
    let second_command = [fixture.tool_name.clone(), "two".to_string()];
    let before = fixture.intent(ExecutionMode::Pentest, CommandSpec::Argv(&first_command));
    let changed_args = fixture.intent(ExecutionMode::Pentest, CommandSpec::Argv(&second_command));
    assert_ne!(before.argument_sha256, changed_args.argument_sha256);
    assert_ne!(before.binding_sha256, changed_args.binding_sha256);

    fs::write(&fixture.tool_path, b"replacement").unwrap();
    let replaced = fixture.intent(ExecutionMode::Pentest, CommandSpec::Argv(&first_command));
    assert_eq!(before.executables[0].inventory_hash_matches, Some(true));
    assert_eq!(replaced.executables[0].inventory_hash_matches, Some(false));
    assert_ne!(before.binding_sha256, replaced.binding_sha256);
    assert_eq!(
        disposition(&replaced, /*now*/ 100, &[]),
        ExecutionDisposition::Deny,
    );
}

#[test]
fn unknown_risk_expiry_and_capabilities_fail_closed() {
    let unknown = Fixture::new("unknown", /*risk*/ None, &[]);
    let unknown_command = [unknown.tool_name.clone()];
    let unknown_intent =
        unknown.intent(ExecutionMode::Pentest, CommandSpec::Argv(&unknown_command));
    assert_eq!(unknown_intent.risk, ExecutionRisk::Unknown);
    assert_eq!(
        unknown_intent.executables[0].risk_source,
        RiskSource::MissingRisk
    );
    assert_eq!(
        disposition(&unknown_intent, /*now*/ 100, &[]),
        ExecutionDisposition::RequireApproval,
    );

    let guarded = Fixture::new("guarded", Some(ToolRisk::Low), &["network.scan"]);
    let guarded_command = [guarded.tool_name.clone()];
    let guarded_intent =
        guarded.intent(ExecutionMode::Pentest, CommandSpec::Argv(&guarded_command));
    assert_eq!(
        decide(
            &guarded_intent,
            DecisionContext {
                now: 1_000,
                authorized_capabilities: &[],
            },
        ),
        ExecutionDecision {
            disposition: ExecutionDisposition::Deny,
            reasons: vec![
                DecisionReason::AuthorizationExpired,
                DecisionReason::CapabilityDenied {
                    capability: "network.scan".to_string(),
                },
            ],
        },
    );
}

#[test]
fn complex_shell_requires_approval_and_display_redacts_credentials() {
    let fixture = Fixture::new("simple", Some(ToolRisk::Low), &[]);
    let shell = [
        "sh".to_string(),
        "-lc".to_string(),
        format!("{} $(dynamic-command)", fixture.tool_name),
    ];
    let complex = fixture.intent(ExecutionMode::Pentest, CommandSpec::Argv(&shell));
    assert_eq!(complex.parse_status, ExecutionParseStatus::Complex);
    assert_eq!(
        disposition(&complex, /*now*/ 100, &[]),
        ExecutionDisposition::RequireApproval,
    );
    let dynamic_line = format!("{} $(dynamic-command)", fixture.tool_name);
    let line_intent = fixture.intent(
        ExecutionMode::Pentest,
        CommandSpec::CommandLine(&dynamic_line),
    );
    assert_eq!(line_intent.parse_status, ExecutionParseStatus::Complex);

    let secret = [
        fixture.tool_name.clone(),
        "--api-key".to_string(),
        "super-secret-value".to_string(),
    ];
    let redacted = fixture.intent(ExecutionMode::Pentest, CommandSpec::Argv(&secret));
    assert_eq!(
        redacted.display_argv,
        vec![
            fixture.tool_name,
            "--api-key".to_string(),
            "[REDACTED]".to_string()
        ],
    );
}

fn disposition(intent: &ExecutionIntent, now: i64, authorized: &[String]) -> ExecutionDisposition {
    decide(
        intent,
        DecisionContext {
            now,
            authorized_capabilities: authorized,
        },
    )
    .disposition
}

fn names(intent: &ExecutionIntent) -> Vec<&str> {
    intent
        .executables
        .iter()
        .map(|executable| executable.requested_name.as_str())
        .collect()
}

struct Fixture {
    root: TempDir,
    tool_name: String,
    tool_path: PathBuf,
    inventory: ToolInventory,
}

impl Fixture {
    fn new(name: &str, risk: Option<ToolRisk>, capabilities: &[&str]) -> Self {
        let root = tempfile::tempdir().unwrap();
        let tool_name = executable_name(name);
        let path = root.path().join(&tool_name);
        fs::write(&path, b"original").unwrap();
        let tool_path = path.canonicalize().unwrap();
        let inventory = ToolInventory {
            roots: vec![root.path().to_path_buf()],
            path_entries: vec![root.path().to_path_buf()],
            tools: vec![DiscoveredTool {
                name: tool_name.clone(),
                path: tool_path.clone(),
                sha256: hash_file(&tool_path).unwrap(),
                metadata_path: None,
                metadata_sha256: None,
                metadata: Some(ToolMetadata {
                    schema_version: 1,
                    capabilities: capabilities.iter().map(ToString::to_string).collect(),
                    risk,
                    help_args: Vec::new(),
                    version_args: Vec::new(),
                    health_check_args: Vec::new(),
                    input_target_field: None,
                    output_format: None,
                    parser: None,
                    credential: None,
                }),
                shadowed_by: None,
            }],
            snapshot_sha256: "inventory-v1".to_string(),
            diagnostics: Vec::new(),
        };
        Self {
            root,
            tool_name,
            tool_path,
            inventory,
        }
    }

    fn write(&self, name: &str, contents: &[u8]) -> PathBuf {
        let path = self.root.path().join(executable_name(name));
        fs::write(&path, contents).unwrap();
        path.canonicalize().unwrap()
    }

    fn intent(&self, mode: ExecutionMode, command: CommandSpec<'_>) -> ExecutionIntent {
        ExecutionIntent::from_command(CommandIntentInput {
            engagement_id: "engagement-1",
            thread_id: "thread-1",
            turn_id: "turn-1",
            tool_call_id: "tool-call-1",
            mode,
            command,
            cwd: self.root.path(),
            search_path: &[],
            inventory: &self.inventory,
            requested_capabilities: &[],
            authorization_deadline: Some(500),
            policy_revision: "policy-v1",
        })
    }
}

fn executable_name(name: &str) -> String {
    if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_string()
    }
}
