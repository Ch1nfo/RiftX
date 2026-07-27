use super::*;
use pretty_assertions::assert_eq;

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
    let Command::Create {
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
    } = cli.command
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
fn tools_doctor_supports_machine_readable_output() {
    let cli = Cli::try_parse_from(["riftx", "tools", "doctor", "--json"]).expect("valid CLI");
    assert!(matches!(
        cli.command,
        Command::Tools {
            command: ToolsCommand::Doctor { json: true }
        }
    ));
}

#[test]
fn skills_doctor_supports_machine_readable_output() {
    let cli = Cli::try_parse_from(["riftx", "skills", "doctor", "--json"]).expect("valid CLI");
    assert!(matches!(
        cli.command,
        Command::Skills {
            command: SkillsCommand::Doctor { json: true }
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
    ])
    .expect("valid CLI");
    assert!(matches!(
        cli.command,
        Command::Mode {
            id,
            mode: ExecutionModeArg::Auto,
            confirmation: Some(confirmation),
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
    ])
    .expect("capture command");
    assert!(matches!(
        capture.command,
        Command::Artifacts {
            command: ArtifactsCommand::Capture {
                id,
                execution_id: Some(execution_id),
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
    ])
    .expect("export command");
    assert!(matches!(
        export.command,
        Command::Artifacts {
            command: ArtifactsCommand::Export {
                id,
                artifact_id,
                output,
            }
        } if id == "eng-1"
            && artifact_id == "artifact-1"
            && output.as_path() == std::path::Path::new("result.json")
    ));
}
