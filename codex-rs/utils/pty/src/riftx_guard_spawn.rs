//! Optional RiftX Hardened Guard hooks for local process spawn.

use std::collections::HashMap;

use anyhow::Context;
use anyhow::Result;
use codex_riftx_guard::GuardExecPolicy;
use codex_riftx_guard::RIFTX_GUARD_WORK_ROOT_ENV;
use codex_riftx_guard::apply_hardened_launch;
use codex_riftx_guard::apply_hardened_launch_std;

pub(crate) fn guard_requested(env: &HashMap<String, String>) -> bool {
    env.contains_key(RIFTX_GUARD_WORK_ROOT_ENV)
}

pub(crate) fn apply_guard_to_tokio_command(
    command: &mut tokio::process::Command,
    env: &HashMap<String, String>,
    program: &str,
) -> Result<()> {
    if let Some(policy) = GuardExecPolicy::from_spawn_env(env, program)
        .context("invalid RiftX Guard spawn environment")?
    {
        apply_hardened_launch(command, policy)
            .context("Hardened process launch requires an available RiftX Guard")?;
    }
    Ok(())
}

pub(crate) fn apply_guard_to_std_command(
    command: &mut std::process::Command,
    env: &HashMap<String, String>,
    program: &str,
) -> Result<()> {
    if let Some(policy) = GuardExecPolicy::from_spawn_env(env, program)
        .context("invalid RiftX Guard spawn environment")?
    {
        apply_hardened_launch_std(command, policy)
            .context("Hardened process launch requires an available RiftX Guard")?;
    }
    Ok(())
}
