use super::*;
use clap::error::ErrorKind;
use pretty_assertions::assert_eq;

#[test]
fn version_flag_reports_the_release_version() {
    let error = Cli::try_parse_from(["riftx", "--version"]).expect_err("version exits early");

    assert_eq!(error.kind(), ErrorKind::DisplayVersion);
    assert!(error.to_string().contains("1.0.0"));
}

#[test]
fn create_command_accepts_repeated_scope_arguments() {
    let cli = Cli::try_parse_from([
        "riftx",
        "create",
        "--name",
        "Juice Shop",
        "--objective",
        "Validate exploitable web risks",
        "--structured-criterion",
        r#"{"id":"record-evidence","description":"Record evidence","predicate":{"type":"evidence","minimumItems":1,"reproductionRequired":true}}"#,
        "--entry-point",
        "juice.local",
        "--cidr",
        "10.10.0.0/24",
        "--domain",
        "juice.local",
        "--port",
        "80",
        "--mode",
        "pentest",
        "--llm-profile",
        "red-team",
        "--environment",
        "lab",
        "--capability",
        "web.discovery",
    ])
    .expect("valid CLI");
    let Command::Create(engagement_commands::CreateEngagementArgs {
        objective,
        structured_criteria,
        entry_points,
        cidrs,
        domains,
        ports,
        mode,
        llm_profile,
        environment,
        capabilities,
        ..
    }) = cli.command
    else {
        panic!("expected create command");
    };
    assert_eq!(objective, "Validate exploitable web risks");
    assert_eq!(structured_criteria.len(), 1);
    assert_eq!(entry_points, vec!["juice.local"]);
    assert_eq!(cidrs, vec!["10.10.0.0/24"]);
    assert_eq!(domains, vec!["juice.local"]);
    assert_eq!(ports, vec![80]);
    assert!(matches!(mode, ExecutionModeArg::Pentest));
    assert_eq!(llm_profile.as_deref(), Some("red-team"));
    assert!(matches!(environment, EnvironmentClassArg::Lab));
    assert_eq!(capabilities, vec!["web.discovery"]);
}

#[test]
fn engagement_and_approval_namespaces_match_the_m6_surface() {
    let create = Cli::try_parse_from([
        "riftx",
        "engagements",
        "create",
        "--name",
        "Lab",
        "--objective",
        "Assess lab",
        "--cidr",
        "10.0.0.0/24",
        "--mode",
        "pentest",
        "--environment",
        "lab",
        "--capability",
        "web.discovery",
        "--json",
    ])
    .expect("engagement create");
    assert!(matches!(
        create.command,
        Command::Engagements {
            command: engagement_commands::EngagementCommand::Create(args)
        } if args.json
    ));

    let list =
        Cli::try_parse_from(["riftx", "engagements", "list", "--json"]).expect("engagement list");
    assert!(matches!(
        list.command,
        Command::Engagements {
            command: engagement_commands::EngagementCommand::List { json: true }
        }
    ));

    let approvals = Cli::try_parse_from(["riftx", "approvals", "list", "eng-1", "--json"])
        .expect("approval list");
    assert!(matches!(
        approvals.command,
        Command::Approvals {
            command: engagement_commands::ApprovalCommand::List {
                engagement_id,
                json: true,
            }
        } if engagement_id == "eng-1"
    ));

    let decide = Cli::try_parse_from([
        "riftx",
        "approvals",
        "decide",
        "approval-1",
        "approve",
        "--json",
    ])
    .expect("approval decision");
    assert!(matches!(
        decide.command,
        Command::Approvals {
            command: engagement_commands::ApprovalCommand::Decide {
                approval_id,
                decision: engagement_commands::ApprovalDecisionArg::Approve,
                json: true,
            }
        } if approval_id == "approval-1"
    ));
}

#[test]
fn diagnostic_and_discovery_commands_match_the_m6_surface() {
    let doctor = Cli::try_parse_from(["riftx", "doctor", "--json"]).expect("doctor");
    assert!(matches!(doctor.command, Command::Doctor { json: true }));

    let validate =
        Cli::try_parse_from(["riftx", "config", "validate", "--json"]).expect("validate");
    assert!(matches!(
        validate.command,
        Command::Config {
            command: system_commands::ConfigCommand::Validate { json: true }
        }
    ));

    let tools = Cli::try_parse_from(["riftx", "tools", "list", "--json"]).expect("tools");
    assert!(matches!(
        tools.command,
        Command::Tools {
            command: extension_commands::ToolsCommand::List { json: true }
        }
    ));

    let skills = Cli::try_parse_from(["riftx", "skills", "list", "--json"]).expect("skills");
    assert!(matches!(
        skills.command,
        Command::Skills {
            command: extension_commands::SkillsCommand::List { json: true }
        }
    ));

    let kill = Cli::try_parse_from(["riftx", "kill", "--json"]).expect("kill");
    assert!(matches!(kill.command, Command::Kill { json: true }));
}

#[test]
fn tools_doctor_supports_machine_readable_output() {
    let cli = Cli::try_parse_from(["riftx", "tools", "doctor", "--json"]).expect("valid CLI");
    assert!(matches!(
        cli.command,
        Command::Tools {
            command: extension_commands::ToolsCommand::Doctor { json: true }
        }
    ));
}

#[test]
fn skills_doctor_supports_machine_readable_output() {
    let cli = Cli::try_parse_from(["riftx", "skills", "doctor", "--json"]).expect("valid CLI");
    assert!(matches!(
        cli.command,
        Command::Skills {
            command: extension_commands::SkillsCommand::Doctor { json: true }
        }
    ));
}

#[test]
fn llm_profiles_list_and_test_parse() {
    let list = Cli::try_parse_from(["riftx", "llm", "profiles", "list", "--json"]).expect("list");
    assert!(matches!(
        list.command,
        Command::Llm {
            command: llm_commands::LlmCommand::Profiles {
                command: llm_commands::LlmProfilesCommand::List { json: true }
            }
        }
    ));
    let test = Cli::try_parse_from(["riftx", "llm", "profiles", "test", "default", "--json"])
        .expect("test");
    assert!(matches!(
        test.command,
        Command::Llm {
            command: llm_commands::LlmCommand::Profiles {
                command: llm_commands::LlmProfilesCommand::Test {
                    profile,
                    json: true
                }
            }
        } if profile == "default"
    ));
}

#[test]
fn mode_command_carries_the_explicit_auto_confirmation() {
    let cli = Cli::try_parse_from([
        "riftx",
        "mode",
        "eng-1",
        "auto",
        "--confirmation",
        "AUTO MODE - TEST ENVIRONMENT ONLY",
        "--json",
    ])
    .expect("valid CLI");
    assert!(matches!(
        cli.command,
        Command::Mode {
            id,
            mode: ExecutionModeArg::Auto,
            confirmation: Some(confirmation),
            json: true,
        } if id == "eng-1" && confirmation == "AUTO MODE - TEST ENVIRONMENT ONLY"
    ));
}

#[test]
fn artifacts_commands_capture_and_export_workspace_files() {
    let capture = Cli::try_parse_from([
        "riftx",
        "artifacts",
        "capture",
        "eng-1",
        "artifacts/result.json",
        "--execution-id",
        "execution-1",
        "--json",
    ])
    .expect("capture command");
    assert!(matches!(
        capture.command,
        Command::Artifacts {
            command: ArtifactsCommand::Capture {
                id,
                execution_id: Some(execution_id),
                json: true,
                ..
            }
        } if id == "eng-1" && execution_id == "execution-1"
    ));

    let export = Cli::try_parse_from([
        "riftx",
        "artifacts",
        "export",
        "eng-1",
        "artifact-1",
        "--output",
        "result.json",
        "--json",
    ])
    .expect("export command");
    assert!(matches!(
        export.command,
        Command::Artifacts {
            command: ArtifactsCommand::Export {
                id,
                artifact_id,
                output,
                json: true,
            }
        } if id == "eng-1"
            && artifact_id == "artifact-1"
            && output.as_path() == std::path::Path::new("result.json")
    ));
}

#[test]
fn auto_lifecycle_commands_parse_with_engagement_ids() {
    let cases = [
        ("status", "eng-status"),
        ("pause", "eng-pause"),
        ("resume", "eng-resume"),
        ("kill", "eng-kill"),
    ];

    for (operation, expected_id) in cases {
        let cli = Cli::try_parse_from(["riftx", "auto", operation, expected_id, "--json"])
            .expect("valid Auto lifecycle command");
        let Command::Auto { command } = cli.command else {
            panic!("expected Auto command");
        };
        let id = match command {
            AutoCommand::Status { id, json }
            | AutoCommand::Pause { id, json }
            | AutoCommand::Resume { id, json }
            | AutoCommand::Kill { id, json } => {
                assert!(json);
                id
            }
        };
        assert_eq!(id, expected_id);
    }
}

#[test]
fn streaming_and_control_commands_support_json_output() {
    let turn = Cli::try_parse_from(["riftx", "turn", "eng-1", "Run checks", "--json"])
        .expect("turn command");
    assert!(matches!(
        turn.command,
        Command::Turn { id, json: true, .. } if id == "eng-1"
    ));

    let events =
        Cli::try_parse_from(["riftx", "events", "eng-1", "--json"]).expect("events command");
    assert!(matches!(
        events.command,
        Command::Events { id, json: true } if id == "eng-1"
    ));

    let interrupt =
        Cli::try_parse_from(["riftx", "interrupt", "eng-1", "--json"]).expect("interrupt command");
    assert!(matches!(
        interrupt.command,
        Command::Interrupt { id, json: true } if id == "eng-1"
    ));

    let artifacts = Cli::try_parse_from(["riftx", "artifacts", "list", "eng-1", "--json"])
        .expect("artifact list command");
    assert!(matches!(
        artifacts.command,
        Command::Artifacts {
            command: ArtifactsCommand::List { id, json: true }
        } if id == "eng-1"
    ));
}

#[test]
fn runtime_failures_use_stable_exit_codes() {
    use crate::exit_codes::CliExitCode;
    use crate::exit_codes::WithExitCode;

    let config = Err::<(), _>(anyhow::anyhow!("invalid config"))
        .with_exit_code(CliExitCode::Config)
        .expect_err("config error");
    let daemon = Err::<(), _>(anyhow::anyhow!("daemon unavailable"))
        .with_exit_code(CliExitCode::Daemon)
        .expect_err("daemon error");
    let request = Err::<(), _>(anyhow::anyhow!("request rejected"))
        .with_exit_code(CliExitCode::Request)
        .expect_err("request error");
    let io = anyhow::Error::new(std::io::Error::other("disk failure"));
    let internal = anyhow::anyhow!("unexpected failure");

    assert_eq!(
        [
            exit_code_for_error(&config),
            exit_code_for_error(&daemon),
            exit_code_for_error(&request),
            exit_code_for_error(&io),
            exit_code_for_error(&internal),
        ],
        [2, 3, 4, 5, 1]
    );
}
