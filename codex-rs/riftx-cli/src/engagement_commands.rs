use anyhow::Context;
use clap::Args;
use clap::Subcommand;
use codex_riftx_ipc::ApprovalDecision;
use codex_riftx_ipc::ApprovalDecisionParams;
use codex_riftx_ipc::ApprovalKind;
use codex_riftx_ipc::AssessmentObjective;
use codex_riftx_ipc::AuthorizationScope;
use codex_riftx_ipc::AuthorizationWindow;
use codex_riftx_ipc::CreateEngagementParams;
use codex_riftx_ipc::Engagement;
use codex_riftx_ipc::EngagementStatus;
use codex_riftx_ipc::ExecutionMode;
use codex_riftx_ipc::IdentitySelector;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcResponse;
use codex_riftx_ipc::PendingApproval;
use codex_riftx_ipc::Scope;
use codex_riftx_ipc::StructuredSuccessCriterion;
use serde::Serialize;
use serde::de::DeserializeOwned;

use crate::EnvironmentClassArg;
use crate::ExecutionModeArg;

#[derive(Debug, Args)]
pub(crate) struct CreateEngagementArgs {
    #[arg(long)]
    pub(crate) name: String,
    #[arg(long)]
    pub(crate) objective: String,
    #[arg(long = "success-criterion")]
    pub(crate) success_criteria: Vec<String>,
    #[arg(long = "structured-criterion")]
    pub(crate) structured_criteria: Vec<String>,
    #[arg(long = "entry-point")]
    pub(crate) entry_points: Vec<String>,
    #[arg(long = "cidr", required = true)]
    pub(crate) cidrs: Vec<String>,
    #[arg(long = "domain")]
    pub(crate) domains: Vec<String>,
    #[arg(long = "port")]
    pub(crate) ports: Vec<u16>,
    #[arg(long, value_enum)]
    pub(crate) mode: ExecutionModeArg,
    #[arg(long = "llm-profile")]
    pub(crate) llm_profile: Option<String>,
    #[arg(long, value_enum)]
    pub(crate) environment: EnvironmentClassArg,
    #[arg(long = "capability", required = true)]
    pub(crate) capabilities: Vec<String>,
    #[arg(long = "identity-selector")]
    pub(crate) identity_selectors: Vec<String>,
    #[arg(long)]
    pub(crate) starts_at: Option<i64>,
    #[arg(long)]
    pub(crate) expires_at: Option<i64>,
    #[arg(long)]
    pub(crate) confirmation: Option<String>,
    #[arg(long)]
    pub(crate) json: bool,
}

#[derive(Debug, Subcommand)]
pub(crate) enum EngagementCommand {
    Create(Box<CreateEngagementArgs>),
    Get {
        id: String,
        #[arg(long)]
        json: bool,
    },
    List {
        #[arg(long)]
        json: bool,
    },
    Activate {
        id: String,
        #[arg(long)]
        json: bool,
    },
}

#[derive(Debug, Subcommand)]
pub(crate) enum ApprovalCommand {
    List {
        engagement_id: String,
        #[arg(long)]
        json: bool,
    },
    Decide {
        approval_id: String,
        #[arg(value_enum)]
        decision: ApprovalDecisionArg,
        #[arg(long)]
        json: bool,
    },
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
pub(crate) enum ApprovalDecisionArg {
    Approve,
    Deny,
}

impl From<ApprovalDecisionArg> for ApprovalDecision {
    fn from(decision: ApprovalDecisionArg) -> Self {
        match decision {
            ApprovalDecisionArg::Approve => Self::Approve,
            ApprovalDecisionArg::Deny => Self::Deny,
        }
    }
}

pub(crate) async fn execute_engagements(
    client: &LocalIpcClient,
    command: EngagementCommand,
) -> anyhow::Result<()> {
    match command {
        EngagementCommand::Create(args) => create(client, *args).await,
        EngagementCommand::Get { id, json } => get(client, &id, json).await,
        EngagementCommand::List { json } => list(client, json).await,
        EngagementCommand::Activate { id, json } => activate(client, &id, json).await,
    }
}

pub(crate) async fn execute_approvals(
    client: &LocalIpcClient,
    command: ApprovalCommand,
) -> anyhow::Result<()> {
    match command {
        ApprovalCommand::List {
            engagement_id,
            json,
        } => list_approvals(client, &engagement_id, json).await,
        ApprovalCommand::Decide {
            approval_id,
            decision,
            json,
        } => decide(client, &approval_id, decision.into(), json).await,
    }
}

pub(crate) async fn create(
    client: &LocalIpcClient,
    args: CreateEngagementArgs,
) -> anyhow::Result<()> {
    let identities: Vec<IdentitySelector> =
        parse_json_arguments(&args.identity_selectors, "identity selector")?;
    let structured_criteria: Vec<StructuredSuccessCriterion> =
        parse_json_arguments(&args.structured_criteria, "structured criterion")?;
    let cidrs = args
        .cidrs
        .into_iter()
        .map(|cidr| {
            cidr.parse()
                .with_context(|| format!("invalid CIDR: {cidr}"))
        })
        .collect::<anyhow::Result<Vec<_>>>()?;
    let params = CreateEngagementParams {
        name: args.name,
        objective: AssessmentObjective {
            summary: args.objective,
            success_criteria: args.success_criteria,
            structured_criteria,
        },
        entry_points: args.entry_points,
        mode: args.mode.into(),
        llm_profile: args.llm_profile,
        auto_limits: None,
        confirmation: args.confirmation,
        authorization: AuthorizationScope {
            network: Scope {
                cidrs,
                domains: args.domains,
                ports: args.ports,
            },
            identities,
            capabilities: args.capabilities,
            environment: args.environment.into(),
            window: AuthorizationWindow {
                starts_at: args.starts_at,
                expires_at: args.expires_at,
            },
        },
    };
    let engagement: Engagement = decode_success(
        client
            .post_typed("/v1/engagements", &params)
            .await
            .context("create engagement")?,
        "created engagement",
    )
    .await?;
    print_engagement(&engagement, args.json)
}

pub(crate) async fn get(client: &LocalIpcClient, id: &str, json: bool) -> anyhow::Result<()> {
    let engagement: Engagement = decode_success(
        client
            .get(&format!("/v1/engagements/{id}"))
            .await
            .context("get engagement")?,
        "engagement",
    )
    .await?;
    print_engagement(&engagement, json)
}

pub(crate) async fn activate(client: &LocalIpcClient, id: &str, json: bool) -> anyhow::Result<()> {
    let engagement: Engagement = decode_success(
        client
            .post(&format!("/v1/engagements/{id}/activate"))
            .await
            .context("activate engagement")?,
        "activated engagement",
    )
    .await?;
    print_engagement(&engagement, json)
}

async fn list(client: &LocalIpcClient, json: bool) -> anyhow::Result<()> {
    let engagements: Vec<Engagement> = decode_success(
        client
            .get("/v1/engagements")
            .await
            .context("list engagements")?,
        "engagement list",
    )
    .await?;
    if json {
        return print_json(&engagements);
    }
    if engagements.is_empty() {
        println!("No engagements.");
        return Ok(());
    }
    for engagement in engagements {
        println!(
            "{}  {}  {}  {}",
            engagement.id,
            engagement_status(engagement.status),
            execution_mode(engagement.mode),
            engagement.name
        );
    }
    Ok(())
}

async fn list_approvals(
    client: &LocalIpcClient,
    engagement_id: &str,
    json: bool,
) -> anyhow::Result<()> {
    let approvals: Vec<PendingApproval> = decode_success(
        client
            .get(&format!("/v1/engagements/{engagement_id}/approvals"))
            .await
            .context("list approvals")?,
        "approval list",
    )
    .await?;
    if json {
        return print_json(&approvals);
    }
    if approvals.is_empty() {
        println!("No pending approvals.");
        return Ok(());
    }
    for approval in approvals {
        let kind = match approval.kind {
            ApprovalKind::Command => "command",
            ApprovalKind::Tool => "tool",
        };
        println!(
            "{}  {kind}  requested={}",
            approval.id, approval.requested_at
        );
        if let Some(command) = approval.command {
            println!("  command: {command}");
        }
        if let Some(cwd) = approval.cwd {
            println!("  cwd: {cwd}");
        }
        if let Some(reason) = approval.reason {
            println!("  reason: {reason}");
        }
    }
    Ok(())
}

pub(crate) async fn decide(
    client: &LocalIpcClient,
    approval_id: &str,
    decision: ApprovalDecision,
    json: bool,
) -> anyhow::Result<()> {
    let response = client
        .post_typed(
            &format!("/v1/approvals/{approval_id}/decision"),
            &ApprovalDecisionParams { decision },
        )
        .await
        .context("decide approval")?;
    ensure_success(response).await?;
    let decision_name = match decision {
        ApprovalDecision::Approve => "approve",
        ApprovalDecision::Deny => "deny",
    };
    if json {
        print_json(&ApprovalDecisionOutput {
            approval_id,
            decision: decision_name,
        })
    } else {
        println!("Approval {approval_id}: {decision_name}");
        Ok(())
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ApprovalDecisionOutput<'a> {
    approval_id: &'a str,
    decision: &'a str,
}

fn print_engagement(engagement: &Engagement, json: bool) -> anyhow::Result<()> {
    if json {
        return print_json(engagement);
    }
    println!("{}", engagement.name);
    println!("  ID: {}", engagement.id);
    println!("  Status: {}", engagement_status(engagement.status));
    println!("  Mode: {}", execution_mode(engagement.mode));
    println!("  LLM profile: {}", engagement.llm_profile);
    println!("  Objective: {}", engagement.objective.summary);
    Ok(())
}

fn engagement_status(status: EngagementStatus) -> &'static str {
    match status {
        EngagementStatus::Draft => "draft",
        EngagementStatus::Active => "active",
        EngagementStatus::Interrupted => "interrupted",
        EngagementStatus::Expired => "expired",
        EngagementStatus::Completed => "completed",
    }
}

fn execution_mode(mode: ExecutionMode) -> &'static str {
    match mode {
        ExecutionMode::RedTeam => "red-team",
        ExecutionMode::Pentest => "pentest",
        ExecutionMode::Auto => "auto",
    }
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

async fn decode_success<T: DeserializeOwned>(
    response: LocalIpcResponse,
    kind: &str,
) -> anyhow::Result<T> {
    let status = response.status();
    let body = response.bytes().await?;
    anyhow::ensure!(
        status.is_success(),
        "riftxd returned {status}: {}",
        String::from_utf8_lossy(&body)
    );
    serde_json::from_slice(&body).with_context(|| format!("decode {kind}"))
}

async fn ensure_success(response: LocalIpcResponse) -> anyhow::Result<()> {
    let status = response.status();
    let body = response.bytes().await?;
    anyhow::ensure!(
        status.is_success(),
        "riftxd returned {status}: {}",
        String::from_utf8_lossy(&body)
    );
    Ok(())
}

fn print_json(value: &impl Serialize) -> anyhow::Result<()> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
}
