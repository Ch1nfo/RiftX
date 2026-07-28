use anyhow::Context;
use clap::Parser;
use clap::Subcommand;
use codex_riftx_core::RiftxConfig;
use codex_riftx_ipc::ApprovalDecision;
use codex_riftx_ipc::CaptureArtifactParams;
use codex_riftx_ipc::ChangeModeParams;
use codex_riftx_ipc::DaemonInfo;
use codex_riftx_ipc::EnvironmentClass;
use codex_riftx_ipc::ExecutionMode;
use codex_riftx_ipc::IPC_PROTOCOL_VERSION;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use codex_riftx_ipc::LocalIpcResponse;
use codex_riftx_ipc::ReportFormat;
use codex_riftx_ipc::StartTurnParams;
use futures::StreamExt;
use serde::Serialize;
use std::ffi::OsString;
use std::path::PathBuf;
use tokio::io::AsyncWriteExt;

mod credential_commands;
mod engagement_commands;
mod extension_commands;
mod llm_commands;
mod system_commands;

#[derive(Debug, Parser)]
#[command(name = "riftx")]
struct Cli {
    #[arg(long, default_value = "riftx.toml")]
    config: PathBuf,
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
#[allow(clippy::large_enum_variant)]
enum Command {
    Doctor {
        #[arg(long)]
        json: bool,
    },
    Config {
        #[command(subcommand)]
        command: system_commands::ConfigCommand,
    },
    Create(engagement_commands::CreateEngagementArgs),
    Get {
        id: String,
    },
    Activate {
        id: String,
    },
    Engagements {
        #[command(subcommand)]
        command: engagement_commands::EngagementCommand,
    },
    Approvals {
        #[command(subcommand)]
        command: engagement_commands::ApprovalCommand,
    },
    Mode {
        id: String,
        #[arg(value_enum)]
        mode: ExecutionModeArg,
        #[arg(long)]
        confirmation: Option<String>,
    },
    Turn {
        id: String,
        input: Option<String>,
    },
    Approve {
        id: String,
    },
    Deny {
        id: String,
    },
    Interrupt {
        id: String,
    },
    Kill,
    Events {
        id: String,
    },
    Report {
        id: String,
        #[arg(long, value_enum, default_value_t = ReportFormatArg::Markdown)]
        format: ReportFormatArg,
    },
    Credentials {
        #[command(subcommand)]
        command: credential_commands::CredentialCommand,
    },
    Llm {
        #[command(subcommand)]
        command: llm_commands::LlmCommand,
    },
    Auto {
        #[command(subcommand)]
        command: AutoCommand,
    },
    Tools {
        #[command(subcommand)]
        command: extension_commands::ToolsCommand,
    },
    Skills {
        #[command(subcommand)]
        command: extension_commands::SkillsCommand,
    },
    Artifacts {
        #[command(subcommand)]
        command: ArtifactsCommand,
    },
}

#[derive(Debug, Subcommand)]
enum AutoCommand {
    Status { id: String },
    Pause { id: String },
    Resume { id: String },
    Kill { id: String },
}

#[derive(Debug, Subcommand)]
enum ArtifactsCommand {
    Capture {
        id: String,
        path: PathBuf,
        #[arg(long)]
        media_type: Option<String>,
        #[arg(long)]
        execution_id: Option<String>,
    },
    List {
        id: String,
    },
    Export {
        id: String,
        artifact_id: String,
        #[arg(long)]
        output: PathBuf,
    },
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
enum ReportFormatArg {
    Markdown,
    Json,
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
enum ExecutionModeArg {
    RedTeam,
    Pentest,
    Auto,
}

impl From<ExecutionModeArg> for ExecutionMode {
    fn from(mode: ExecutionModeArg) -> Self {
        match mode {
            ExecutionModeArg::RedTeam => Self::RedTeam,
            ExecutionModeArg::Pentest => Self::Pentest,
            ExecutionModeArg::Auto => Self::Auto,
        }
    }
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
enum EnvironmentClassArg {
    Lab,
    Staging,
    Production,
}

impl From<EnvironmentClassArg> for EnvironmentClass {
    fn from(environment: EnvironmentClassArg) -> Self {
        match environment {
            EnvironmentClassArg::Lab => Self::Lab,
            EnvironmentClassArg::Staging => Self::Staging,
            EnvironmentClassArg::Production => Self::Production,
        }
    }
}

impl From<ReportFormatArg> for ReportFormat {
    fn from(format: ReportFormatArg) -> Self {
        match format {
            ReportFormatArg::Markdown => Self::Markdown,
            ReportFormatArg::Json => Self::Json,
        }
    }
}

/// Parse CLI arguments and dispatch the requested operation through `riftxd`.
pub async fn run_from<I, T>(args: I) -> anyhow::Result<()>
where
    I: IntoIterator<Item = T>,
    T: Into<OsString> + Clone,
{
    let Cli {
        config: config_path,
        command,
    } = Cli::parse_from(args);
    let command = match command {
        Command::Config { command } => {
            return system_commands::execute_config(&config_path, command).await;
        }
        command => command,
    };
    let config = RiftxConfig::load_resolved(&config_path).await?;
    let client = LocalIpcClient::new(LocalIpcEndpoint::new(config.daemon.ipc_dir));
    let daemon = verify_daemon(&client, &config_path).await?;
    match command {
        Command::Doctor { json } => {
            system_commands::print_doctor(&config_path, &daemon, json)?;
        }
        Command::Config { .. } => unreachable!("config command returned before daemon setup"),
        Command::Create(mut args) => {
            args.json = true;
            engagement_commands::create(&client, args).await?;
        }
        Command::Get { id } => {
            engagement_commands::get(&client, &id, /*json*/ true).await?;
        }
        Command::Activate { id } => {
            engagement_commands::activate(&client, &id, /*json*/ true).await?;
        }
        Command::Engagements { command } => {
            engagement_commands::execute_engagements(&client, command).await?;
        }
        Command::Approvals { command } => {
            engagement_commands::execute_approvals(&client, command).await?;
        }
        Command::Mode {
            id,
            mode,
            confirmation,
        } => {
            send_typed(
                &client,
                &format!("/v1/engagements/{id}/mode"),
                &ChangeModeParams {
                    mode: mode.into(),
                    confirmation,
                },
            )
            .await?;
        }
        Command::Turn { id, input } => {
            send_typed(
                &client,
                &format!("/v1/engagements/{id}/turns"),
                &StartTurnParams { input },
            )
            .await?;
        }
        Command::Approve { id } => {
            engagement_commands::decide(
                &client,
                &id,
                ApprovalDecision::Approve,
                /*json*/ false,
            )
            .await?;
        }
        Command::Deny { id } => {
            engagement_commands::decide(&client, &id, ApprovalDecision::Deny, /*json*/ false)
                .await?;
        }
        Command::Interrupt { id } => {
            send(
                &client,
                RequestKind::Post,
                format!("/v1/engagements/{id}/interrupt"),
            )
            .await?;
        }
        Command::Kill => {
            send(&client, RequestKind::Post, "/v1/system/kill".to_string()).await?;
        }
        Command::Events { id } => {
            let response = client
                .get(&format!("/v1/engagements/{id}/events"))
                .await
                .context("riftxd request failed")?;
            ensure_success(&response)?;
            let mut body = response.into_data_stream();
            while let Some(chunk) = body.next().await {
                print!("{}", String::from_utf8_lossy(&chunk?));
            }
        }
        Command::Report { id, format } => {
            let format = ReportFormat::from(format);
            send(
                &client,
                RequestKind::Get,
                format!("/v1/engagements/{id}/report?format={}", format.as_str()),
            )
            .await?;
        }
        Command::Credentials { command } => {
            credential_commands::execute(&client, command).await?;
        }
        Command::Llm { command } => {
            llm_commands::execute(&client, command).await?;
        }
        Command::Auto { command } => {
            let (request, path) = match command {
                AutoCommand::Status { id } => {
                    (RequestKind::Get, format!("/v1/engagements/{id}/auto"))
                }
                AutoCommand::Pause { id } => (
                    RequestKind::Post,
                    format!("/v1/engagements/{id}/auto/pause"),
                ),
                AutoCommand::Resume { id } => (
                    RequestKind::Post,
                    format!("/v1/engagements/{id}/auto/resume"),
                ),
                AutoCommand::Kill { id } => {
                    (RequestKind::Post, format!("/v1/engagements/{id}/auto/kill"))
                }
            };
            send(&client, request, path).await?;
        }
        Command::Tools { command } => {
            extension_commands::execute_tools(&client, command).await?;
        }
        Command::Skills { command } => {
            extension_commands::execute_skills(&client, command).await?;
        }
        Command::Artifacts {
            command:
                ArtifactsCommand::Capture {
                    id,
                    path,
                    media_type,
                    execution_id,
                },
        } => {
            send_typed(
                &client,
                &format!("/v1/engagements/{id}/artifacts"),
                &CaptureArtifactParams {
                    path,
                    media_type,
                    execution_id,
                },
            )
            .await?;
        }
        Command::Artifacts {
            command: ArtifactsCommand::List { id },
        } => {
            send(
                &client,
                RequestKind::Get,
                format!("/v1/engagements/{id}/artifacts"),
            )
            .await?;
        }
        Command::Artifacts {
            command:
                ArtifactsCommand::Export {
                    id,
                    artifact_id,
                    output,
                },
        } => export_artifact(&client, &id, &artifact_id, &output).await?,
    }
    Ok(())
}

async fn send_typed<T: Serialize + ?Sized>(
    client: &LocalIpcClient,
    path: &str,
    body: &T,
) -> anyhow::Result<()> {
    let response = client
        .post_typed(path, body)
        .await
        .context("riftxd request failed")?;
    print_response(response).await
}

async fn send(client: &LocalIpcClient, kind: RequestKind, path: String) -> anyhow::Result<()> {
    let response = match kind {
        RequestKind::Get => client.get(&path).await,
        RequestKind::Post => client.post(&path).await,
    }
    .context("riftxd request failed")?;
    print_response(response).await
}

async fn print_response(response: LocalIpcResponse) -> anyhow::Result<()> {
    let status = response.status();
    let body = response.bytes().await?;
    anyhow::ensure!(
        status.is_success(),
        "riftxd returned {status}: {}",
        String::from_utf8_lossy(&body)
    );
    if !body.is_empty() {
        println!("{}", String::from_utf8_lossy(&body));
    }
    Ok(())
}

#[derive(Debug, Clone, Copy)]
enum RequestKind {
    Get,
    Post,
}

fn ensure_success(response: &LocalIpcResponse) -> anyhow::Result<()> {
    anyhow::ensure!(
        response.status().is_success(),
        "riftxd returned {}",
        response.status()
    );
    Ok(())
}

async fn verify_daemon(
    client: &LocalIpcClient,
    config_path: &std::path::Path,
) -> anyhow::Result<DaemonInfo> {
    let response = client.get("/v1/system/info").await.with_context(|| {
        format!(
            "riftxd is unavailable; start it with `riftxd --config {}` and retry",
            config_path.display()
        )
    })?;
    let status = response.status();
    let body = response.bytes().await?;
    anyhow::ensure!(
        status.is_success(),
        "riftxd protocol handshake returned {status}"
    );
    let info: DaemonInfo =
        serde_json::from_slice(&body).context("decode riftxd protocol handshake")?;
    anyhow::ensure!(
        info.protocol_version == IPC_PROTOCOL_VERSION,
        "incompatible riftxd protocol: CLI requires {}, daemon provides {}",
        IPC_PROTOCOL_VERSION,
        info.protocol_version
    );
    Ok(info)
}

async fn export_artifact(
    client: &LocalIpcClient,
    engagement_id: &str,
    artifact_id: &str,
    output: &std::path::Path,
) -> anyhow::Result<()> {
    let response = client
        .get(&format!(
            "/v1/engagements/{engagement_id}/artifacts/{artifact_id}/content"
        ))
        .await
        .context("request artifact export")?;
    let status = response.status();
    if !status.is_success() {
        let body = response.bytes().await?;
        anyhow::bail!(
            "riftxd returned {status}: {}",
            String::from_utf8_lossy(&body)
        );
    }
    let mut file = tokio::fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(output)
        .await
        .with_context(|| format!("create artifact export {}", output.display()))?;
    let mut stream = response.into_data_stream();
    let write_result: anyhow::Result<()> = async {
        while let Some(chunk) = stream.next().await {
            file.write_all(&chunk?)
                .await
                .with_context(|| format!("write artifact export {}", output.display()))?;
        }
        file.flush()
            .await
            .with_context(|| format!("flush artifact export {}", output.display()))
    }
    .await;
    if let Err(error) = write_result {
        drop(file);
        let _ = tokio::fs::remove_file(output).await;
        return Err(error);
    }
    println!("{}", output.display());
    Ok(())
}

#[cfg(test)]
#[path = "main_tests.rs"]
mod tests;
