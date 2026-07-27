use anyhow::Context;
use clap::Parser;
use clap::Subcommand;
use codex_riftx_core::RiftxConfig;
use codex_riftx_ipc::ApprovalDecision;
use codex_riftx_ipc::ApprovalDecisionParams;
use codex_riftx_ipc::AssessmentObjective;
use codex_riftx_ipc::AuthorizationScope;
use codex_riftx_ipc::AuthorizationWindow;
use codex_riftx_ipc::CaptureArtifactParams;
use codex_riftx_ipc::ChangeModeParams;
use codex_riftx_ipc::CreateEngagementParams;
use codex_riftx_ipc::DaemonInfo;
use codex_riftx_ipc::EnvironmentClass;
use codex_riftx_ipc::ExecutionMode;
use codex_riftx_ipc::ExtensionDiagnosticLevel;
use codex_riftx_ipc::IPC_PROTOCOL_VERSION;
use codex_riftx_ipc::IdentitySelector;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use codex_riftx_ipc::LocalIpcResponse;
use codex_riftx_ipc::ReportFormat;
use codex_riftx_ipc::Scope;
use codex_riftx_ipc::SkillCatalog;
use codex_riftx_ipc::StartTurnParams;
use codex_riftx_ipc::StructuredSuccessCriterion;
use codex_riftx_ipc::ToolInventory;
use futures::StreamExt;
use serde::Serialize;
use serde::de::DeserializeOwned;
use std::path::PathBuf;
use tokio::io::AsyncWriteExt;

mod credential_commands;
mod llm_commands;

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
    Create {
        #[arg(long)]
        name: String,
        #[arg(long)]
        objective: String,
        #[arg(long = "success-criterion")]
        success_criteria: Vec<String>,
        #[arg(long = "structured-criterion")]
        structured_criteria: Vec<String>,
        #[arg(long = "entry-point")]
        entry_points: Vec<String>,
        #[arg(long = "cidr", required = true)]
        cidrs: Vec<String>,
        #[arg(long = "domain")]
        domains: Vec<String>,
        #[arg(long = "port")]
        ports: Vec<u16>,
        #[arg(long, value_enum)]
        mode: ExecutionModeArg,
        #[arg(long = "llm-profile")]
        llm_profile: Option<String>,
        #[arg(long, value_enum)]
        environment: EnvironmentClassArg,
        #[arg(long = "capability", required = true)]
        capabilities: Vec<String>,
        #[arg(long = "identity-selector")]
        identity_selectors: Vec<String>,
        #[arg(long)]
        starts_at: Option<i64>,
        #[arg(long)]
        expires_at: Option<i64>,
        #[arg(long)]
        confirmation: Option<String>,
    },
    Get {
        id: String,
    },
    Activate {
        id: String,
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
    Tools {
        #[command(subcommand)]
        command: ToolsCommand,
    },
    Skills {
        #[command(subcommand)]
        command: SkillsCommand,
    },
    Artifacts {
        #[command(subcommand)]
        command: ArtifactsCommand,
    },
}

#[derive(Debug, Subcommand)]
enum ToolsCommand {
    Doctor {
        #[arg(long)]
        json: bool,
    },
}

#[derive(Debug, Subcommand)]
enum SkillsCommand {
    Doctor {
        #[arg(long)]
        json: bool,
    },
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

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    let config = RiftxConfig::load_resolved(&cli.config).await?;
    let client = LocalIpcClient::new(LocalIpcEndpoint::new(config.daemon.ipc_dir));
    verify_daemon(&client).await?;
    match cli.command {
        Command::Create {
            name,
            objective,
            success_criteria,
            structured_criteria,
            entry_points,
            cidrs,
            domains,
            ports,
            mode,
            llm_profile,
            environment,
            capabilities,
            identity_selectors,
            starts_at,
            expires_at,
            confirmation,
        } => {
            let identities: Vec<IdentitySelector> =
                parse_json_arguments(&identity_selectors, "identity selector")?;
            let structured_criteria: Vec<StructuredSuccessCriterion> =
                parse_json_arguments(&structured_criteria, "structured criterion")?;
            let cidrs = cidrs
                .into_iter()
                .map(|cidr| {
                    cidr.parse()
                        .with_context(|| format!("invalid CIDR: {cidr}"))
                })
                .collect::<anyhow::Result<Vec<_>>>()?;
            send_typed(
                &client,
                "/v1/engagements",
                &CreateEngagementParams {
                    name,
                    objective: AssessmentObjective {
                        summary: objective,
                        success_criteria,
                        structured_criteria,
                    },
                    entry_points,
                    mode: mode.into(),
                    llm_profile,
                    confirmation,
                    authorization: AuthorizationScope {
                        network: Scope {
                            cidrs,
                            domains,
                            ports,
                        },
                        identities,
                        capabilities,
                        environment: environment.into(),
                        window: AuthorizationWindow {
                            starts_at,
                            expires_at,
                        },
                    },
                },
            )
            .await?;
        }
        Command::Get { id } => {
            send(&client, RequestKind::Get, format!("/v1/engagements/{id}")).await?;
        }
        Command::Activate { id } => {
            send(
                &client,
                RequestKind::Post,
                format!("/v1/engagements/{id}/activate"),
            )
            .await?;
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
            decide(&client, &id, ApprovalDecision::Approve).await?;
        }
        Command::Deny { id } => {
            decide(&client, &id, ApprovalDecision::Deny).await?;
        }
        Command::Interrupt { id } => {
            send(
                &client,
                RequestKind::Post,
                format!("/v1/engagements/{id}/interrupt"),
            )
            .await?;
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
        Command::Tools {
            command: ToolsCommand::Doctor { json },
        } => tools_doctor(&client, json).await?,
        Command::Skills {
            command: SkillsCommand::Doctor { json },
        } => skills_doctor(&client, json).await?,
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

fn parse_json_arguments<T: DeserializeOwned>(
    arguments: &[String],
    kind: &str,
) -> anyhow::Result<Vec<T>> {
    arguments
        .iter()
        .map(|argument| {
            serde_json::from_str::<T>(argument)
                .with_context(|| format!("invalid {kind} JSON: {argument}"))
        })
        .collect()
}

async fn decide(
    client: &LocalIpcClient,
    id: &str,
    decision: ApprovalDecision,
) -> anyhow::Result<()> {
    send_typed(
        client,
        &format!("/v1/approvals/{id}/decision"),
        &ApprovalDecisionParams { decision },
    )
    .await
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

async fn verify_daemon(client: &LocalIpcClient) -> anyhow::Result<()> {
    let response = client
        .get("/v1/system/info")
        .await
        .context("connect to riftxd")?;
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
    Ok(())
}

async fn tools_doctor(client: &LocalIpcClient, json: bool) -> anyhow::Result<()> {
    let response = client
        .post("/v1/tools/doctor")
        .await
        .context("run tool doctor")?;
    let status = response.status();
    let body = response.bytes().await?;
    anyhow::ensure!(
        status.is_success(),
        "riftxd returned {status}: {}",
        String::from_utf8_lossy(&body)
    );
    let inventory: ToolInventory =
        serde_json::from_slice(&body).context("decode tool inventory")?;
    if json {
        println!("{}", serde_json::to_string_pretty(&inventory)?);
    } else {
        println!("Tools: {}", inventory.tools.len());
        println!("PATH entries: {}", inventory.path_entries.len());
        println!("Snapshot: {}", inventory.snapshot_sha256);
        for diagnostic in &inventory.diagnostics {
            let level = match diagnostic.level {
                ExtensionDiagnosticLevel::Info => "INFO",
                ExtensionDiagnosticLevel::Warning => "WARN",
                ExtensionDiagnosticLevel::Error => "ERROR",
            };
            let path = diagnostic
                .path
                .as_ref()
                .map(|path| format!(" {}:", path.display()))
                .unwrap_or_default();
            println!("{level} {}{path} {}", diagnostic.code, diagnostic.message);
        }
    }
    anyhow::ensure!(inventory.is_healthy(), "one or more tool checks failed");
    Ok(())
}

async fn skills_doctor(client: &LocalIpcClient, json: bool) -> anyhow::Result<()> {
    let response = client
        .post("/v1/skills/doctor")
        .await
        .context("run skill doctor")?;
    let status = response.status();
    let body = response.bytes().await?;
    anyhow::ensure!(
        status.is_success(),
        "riftxd returned {status}: {}",
        String::from_utf8_lossy(&body)
    );
    let catalog: SkillCatalog = serde_json::from_slice(&body).context("decode skill catalog")?;
    if json {
        println!("{}", serde_json::to_string_pretty(&catalog)?);
    } else {
        println!("Skills: {}", catalog.skills.len());
        println!("Directory: {}", catalog.root.display());
        println!("Snapshot: {}", catalog.snapshot_sha256);
        for diagnostic in &catalog.diagnostics {
            let level = match diagnostic.level {
                ExtensionDiagnosticLevel::Info => "INFO",
                ExtensionDiagnosticLevel::Warning => "WARN",
                ExtensionDiagnosticLevel::Error => "ERROR",
            };
            let path = diagnostic
                .path
                .as_deref()
                .map(|path| format!(" ({})", path.display()))
                .unwrap_or_default();
            println!("{level} {}{path}: {}", diagnostic.code, diagnostic.message);
        }
    }
    anyhow::ensure!(catalog.is_healthy(), "one or more RiftX skills are invalid");
    Ok(())
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
