use anyhow::Context;
use clap::Parser;
use clap::Subcommand;
use codex_riftx_core::RiftxConfig;
use codex_riftx_ipc::DaemonInfo;
use codex_riftx_ipc::IPC_PROTOCOL_VERSION;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use codex_riftx_ipc::LocalIpcResponse;
use codex_riftx_skills::SkillCatalog;
use codex_riftx_skills::SkillDiagnosticLevel;
use codex_riftx_tools::DiagnosticLevel;
use codex_riftx_tools::ToolInventory;
use futures::StreamExt;
use serde_json::Value;
use serde_json::json;
use std::path::PathBuf;
use tokio::io::AsyncWriteExt;

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
    },
    Get {
        id: String,
    },
    Activate {
        id: String,
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
        #[arg(long, value_enum, default_value_t = ReportFormat::Markdown)]
        format: ReportFormat,
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
enum ReportFormat {
    Markdown,
    Json,
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
enum ExecutionModeArg {
    Native,
    Hardened,
    Auto,
}

impl ExecutionModeArg {
    fn as_str(self) -> &'static str {
        match self {
            Self::Native => "native",
            Self::Hardened => "hardened",
            Self::Auto => "auto",
        }
    }
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
enum EnvironmentClassArg {
    Lab,
    Staging,
    Production,
}

impl EnvironmentClassArg {
    fn as_str(self) -> &'static str {
        match self {
            Self::Lab => "lab",
            Self::Staging => "staging",
            Self::Production => "production",
        }
    }
}

impl ReportFormat {
    fn as_str(self) -> &'static str {
        match self {
            Self::Markdown => "markdown",
            Self::Json => "json",
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
            environment,
            capabilities,
            identity_selectors,
            starts_at,
            expires_at,
        } => {
            let identity_selectors =
                parse_json_arguments(&identity_selectors, "identity selector")?;
            let structured_criteria =
                parse_json_arguments(&structured_criteria, "structured criterion")?;
            send(
                &client,
                RequestKind::PostJson,
                "/v1/engagements".to_string(),
                Some(json!({
                    "name": name,
                    "objective": {
                        "summary": objective,
                        "successCriteria": success_criteria,
                        "structuredCriteria": structured_criteria,
                    },
                    "entryPoints": entry_points,
                    "mode": mode.as_str(),
                    "authorization": {
                        "network": {"cidrs": cidrs, "domains": domains, "ports": ports},
                        "identities": identity_selectors,
                        "capabilities": capabilities,
                        "environment": environment.as_str(),
                        "window": {"startsAt": starts_at, "expiresAt": expires_at},
                    },
                })),
            )
            .await?;
        }
        Command::Get { id } => {
            send(
                &client,
                RequestKind::Get,
                format!("/v1/engagements/{id}"),
                None,
            )
            .await?;
        }
        Command::Activate { id } => {
            send(
                &client,
                RequestKind::Post,
                format!("/v1/engagements/{id}/activate"),
                None,
            )
            .await?;
        }
        Command::Turn { id, input } => {
            send(
                &client,
                RequestKind::PostJson,
                format!("/v1/engagements/{id}/turns"),
                Some(json!({"input": input})),
            )
            .await?;
        }
        Command::Approve { id } => {
            decide(&client, &id, "approve").await?;
        }
        Command::Deny { id } => {
            decide(&client, &id, "deny").await?;
        }
        Command::Interrupt { id } => {
            send(
                &client,
                RequestKind::Post,
                format!("/v1/engagements/{id}/interrupt"),
                None,
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
            send(
                &client,
                RequestKind::Get,
                format!("/v1/engagements/{id}/report?format={}", format.as_str()),
                None,
            )
            .await?;
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
            send(
                &client,
                RequestKind::PostJson,
                format!("/v1/engagements/{id}/artifacts"),
                Some(json!({
                    "path": path,
                    "mediaType": media_type,
                    "executionId": execution_id,
                })),
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
                None,
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

fn parse_json_arguments(arguments: &[String], kind: &str) -> anyhow::Result<Vec<Value>> {
    arguments
        .iter()
        .map(|argument| {
            serde_json::from_str::<Value>(argument)
                .with_context(|| format!("invalid {kind} JSON: {argument}"))
        })
        .collect()
}

async fn decide(client: &LocalIpcClient, id: &str, decision: &str) -> anyhow::Result<()> {
    send(
        client,
        RequestKind::PostJson,
        format!("/v1/approvals/{id}/decision"),
        Some(json!({"decision": decision})),
    )
    .await
}

async fn send(
    client: &LocalIpcClient,
    kind: RequestKind,
    path: String,
    body: Option<Value>,
) -> anyhow::Result<()> {
    let response = match kind {
        RequestKind::Get => client.get(&path).await,
        RequestKind::Post => client.post(&path).await,
        RequestKind::PostJson => {
            let body = body.context("JSON request body is required")?;
            client.post_json(&path, serde_json::to_vec(&body)?).await
        }
    }
    .context("riftxd request failed")?;
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
    PostJson,
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
        .get("/v1/tools")
        .await
        .context("request tool inventory")?;
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
                DiagnosticLevel::Info => "INFO",
                DiagnosticLevel::Warning => "WARN",
                DiagnosticLevel::Error => "ERROR",
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
        .get("/v1/skills")
        .await
        .context("request skill catalog")?;
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
                SkillDiagnosticLevel::Info => "INFO",
                SkillDiagnosticLevel::Warning => "WARN",
                SkillDiagnosticLevel::Error => "ERROR",
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
