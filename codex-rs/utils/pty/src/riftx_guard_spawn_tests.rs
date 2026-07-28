use std::collections::HashMap;
use std::path::Path;
use std::time::Duration;

use codex_riftx_guard::RIFTX_GUARD_WORK_ROOT_ENV;
use tempfile::TempDir;
use tokio::time::timeout;

#[tokio::test]
async fn hardened_guard_env_fail_closes_outside_linux() {
    let work = TempDir::new().expect("work root");
    let env = HashMap::from([(
        RIFTX_GUARD_WORK_ROOT_ENV.to_string(),
        work.path().to_string_lossy().into_owned(),
    )]);
    let program = if Path::new("/bin/true").exists() {
        "/bin/true"
    } else {
        "true"
    };
    let result = timeout(
        Duration::from_secs(5),
        crate::pipe::spawn_process(program, &[], work.path(), &env, &None, &[]),
    )
    .await
    .expect("spawn should finish");
    #[cfg(target_os = "linux")]
    {
        // Landlock/netns may succeed or fail depending on kernel privileges;
        // either outcome is fail-closed-safe as long as spawn does not hang.
        let _ = result;
    }
    #[cfg(not(target_os = "linux"))]
    {
        let err = result.expect_err("Hardened spawn must refuse off Linux");
        assert!(
            err.to_string().contains("RiftX Guard"),
            "unexpected error: {err}"
        );
    }
}
