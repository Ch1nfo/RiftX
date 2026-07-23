use anyhow::Context;
use clap::Parser;
use clap::Subcommand;
use futures::StreamExt;
use reqwest::Client;
use reqwest::Method;
use serde_json::Value;
use serde_json::json;

#[derive(Debug, Parser)]
#[command(name = "riftx")]
struct Cli {
    #[arg(
        long,
        env = "RIFTX_GATEWAY_URL",
        default_value = "http://127.0.0.1:8787"
    )]
    gateway: String,
    #[arg(long, env = "RIFTX_OPERATOR_TOKEN", hide_env_values = true)]
    token: String,
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
    anyhow::ensure!(
        !cli.token.is_empty(),
        "operator bearer token cannot be empty"
    );
    let client = Client::new();
    let base = cli.gateway.trim_end_matches('/');
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
                &cli.token,
                Method::POST,
                format!("{base}/v1/engagements"),
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
                &cli.token,
                Method::GET,
                format!("{base}/v1/engagements/{id}"),
                None,
            )
            .await?;
        }
        Command::Activate { id } => {
            send(
                &client,
                &cli.token,
                Method::POST,
                format!("{base}/v1/engagements/{id}/activate"),
                None,
            )
            .await?;
        }
        Command::Turn { id, input } => {
            send(
                &client,
                &cli.token,
                Method::POST,
                format!("{base}/v1/engagements/{id}/turns"),
                Some(json!({"input": input})),
            )
            .await?;
        }
        Command::Approve { id } => {
            decide(&client, &cli.token, base, &id, "approve").await?;
        }
        Command::Deny { id } => {
            decide(&client, &cli.token, base, &id, "deny").await?;
        }
        Command::Interrupt { id } => {
            send(
                &client,
                &cli.token,
                Method::POST,
                format!("{base}/v1/engagements/{id}/interrupt"),
                None,
            )
            .await?;
        }
        Command::Events { id } => {
            let response = client
                .get(format!("{base}/v1/engagements/{id}/events"))
                .bearer_auth(&cli.token)
                .send()
                .await?
                .error_for_status()?;
            let mut body = response.bytes_stream();
            while let Some(chunk) = body.next().await {
                print!("{}", String::from_utf8_lossy(&chunk?));
            }
        }
        Command::Report { id, format } => {
            send(
                &client,
                &cli.token,
                Method::GET,
                format!(
                    "{base}/v1/engagements/{id}/report?format={}",
                    format.as_str()
                ),
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

async fn decide(
    client: &Client,
    token: &str,
    base: &str,
    id: &str,
    decision: &str,
) -> anyhow::Result<()> {
    send(
        client,
        token,
        Method::POST,
        format!("{base}/v1/approvals/{id}/decision"),
        Some(json!({"decision": decision})),
    )
    .await
}

async fn send(
    client: &Client,
    token: &str,
    method: Method,
    url: String,
    body: Option<Value>,
) -> anyhow::Result<()> {
    let mut request = client.request(method, url).bearer_auth(token);
    if let Some(body) = body {
        request = request.json(&body);
    }
    let response = request.send().await.context("Gateway request failed")?;
    let status = response.status();
    let body = response.text().await?;
    anyhow::ensure!(status.is_success(), "Gateway returned {status}: {body}");
    if !body.is_empty() {
        println!("{body}");
    }
    Ok(())
}

#[cfg(test)]
#[path = "main_tests.rs"]
mod tests;
