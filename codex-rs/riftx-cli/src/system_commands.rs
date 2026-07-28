use crate::exit_codes::CliExitCode;
use crate::exit_codes::WithExitCode;
use crate::json_contract::ConfigValidation;
use crate::json_contract::DoctorReport;
use clap::Subcommand;
use codex_riftx_core::RiftxConfig;
use codex_riftx_ipc::DaemonInfo;
use std::path::Path;

#[derive(Debug, Subcommand)]
pub(crate) enum ConfigCommand {
    Validate {
        #[arg(long)]
        json: bool,
    },
}

pub(crate) async fn execute_config(
    config_path: &Path,
    command: ConfigCommand,
) -> anyhow::Result<()> {
    match command {
        ConfigCommand::Validate { json } => {
            RiftxConfig::load_resolved(config_path)
                .await
                .with_exit_code(CliExitCode::Config)?;
            if json {
                println!(
                    "{}",
                    serde_json::to_string_pretty(&ConfigValidation {
                        ok: true,
                        config: config_path,
                    })?
                );
            } else {
                println!("Configuration valid: {}", config_path.display());
            }
        }
    }
    Ok(())
}

pub(crate) fn print_doctor(
    config_path: &Path,
    daemon: &DaemonInfo,
    json: bool,
) -> anyhow::Result<()> {
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&DoctorReport {
                ok: true,
                config: config_path,
                daemon,
            })?
        );
    } else {
        println!("Configuration: OK ({})", config_path.display());
        println!(
            "Daemon: OK (version {}, protocol {})",
            daemon.daemon_version, daemon.protocol_version
        );
    }
    Ok(())
}
