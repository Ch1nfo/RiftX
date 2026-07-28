use anyhow::Context;
use clap::Subcommand;
use codex_riftx_ipc::ExtensionDiagnosticLevel;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcResponse;
use codex_riftx_ipc::SkillCatalog;
use codex_riftx_ipc::ToolInventory;

#[derive(Debug, Subcommand)]
pub(crate) enum ToolsCommand {
    List {
        #[arg(long)]
        json: bool,
    },
    Doctor {
        #[arg(long)]
        json: bool,
    },
}

#[derive(Debug, Subcommand)]
pub(crate) enum SkillsCommand {
    List {
        #[arg(long)]
        json: bool,
    },
    Doctor {
        #[arg(long)]
        json: bool,
    },
}

pub(crate) async fn execute_tools(
    client: &LocalIpcClient,
    command: ToolsCommand,
) -> anyhow::Result<()> {
    let (path, json, require_healthy) = match command {
        ToolsCommand::List { json } => ("/v1/tools", json, false),
        ToolsCommand::Doctor { json } => ("/v1/tools/doctor", json, true),
    };
    let response = if require_healthy {
        client.post(path).await.context("run tool doctor")?
    } else {
        client.get(path).await.context("list tools")?
    };
    let inventory: ToolInventory = decode_success(response, "tool inventory").await?;
    if json {
        println!("{}", serde_json::to_string_pretty(&inventory)?);
    } else {
        print_tools(&inventory);
    }
    if require_healthy {
        anyhow::ensure!(inventory.is_healthy(), "one or more tool checks failed");
    }
    Ok(())
}

pub(crate) async fn execute_skills(
    client: &LocalIpcClient,
    command: SkillsCommand,
) -> anyhow::Result<()> {
    let (path, json, require_healthy) = match command {
        SkillsCommand::List { json } => ("/v1/skills", json, false),
        SkillsCommand::Doctor { json } => ("/v1/skills/doctor", json, true),
    };
    let response = if require_healthy {
        client.post(path).await.context("run skill doctor")?
    } else {
        client.get(path).await.context("list skills")?
    };
    let catalog: SkillCatalog = decode_success(response, "skill catalog").await?;
    if json {
        println!("{}", serde_json::to_string_pretty(&catalog)?);
    } else {
        print_skills(&catalog);
    }
    if require_healthy {
        anyhow::ensure!(catalog.is_healthy(), "one or more RiftX skills are invalid");
    }
    Ok(())
}

fn print_tools(inventory: &ToolInventory) {
    println!("Tools: {}", inventory.tools.len());
    println!("PATH entries: {}", inventory.path_entries.len());
    println!("Snapshot: {}", inventory.snapshot_sha256);
    for tool in &inventory.tools {
        println!("  {}  {}", tool.name, tool.path.display());
    }
    for diagnostic in &inventory.diagnostics {
        let level = diagnostic_level(diagnostic.level);
        let path = diagnostic
            .path
            .as_ref()
            .map(|path| format!(" {}:", path.display()))
            .unwrap_or_default();
        println!("{level} {}{path} {}", diagnostic.code, diagnostic.message);
    }
}

fn print_skills(catalog: &SkillCatalog) {
    println!("Skills: {}", catalog.skills.len());
    println!("Directory: {}", catalog.root.display());
    println!("Snapshot: {}", catalog.snapshot_sha256);
    for skill in &catalog.skills {
        println!("  {}  {}", skill.name, skill.path.display());
    }
    for diagnostic in &catalog.diagnostics {
        let level = diagnostic_level(diagnostic.level);
        let path = diagnostic
            .path
            .as_deref()
            .map(|path| format!(" ({})", path.display()))
            .unwrap_or_default();
        println!("{level} {}{path}: {}", diagnostic.code, diagnostic.message);
    }
}

fn diagnostic_level(level: ExtensionDiagnosticLevel) -> &'static str {
    match level {
        ExtensionDiagnosticLevel::Info => "INFO",
        ExtensionDiagnosticLevel::Warning => "WARN",
        ExtensionDiagnosticLevel::Error => "ERROR",
    }
}

async fn decode_success<T: serde::de::DeserializeOwned>(
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
