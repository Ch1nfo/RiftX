use anyhow::Context;
use clap::Subcommand;
use codex_riftx_credentials::AssessmentCredentialStore;
use codex_riftx_credentials::AssessmentSecret;
use codex_riftx_credentials::CredentialLocator;
use codex_riftx_ipc::LocalIpcClient;
use serde_json::Value;
use serde_json::json;
use std::io::IsTerminal;
use std::io::Read;

#[derive(Debug, Subcommand)]
pub(crate) enum CredentialCommand {
    Add {
        id: String,
        #[arg(long)]
        label: String,
        #[arg(long, value_enum)]
        kind: CredentialKindArg,
        #[arg(long)]
        username: Option<String>,
        #[arg(long)]
        domain: Option<String>,
        #[arg(long)]
        secret_stdin: bool,
    },
    List {
        id: String,
    },
    Delete {
        id: String,
        credential_id: String,
    },
    Grant {
        id: String,
        credential_id: String,
        #[arg(long = "cidr")]
        cidrs: Vec<String>,
        #[arg(long = "domain")]
        domains: Vec<String>,
        #[arg(long = "port")]
        ports: Vec<u16>,
        #[arg(long = "capability", required = true)]
        capabilities: Vec<String>,
        #[arg(long, default_value_t = 1)]
        max_uses: u32,
        #[arg(long, default_value_t = 3)]
        max_failures_per_identity: u32,
        #[arg(long)]
        starts_at: Option<i64>,
        #[arg(long)]
        expires_at: i64,
    },
    Grants {
        id: String,
    },
    Revoke {
        id: String,
        grant_id: String,
    },
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
pub(crate) enum CredentialKindArg {
    Password,
    ApiToken,
    SshKey,
    Certificate,
    Other,
}

impl CredentialKindArg {
    fn as_str(self) -> &'static str {
        match self {
            Self::Password => "password",
            Self::ApiToken => "apiToken",
            Self::SshKey => "sshKey",
            Self::Certificate => "certificate",
            Self::Other => "other",
        }
    }
}

pub(crate) async fn execute(
    client: &LocalIpcClient,
    command: CredentialCommand,
) -> anyhow::Result<()> {
    match command {
        CredentialCommand::Add {
            id,
            label,
            kind,
            username,
            domain,
            secret_stdin,
        } => {
            let secret = read_secret(secret_stdin).await?;
            let reference = request_json(
                client,
                Request::PostJson {
                    path: format!("/v1/engagements/{id}/credentials"),
                    body: json!({
                        "label": label,
                        "kind": kind.as_str(),
                        "username": username,
                        "domain": domain,
                    }),
                },
            )
            .await?;
            let credential_id = response_id(&reference, "credential")?;
            let locator = CredentialLocator::new(&id, credential_id)?;
            let save_result = tokio::task::spawn_blocking(move || {
                AssessmentCredentialStore::default().save(&locator, secret)
            })
            .await
            .context("credential store task failed")?;
            if let Err(error) = save_result {
                let _ = request_json(
                    client,
                    Request::Post {
                        path: format!("/v1/engagements/{id}/credentials/{credential_id}/delete"),
                    },
                )
                .await;
                return Err(error).context("save assessment credential");
            }
            print_json(&reference)?;
        }
        CredentialCommand::List { id } => {
            print_json(
                &request_json(
                    client,
                    Request::Get {
                        path: format!("/v1/engagements/{id}/credentials"),
                    },
                )
                .await?,
            )?;
        }
        CredentialCommand::Delete { id, credential_id } => {
            let references = request_json(
                client,
                Request::Get {
                    path: format!("/v1/engagements/{id}/credentials"),
                },
            )
            .await?;
            let reference = entity_by_id(&references, &credential_id, "credential")?;
            let grants = request_json(
                client,
                Request::Get {
                    path: format!("/v1/engagements/{id}/credential-grants"),
                },
            )
            .await?;
            let has_grant_history = grants.as_array().is_some_and(|grants| {
                grants.iter().any(|grant| {
                    grant.get("credentialId").and_then(Value::as_str)
                        == Some(credential_id.as_str())
                })
            });
            let locator = CredentialLocator::new(&id, &credential_id)?;
            tokio::task::spawn_blocking(move || {
                AssessmentCredentialStore::default().delete(&locator)
            })
            .await
            .context("credential store task failed")??;
            let reference = if has_grant_history {
                reference
            } else {
                request_json(
                    client,
                    Request::Post {
                        path: format!("/v1/engagements/{id}/credentials/{credential_id}/delete"),
                    },
                )
                .await?
            };
            print_json(&reference)?;
        }
        CredentialCommand::Grant {
            id,
            credential_id,
            cidrs,
            domains,
            ports,
            capabilities,
            max_uses,
            max_failures_per_identity,
            starts_at,
            expires_at,
        } => {
            print_json(
                &request_json(
                    client,
                    Request::PostJson {
                        path: format!("/v1/engagements/{id}/credential-grants"),
                        body: json!({
                            "credentialId": credential_id,
                            "allowedTargets": {
                                "cidrs": cidrs,
                                "domains": domains,
                                "ports": ports,
                            },
                            "allowedCapabilities": capabilities,
                            "maxUses": max_uses,
                            "maxFailuresPerIdentity": max_failures_per_identity,
                            "startsAt": starts_at,
                            "expiresAt": expires_at,
                        }),
                    },
                )
                .await?,
            )?;
        }
        CredentialCommand::Grants { id } => {
            print_json(
                &request_json(
                    client,
                    Request::Get {
                        path: format!("/v1/engagements/{id}/credential-grants"),
                    },
                )
                .await?,
            )?;
        }
        CredentialCommand::Revoke { id, grant_id } => {
            print_json(
                &request_json(
                    client,
                    Request::Post {
                        path: format!("/v1/engagements/{id}/credential-grants/{grant_id}/revoke"),
                    },
                )
                .await?,
            )?;
        }
    }
    Ok(())
}

async fn read_secret(secret_stdin: bool) -> anyhow::Result<AssessmentSecret> {
    let value = if secret_stdin {
        anyhow::ensure!(
            !std::io::stdin().is_terminal(),
            "--secret-stdin requires redirected or piped input"
        );
        tokio::task::spawn_blocking(|| {
            let mut value = String::new();
            std::io::stdin().read_to_string(&mut value)?;
            Ok::<_, std::io::Error>(value)
        })
        .await
        .context("credential input task failed")??
    } else {
        tokio::task::spawn_blocking(|| rpassword::prompt_password("Credential secret: "))
            .await
            .context("credential input task failed")??
    };
    AssessmentSecret::new(value).map_err(Into::into)
}

enum Request {
    Get { path: String },
    Post { path: String },
    PostJson { path: String, body: Value },
}

async fn request_json(client: &LocalIpcClient, request: Request) -> anyhow::Result<Value> {
    let response = match request {
        Request::Get { path } => client.get(&path).await,
        Request::Post { path } => client.post(&path).await,
        Request::PostJson { path, body } => {
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
    serde_json::from_slice(&body).context("decode riftxd credential response")
}

fn response_id<'a>(value: &'a Value, kind: &str) -> anyhow::Result<&'a str> {
    value
        .get("id")
        .and_then(Value::as_str)
        .with_context(|| format!("riftxd returned a credential response without a valid {kind} id"))
}

fn entity_by_id(value: &Value, id: &str, kind: &str) -> anyhow::Result<Value> {
    value
        .as_array()
        .and_then(|entities| {
            entities
                .iter()
                .find(|entity| entity.get("id").and_then(Value::as_str) == Some(id))
        })
        .cloned()
        .with_context(|| format!("{kind} {id:?} was not found"))
}

fn print_json(value: &Value) -> anyhow::Result<()> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
}

#[cfg(test)]
#[path = "credential_commands_tests.rs"]
mod tests;
