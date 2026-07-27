use super::*;
#[cfg(target_os = "linux")]
use codex_riftx_guard::GuardExecPolicy;
use pretty_assertions::assert_eq;
use std::io::Read;
use std::path::Path;

const SECRET: &str = "credential-process-secret";
const HELPER_MODE: &str = "RIFTX_CREDENTIAL_HELPER_MODE";
const SECRET_ENV: &str = "RIFTX_TEST_CREDENTIAL";
const FILE_ENV: &str = "RIFTX_TEST_CREDENTIAL_FILE";
const TREE_PID_FILE_ENV: &str = "RIFTX_TEST_TREE_PID_FILE";

#[tokio::test]
async fn stdin_injection_redacts_echoed_secret_and_clears_inherited_environment() {
    let runner = runner();
    let mut request = helper_request(CredentialInjection::Stdin).await;
    request
        .environment
        .insert(HELPER_MODE.to_string(), "stdin".into());
    request
        .environment
        .insert("RIFTX_ALLOWED".to_string(), "present".into());
    let output = runner
        .run(request, secret())
        .await
        .expect("credential process");

    assert_eq!(
        output.termination,
        CredentialProcessTermination::Exited { code: Some(0) }
    );
    let stdout = String::from_utf8(output.stdout).expect("UTF-8 output");
    assert!(stdout.contains("[REDACTED]"));
    assert!(stdout.contains("allowed=present"));
    assert!(stdout.contains("system_path=missing"));
    assert!(!stdout.contains(SECRET));
}

#[tokio::test]
async fn environment_injection_is_redacted_and_cannot_replace_process_controls() {
    let runner = runner();
    let mut request = helper_request(CredentialInjection::Environment {
        variable: SECRET_ENV.to_string(),
    })
    .await;
    request
        .environment
        .insert(HELPER_MODE.to_string(), "environment".into());

    let output = runner
        .run(request, secret())
        .await
        .expect("credential process");
    assert!(
        !output
            .stdout
            .windows(SECRET.len())
            .any(|value| value == SECRET.as_bytes())
    );
    assert!(
        output
            .stdout
            .windows(REDACTED.len())
            .any(|value| value == REDACTED)
    );

    let mut invalid = helper_request(CredentialInjection::Environment {
        variable: "PATH".to_string(),
    })
    .await;
    invalid
        .environment
        .insert(HELPER_MODE.to_string(), "environment".into());
    assert!(matches!(
        runner.run(invalid, secret()).await,
        Err(CredentialProcessError::InvalidEnvironmentVariable)
    ));
}

#[tokio::test]
async fn file_injection_uses_a_temporary_file_and_removes_it_after_exit() {
    let runner = runner();
    let mut request = helper_request(CredentialInjection::FileEnvironment {
        variable: FILE_ENV.to_string(),
    })
    .await;
    request
        .environment
        .insert(HELPER_MODE.to_string(), "file".into());

    let output = runner
        .run(request, secret())
        .await
        .expect("credential process");
    let stdout = String::from_utf8(output.stdout).expect("UTF-8 output");
    let path = stdout
        .lines()
        .find_map(|line| line.strip_prefix("path="))
        .expect("temporary path");

    assert!(stdout.contains("secret=[REDACTED]"));
    assert!(!Path::new(path).exists());
}

#[tokio::test]
async fn runner_rejects_changed_executables_and_secret_in_arguments() {
    let runner = runner();
    let mut request = helper_request(CredentialInjection::Stdin).await;
    request.expected_sha256 = "a".repeat(64);
    assert!(matches!(
        runner.run(request, secret()).await,
        Err(CredentialProcessError::DigestMismatch)
    ));

    let mut request = helper_request(CredentialInjection::Stdin).await;
    request.args.push(SECRET.into());
    assert!(matches!(
        runner.run(request, secret()).await,
        Err(CredentialProcessError::SecretInProcessInput)
    ));
}

#[tokio::test]
async fn timeout_terminates_the_process_and_io_tasks() {
    let runner =
        CredentialProcessRunner::new(Duration::from_millis(50), 1024).expect("short runner");
    let mut request = helper_request(CredentialInjection::Stdin).await;
    request
        .environment
        .insert(HELPER_MODE.to_string(), "timeout".into());

    let output = runner
        .run(request, secret())
        .await
        .expect("timed out process");

    assert_eq!(
        output,
        CredentialProcessOutput {
            termination: CredentialProcessTermination::TimedOut,
            stdout: Vec::new(),
            stderr: Vec::new(),
            stdout_sha256: digest(&[]),
            stderr_sha256: digest(&[]),
            stdout_truncated: true,
            stderr_truncated: true,
        }
    );
}

#[cfg(unix)]
#[tokio::test]
async fn cancellation_terminates_the_descendant_process_tree() {
    let temp = tempfile::tempdir().expect("temp dir");
    let pid_file = temp.path().join("descendant.pid");
    let runner = runner();
    let mut request = helper_request(CredentialInjection::Stdin).await;
    request
        .environment
        .insert(HELPER_MODE.to_string(), "tree_parent".into());
    request
        .environment
        .insert(TREE_PID_FILE_ENV.to_string(), pid_file.as_os_str().into());
    let cancellation = CancellationToken::new();
    let execution_cancellation = cancellation.clone();
    let execution = tokio::spawn(async move {
        runner
            .run_cancellable(request, secret(), execution_cancellation)
            .await
    });

    tokio::time::timeout(Duration::from_secs(5), async {
        while !pid_file.exists() {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("descendant start");
    let descendant_pid = tokio::fs::read_to_string(&pid_file)
        .await
        .expect("read descendant pid")
        .parse::<u32>()
        .expect("descendant pid");
    cancellation.cancel();

    let output = tokio::time::timeout(Duration::from_secs(5), execution)
        .await
        .expect("credential process cancellation")
        .expect("credential process task")
        .expect("credential process output");
    assert_eq!(output.termination, CredentialProcessTermination::Cancelled);
    let terminated = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let alive = std::process::Command::new("/bin/kill")
                .arg("-0")
                .arg(descendant_pid.to_string())
                .status()
                .is_ok_and(|status| status.success());
            if !alive {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .is_ok();
    if !terminated {
        let _ = std::process::Command::new("/bin/kill")
            .arg("-9")
            .arg(descendant_pid.to_string())
            .status();
    }
    assert!(
        terminated,
        "descendant process {descendant_pid} survived cancellation"
    );
}

#[tokio::test]
async fn cancellation_terminates_the_process_and_io_tasks() {
    let runner = runner();
    let mut request = helper_request(CredentialInjection::Stdin).await;
    request
        .environment
        .insert(HELPER_MODE.to_string(), "timeout".into());
    let cancellation = CancellationToken::new();
    cancellation.cancel();

    let output = runner
        .run_cancellable(request, secret(), cancellation)
        .await
        .expect("cancelled process");

    assert_eq!(
        output,
        CredentialProcessOutput {
            termination: CredentialProcessTermination::Cancelled,
            stdout: Vec::new(),
            stderr: Vec::new(),
            stdout_sha256: digest(&[]),
            stderr_sha256: digest(&[]),
            stdout_truncated: true,
            stderr_truncated: true,
        }
    );
}

#[test]
fn credential_process_helper() {
    let Ok(mode) = std::env::var(HELPER_MODE) else {
        return;
    };
    match mode.as_str() {
        "stdin" => {
            let mut secret = String::new();
            std::io::stdin()
                .read_to_string(&mut secret)
                .expect("read stdin");
            println!("secret={secret}");
            println!(
                "allowed={}",
                std::env::var("RIFTX_ALLOWED").unwrap_or_else(|_| "missing".to_string())
            );
            println!(
                "system_path={}",
                std::env::var("PATH").unwrap_or_else(|_| "missing".to_string())
            );
        }
        "environment" => println!(
            "secret={}",
            std::env::var(SECRET_ENV).expect("credential environment")
        ),
        "file" => {
            let path = std::env::var_os(FILE_ENV).expect("credential file environment");
            let secret = std::fs::read_to_string(&path).expect("credential file");
            println!("path={}", PathBuf::from(path).display());
            println!("secret={secret}");
        }
        "timeout" => std::thread::sleep(Duration::from_secs(30)),
        "tree_parent" => {
            let pid_file = std::env::var_os(TREE_PID_FILE_ENV).expect("tree pid file");
            let mut child = std::process::Command::new(
                std::env::current_exe().expect("current test executable"),
            )
            .args([
                "--exact",
                "process::tests::credential_process_helper",
                "--nocapture",
            ])
            .env(HELPER_MODE, "tree_child")
            .env(TREE_PID_FILE_ENV, &pid_file)
            .spawn()
            .expect("spawn descendant");
            while !Path::new(&pid_file).exists() {
                std::thread::sleep(Duration::from_millis(10));
            }
            std::thread::sleep(Duration::from_secs(30));
            let _ = child.kill();
            let _ = child.wait();
        }
        "tree_child" => {
            let pid_file = std::env::var_os(TREE_PID_FILE_ENV).expect("tree pid file");
            std::fs::write(pid_file, std::process::id().to_string()).expect("write descendant pid");
            std::thread::sleep(Duration::from_secs(30));
        }
        _ => panic!("unknown helper mode"),
    }
}

async fn helper_request(injection: CredentialInjection) -> CredentialProcessRequest {
    let program = std::env::current_exe().expect("current test executable");
    CredentialProcessRequest {
        expected_sha256: digest(&tokio::fs::read(&program).await.expect("read executable")),
        program,
        args: vec![
            "--exact".into(),
            "process::tests::credential_process_helper".into(),
            "--nocapture".into(),
        ],
        cwd: std::env::current_dir().expect("current directory"),
        environment: BTreeMap::new(),
        injection,
        guard: None,
    }
}

fn runner() -> CredentialProcessRunner {
    CredentialProcessRunner::new(Duration::from_secs(10), 64 * 1024).expect("runner")
}

fn secret() -> AssessmentSecret {
    AssessmentSecret::new(SECRET.to_string()).expect("secret")
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn hardened_guard_launch_is_applied_or_fails_closed() {
    let runner = runner();
    let work = tempfile::tempdir().expect("work root");
    let mut request = helper_request(CredentialInjection::Stdin).await;
    request
        .environment
        .insert(HELPER_MODE.to_string(), "stdin".into());
    request.cwd = work.path().to_path_buf();
    request.guard =
        Some(GuardExecPolicy::for_tool(work.path(), &request.program).expect("hardened policy"));
    match runner.run(request, secret()).await {
        Ok(output) => assert_eq!(
            output.termination,
            CredentialProcessTermination::Exited { code: Some(0) }
        ),
        Err(CredentialProcessError::Guard(_)) | Err(CredentialProcessError::Spawn(_)) => {}
        Err(error) => panic!("unexpected hardened launch error: {error}"),
    }
}
