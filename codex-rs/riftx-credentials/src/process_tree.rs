use crate::process::CredentialProcessError;
use crate::process::CredentialProcessTermination;
use std::time::Duration;
use tokio::process::Child;
use tokio::process::Command;

const CANCELLATION_TERMINATION_GRACE_PERIOD: Duration = Duration::from_millis(500);

pub(crate) struct ProcessTreeLauncher {
    #[cfg(windows)]
    job: codex_utils_pty::JobObject,
}

impl ProcessTreeLauncher {
    pub(crate) fn configure(command: &mut Command) -> Result<Self, CredentialProcessError> {
        #[cfg(unix)]
        command.process_group(0);
        #[cfg(windows)]
        let job =
            codex_utils_pty::JobObject::create().map_err(CredentialProcessError::ProcessTree)?;
        Ok(Self {
            #[cfg(windows)]
            job,
        })
    }

    pub(crate) fn attach(self, child: &Child) -> Result<ProcessTree, CredentialProcessError> {
        #[cfg(unix)]
        let process_group_id = child.id().ok_or_else(|| {
            CredentialProcessError::ProcessTree(std::io::Error::other(
                "spawned credential process has no process ID",
            ))
        })?;
        #[cfg(windows)]
        self.job
            .assign_child(child)
            .map_err(CredentialProcessError::ProcessTree)?;
        #[cfg(not(any(unix, windows)))]
        let _ = child;
        Ok(ProcessTree {
            active: true,
            #[cfg(unix)]
            process_group_id,
            #[cfg(windows)]
            job: self.job,
        })
    }
}

pub(crate) struct ProcessTree {
    active: bool,
    #[cfg(unix)]
    process_group_id: u32,
    #[cfg(windows)]
    job: codex_utils_pty::JobObject,
}

impl ProcessTree {
    pub(crate) async fn stop(
        &mut self,
        child: &mut Child,
        termination: CredentialProcessTermination,
    ) -> Result<(), CredentialProcessError> {
        let graceful_result = if termination == CredentialProcessTermination::Cancelled {
            self.terminate()
        } else {
            Ok(false)
        };
        if matches!(graceful_result, Ok(true))
            && let Ok(wait_result) =
                tokio::time::timeout(CANCELLATION_TERMINATION_GRACE_PERIOD, child.wait()).await
        {
            wait_result.map_err(CredentialProcessError::Wait)?;
            self.kill().map_err(CredentialProcessError::ProcessTree)?;
            return Ok(());
        }

        let kill_result = self.kill();
        let _ = child.kill().await;
        let _ = child.wait().await;
        graceful_result.map_err(CredentialProcessError::ProcessTree)?;
        kill_result.map_err(CredentialProcessError::ProcessTree)
    }

    fn terminate(&self) -> std::io::Result<bool> {
        #[cfg(unix)]
        {
            codex_utils_pty::process_group::terminate_process_group(self.process_group_id)
        }
        #[cfg(not(unix))]
        {
            Ok(false)
        }
    }

    pub(crate) fn kill(&mut self) -> std::io::Result<()> {
        if !self.active {
            return Ok(());
        }
        let result = self.kill_active();
        if result.is_ok() {
            self.active = false;
        }
        result
    }

    fn kill_active(&self) -> std::io::Result<()> {
        #[cfg(unix)]
        {
            codex_utils_pty::process_group::kill_process_group(self.process_group_id)
        }
        #[cfg(windows)]
        {
            self.job.terminate()
        }
        #[cfg(not(any(unix, windows)))]
        {
            Ok(())
        }
    }
}

impl Drop for ProcessTree {
    fn drop(&mut self) {
        if self.active {
            let _ = self.kill_active();
        }
    }
}
