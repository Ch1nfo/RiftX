use super::*;
use pretty_assertions::assert_eq;

#[test]
fn create_command_accepts_repeated_scope_arguments() {
    let cli = Cli::try_parse_from([
        "riftx",
        "--token",
        "secret",
        "create",
        "--name",
        "Juice Shop",
        "--objective",
        "Validate exploitable web risks",
        "--entry-point",
        "juice.local",
        "--cidr",
        "10.10.0.0/24",
        "--domain",
        "juice.local",
        "--port",
        "80",
    ])
    .expect("valid CLI");
    let Command::Create {
        objective,
        entry_points,
        cidrs,
        domains,
        ports,
        ..
    } = cli.command
    else {
        panic!("expected create command");
    };
    assert_eq!(objective, "Validate exploitable web risks");
    assert_eq!(entry_points, vec!["juice.local"]);
    assert_eq!(cidrs, vec!["10.10.0.0/24"]);
    assert_eq!(domains, vec!["juice.local"]);
    assert_eq!(ports, vec![80]);
}
