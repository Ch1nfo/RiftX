use crate::exit_codes::CliExitCode;
use crate::exit_codes::WithExitCode;
use anyhow::Context;
use clap::Subcommand;
use codex_riftx_credentials::AssessmentSecret;
use codex_riftx_ipc::CreateCredentialGrantParams;
use codex_riftx_ipc::CreateCredentialReferenceParams;
use codex_riftx_ipc::CredentialGrant;
use codex_riftx_ipc::CredentialKind;
use codex_riftx_ipc::CredentialReference;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcResponse;
use codex_riftx_ipc::Scope;
use serde::Serialize;
use serde::de::DeserializeOwned;
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

impl From<CredentialKindArg> for CredentialKind {
    fn from(kind: CredentialKindArg) -> Self {
        match kind {
            CredentialKindArg::Password => Self::Password,
            CredentialKindArg::ApiToken => Self::ApiToken,
            CredentialKindArg::SshKey => Self::SshKey,
            CredentialKindArg::Certificate => Self::Certificate,
            CredentialKindArg::Other => Self::Other,
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
            let reference: CredentialReference = post_typed(
                client,
                &format!("/v1/engagements/{id}/credentials"),
                &CreateCredentialReferenceParams {
                    label,
                    kind: kind.into(),
                    username,
                    domain,
                },
            )
            .await?;
            let credential_id = reference.id;
            let configured: CredentialReference = match request_typed(
                client,
                Request::PostBytes {
                    path: format!("/v1/engagements/{id}/credentials/{credential_id}/secret"),
                    body: secret.into_bytes(),
                },
            )
            .await
            {
                Ok(configured) => configured,
                Err(error) => {
                    let _: Result<CredentialReference, _> = request_typed(
                        client,
                        Request::Post {
                            path: format!(
                                "/v1/engagements/{id}/credentials/{credential_id}/delete"
                            ),
                        },
                    )
                    .await;
                    return Err(error).context("save assessment credential through riftxd");
                }
            };
            print_json(&configured)?;
        }
        CredentialCommand::List { id } => {
            let references: Vec<CredentialReference> = request_typed(
                client,
                Request::Get {
                    path: format!("/v1/engagements/{id}/credentials"),
                },
            )
            .await?;
            print_json(&references)?;
        }
        CredentialCommand::Delete { id, credential_id } => {
            let references: Vec<CredentialReference> = request_typed(
                client,
                Request::Get {
                    path: format!("/v1/engagements/{id}/credentials"),
                },
            )
            .await?;
            credential_by_id(&references, &credential_id)?;
            let grants: Vec<CredentialGrant> = request_typed(
                client,
                Request::Get {
                    path: format!("/v1/engagements/{id}/credential-grants"),
                },
            )
            .await?;
            for grant in grants_for_credential(&grants, &credential_id) {
                let _: CredentialGrant = request_typed(
                    client,
                    Request::Post {
                        path: format!("/v1/engagements/{id}/credential-grants/{}/revoke", grant.id),
                    },
                )
                .await?;
            }
            let reference: CredentialReference = request_typed(
                client,
                Request::Post {
                    path: format!("/v1/engagements/{id}/credentials/{credential_id}/delete"),
                },
            )
            .await?;
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
            let cidrs = cidrs
                .into_iter()
                .map(|cidr| {
                    cidr.parse()
                        .with_context(|| format!("invalid CIDR: {cidr}"))
                })
                .collect::<anyhow::Result<Vec<_>>>()?;
            let grant: CredentialGrant = post_typed(
                client,
                &format!("/v1/engagements/{id}/credential-grants"),
                &CreateCredentialGrantParams {
                    credential_id,
                    allowed_targets: Scope {
                        cidrs,
                        domains,
                        ports,
                    },
                    allowed_capabilities: capabilities,
                    max_uses,
                    max_failures_per_identity,
                    starts_at,
                    expires_at,
                },
            )
            .await?;
            print_json(&grant)?;
        }
        CredentialCommand::Grants { id } => {
            let grants: Vec<CredentialGrant> = request_typed(
                client,
                Request::Get {
                    path: format!("/v1/engagements/{id}/credential-grants"),
                },
            )
            .await?;
            print_json(&grants)?;
        }
        CredentialCommand::Revoke { id, grant_id } => {
            let grant: CredentialGrant = request_typed(
                client,
                Request::Post {
                    path: format!("/v1/engagements/{id}/credential-grants/{grant_id}/revoke"),
                },
            )
            .await?;
            print_json(&grant)?;
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
    PostBytes { path: String, body: Vec<u8> },
}

async fn post_typed<RequestBody, ResponseBody>(
    client: &LocalIpcClient,
    path: &str,
    body: &RequestBody,
) -> anyhow::Result<ResponseBody>
where
    RequestBody: Serialize + ?Sized,
    ResponseBody: DeserializeOwned,
{
    let response = client
        .post_typed(path, body)
        .await
        .context("riftxd request failed")
        .with_exit_code(CliExitCode::Request)?;
    decode_response(response)
        .await
        .with_exit_code(CliExitCode::Request)
}

async fn request_typed<T: DeserializeOwned>(
    client: &LocalIpcClient,
    request: Request,
) -> anyhow::Result<T> {
    let response = match request {
        Request::Get { path } => client.get(&path).await,
        Request::Post { path } => client.post(&path).await,
        Request::PostBytes { path, body } => client.post_bytes(&path, body).await,
    }
    .context("riftxd request failed")
    .with_exit_code(CliExitCode::Request)?;
    decode_response(response)
        .await
        .with_exit_code(CliExitCode::Request)
}

async fn decode_response<T: DeserializeOwned>(response: LocalIpcResponse) -> anyhow::Result<T> {
    let status = response.status();
    let body = response.bytes().await?;
    anyhow::ensure!(
        status.is_success(),
        "riftxd returned {status}: {}",
        String::from_utf8_lossy(&body)
    );
    serde_json::from_slice(&body).context("decode riftxd credential response")
}

fn credential_by_id<'a>(
    references: &'a [CredentialReference],
    id: &str,
) -> anyhow::Result<&'a CredentialReference> {
    references
        .iter()
        .find(|reference| reference.id == id)
        .with_context(|| format!("credential {id:?} was not found"))
}

fn grants_for_credential<'a>(
    grants: &'a [CredentialGrant],
    credential_id: &str,
) -> Vec<&'a CredentialGrant> {
    grants
        .iter()
        .filter(|grant| grant.credential_id == credential_id)
        .collect()
}

fn print_json(value: &impl Serialize) -> anyhow::Result<()> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
}

#[cfg(test)]
#[path = "credential_commands_tests.rs"]
mod tests;
