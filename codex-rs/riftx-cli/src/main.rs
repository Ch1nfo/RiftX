use anyhow::Context;
use clap::Parser;
use clap::Subcommand;
use codex_riftx_core::RiftxConfig;
use codex_riftx_ipc::DaemonInfo;
use codex_riftx_ipc::IPC_PROTOCOL_VERSION;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use codex_riftx_ipc::LocalIpcResponse;
use futures::StreamExt;
use serde_json::Value;
use serde_json::json;
use std::path::PathBuf;

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
    let config = RiftxConfig::load(&cli.config).await?;
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

#[cfg(test)]
#[path = "main_tests.rs"]
mod tests;
