use super::*;
use codex_utils_path_uri::PathUri;
use pretty_assertions::assert_eq;

#[test]
fn always_policy_reviews_only_executable_terminal_input() {
    let cases = [
        (AskForApproval::Always, "echo hello\n", true),
        (AskForApproval::Always, "", false),
        (AskForApproval::Always, INTERRUPT, false),
        (AskForApproval::OnRequest, "echo hello\n", false),
        (AskForApproval::Never, "echo hello\n", false),
    ];

    assert_eq!(
        cases
            .into_iter()
            .map(|(policy, input, _)| write_stdin_input_needs_policy_review(policy, input))
            .collect::<Vec<_>>(),
        cases
            .into_iter()
            .map(|(_, _, expected)| expected)
            .collect::<Vec<_>>()
    );
}

#[test]
fn approval_command_binds_original_process_session_and_input() {
    let context = WriteStdinApprovalContext {
        command: vec!["/bin/sh".to_string(), "-lc".to_string(), "bash".to_string()],
        cwd: PathUri::parse("file:///workspace").expect("cwd"),
        tty: true,
    };

    assert_eq!(
        write_stdin_approval_command(&context, 42, "nmap 10.10.0.1\n"),
        vec![
            "/bin/sh",
            "-lc",
            "bash",
            "<pty-stdin>",
            "session=42",
            "nmap 10.10.0.1\n",
        ]
    );
}
