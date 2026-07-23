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
        "native",
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
    assert!(matches!(mode, ExecutionModeArg::Native));
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
