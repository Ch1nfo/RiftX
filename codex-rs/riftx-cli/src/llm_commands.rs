use anyhow::Context;
use clap::Subcommand;
use codex_riftx_ipc::LlmCheckStatus;
use codex_riftx_ipc::LlmConnectionTestResult;
use codex_riftx_ipc::LlmProfileList;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcResponse;

#[derive(Debug, Subcommand)]
pub(crate) enum LlmCommand {
    Profiles {
        #[command(subcommand)]
        command: LlmProfilesCommand,
    },
}

#[derive(Debug, Subcommand)]
pub(crate) enum LlmProfilesCommand {
    List {
        #[arg(long)]
        json: bool,
    },
    Test {
        profile: String,
        #[arg(long)]
        json: bool,
    },
}

pub(crate) async fn execute(client: &LocalIpcClient, command: LlmCommand) -> anyhow::Result<()> {
    match command {
        LlmCommand::Profiles { command } => match command {
            LlmProfilesCommand::List { json } => list_profiles(client, json).await,
            LlmProfilesCommand::Test { profile, json } => {
                test_profile(client, &profile, json).await
            }
        },
    }
}

async fn list_profiles(client: &LocalIpcClient, json: bool) -> anyhow::Result<()> {
    let list: LlmProfileList = decode_success(
        client
            .get("/v1/llm/profiles")
            .await
            .context("list LLM profiles")?,
    )
    .await?;
    if json {
        println!("{}", serde_json::to_string_pretty(&list)?);
        return Ok(());
    }

    println!("Default profile: {}", list.default_profile);
    for profile in &list.profiles {
        let marker = if profile.is_default { "*" } else { " " };
        let key = if profile.configured {
            "key=configured"
        } else {
            "key=missing"
        };
        let runtime = if profile.runtime_ready {
            "runtime=ready"
        } else {
            "runtime=not-ready"
        };
        println!(
            "{marker} {}  protocol={}  model={}  {}  {}",
            profile.name, profile.protocol, profile.model, key, runtime
        );
        println!("    {}", profile.base_url);
    }
    Ok(())
}

async fn test_profile(client: &LocalIpcClient, profile: &str, json: bool) -> anyhow::Result<()> {
    let result: LlmConnectionTestResult = decode_success(
        client
            .post(&format!("/v1/llm/profiles/{profile}/test"))
            .await
            .context("test LLM profile")?,
    )
    .await?;
    if json {
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        println!(
            "Profile {} ({}/{}) {}",
            result.profile_name,
            result.protocol,
            result.model,
            if result.ok { "OK" } else { "FAILED" }
        );
        print_check("config", &result.capabilities.config);
        print_check("stream_text", &result.capabilities.stream_text);
        print_check("function_tools", &result.capabilities.function_tools);
    }
    anyhow::ensure!(result.ok, "LLM profile connection test failed");
    Ok(())
}

fn print_check(name: &str, check: &codex_riftx_ipc::LlmCapabilityCheck) {
    let status = match check.status {
        LlmCheckStatus::Passed => "PASS",
        LlmCheckStatus::Failed => "FAIL",
        LlmCheckStatus::Skipped => "SKIP",
    };
    println!("  [{status}] {name}: {}", check.detail);
}

async fn decode_success<T: serde::de::DeserializeOwned>(
    response: LocalIpcResponse,
) -> anyhow::Result<T> {
    let status = response.status();
    let body = response.bytes().await?;
    anyhow::ensure!(
        status.is_success(),
        "riftxd returned {status}: {}",
        String::from_utf8_lossy(&body)
    );
    serde_json::from_slice(&body).context("decode LLM profile response")
}
