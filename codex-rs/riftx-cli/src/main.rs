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
enum Command {
    Create {
        #[arg(long)]
        name: String,
        #[arg(long = "cidr", required = true)]
        cidrs: Vec<String>,
        #[arg(long = "domain")]
        domains: Vec<String>,
        #[arg(long = "port")]
        ports: Vec<u16>,
        #[arg(long, default_value = "recon")]
        profile: String,
    },
    Get {
        id: String,
    },
    Activate {
        id: String,
    },
    Turn {
        id: String,
        input: String,
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
            cidrs,
            domains,
            ports,
            profile,
        } => {
            send(
                &client,
                &cli.token,
                Method::POST,
                format!("{base}/v1/engagements"),
                Some(json!({
                    "name": name,
                    "scope": {"cidrs": cidrs, "domains": domains, "ports": ports},
                    "toolProfile": profile,
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
