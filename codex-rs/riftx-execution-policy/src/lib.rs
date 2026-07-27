//! Unified pre-spawn execution intent construction and policy decisions for RiftX.

use codex_riftx_domain::ExecutionMode;
use codex_riftx_tools::DiscoveredTool;
use codex_riftx_tools::ToolInventory;
use codex_riftx_tools::ToolRisk;
use codex_shell_command::bash::extract_bash_command;
use codex_shell_command::bash::parse_shell_lc_plain_commands;
use codex_shell_command::bash::parse_shell_script_into_commands;
use codex_shell_command::powershell::extract_powershell_command;
use codex_shell_command::powershell::parse_powershell_command_into_plain_commands;
use sha2::Digest;
use sha2::Sha256;
use std::fs::File;
use std::io::Read;
use std::path::Path;
use std::path::PathBuf;

mod model;

pub use model::*;

impl ExecutionIntent {
    pub fn from_command(input: CommandIntentInput<'_>) -> Self {
        let (argv, split_ok) = argv(input.command);
        let (commands, parse_status) = parsed_commands(input.command, &argv, split_ok);
        let search_path = combined_search_path(input.inventory, input.search_path);
        let executables = commands
            .iter()
            .filter(|command| !command.is_empty())
            .map(|command| executable(command, input.cwd, &search_path, input.inventory))
            .collect::<Vec<_>>();
        let mut requested_capabilities = input.requested_capabilities.to_vec();
        requested_capabilities.extend(
            executables
                .iter()
                .flat_map(|executable| executable.capabilities.iter().cloned()),
        );
        requested_capabilities.sort();
        requested_capabilities.dedup();
        let risk = aggregate_risk(&executables);
        let command_sha256 = digest_arguments(&argv);
        let argument_sha256 = digest_arguments(argv.get(1..).unwrap_or_default());
        let mut intent = Self {
            engagement_id: input.engagement_id.to_string(),
            thread_id: input.thread_id.to_string(),
            turn_id: input.turn_id.to_string(),
            tool_call_id: input.tool_call_id.to_string(),
            mode: input.mode,
            display_argv: redact_argv(&argv),
            command_sha256,
            argument_sha256,
            cwd: input.cwd.to_path_buf(),
            executables,
            tool_inventory_sha256: input.inventory.snapshot_sha256.clone(),
            risk,
            requested_capabilities,
            authorization_deadline: input.authorization_deadline,
            policy_revision: input.policy_revision.to_string(),
            parse_status,
            binding_sha256: String::new(),
        };
        intent.binding_sha256 = intent.binding_digest();
        intent
    }

    fn binding_digest(&self) -> String {
        let mut values = vec![
            self.engagement_id.clone(),
            self.thread_id.clone(),
            self.turn_id.clone(),
            self.tool_call_id.clone(),
            mode_name(self.mode).to_string(),
            self.command_sha256.clone(),
            self.argument_sha256.clone(),
            format!("{:?}", self.cwd.as_os_str()),
            self.tool_inventory_sha256.clone(),
            self.policy_revision.clone(),
            self.authorization_deadline
                .map_or_else(|| "none".to_string(), |deadline| deadline.to_string()),
        ];
        values.extend(self.requested_capabilities.iter().cloned());
        for executable in &self.executables {
            values.push(executable.requested_name.clone());
            values.push(executable.resolved_path.as_deref().map_or_else(
                || "unresolved".to_string(),
                |path| format!("{:?}", path.as_os_str()),
            ));
            values.push(executable.sha256.clone().unwrap_or_default());
        }
        digest_arguments(&values)
    }
}

pub fn decide(intent: &ExecutionIntent, context: DecisionContext<'_>) -> ExecutionDecision {
    let mut deny_reasons = Vec::new();
    if intent
        .authorization_deadline
        .is_some_and(|deadline| context.now >= deadline)
    {
        deny_reasons.push(DecisionReason::AuthorizationExpired);
    }
    for capability in &intent.requested_capabilities {
        if !context.authorized_capabilities.contains(capability) {
            deny_reasons.push(DecisionReason::CapabilityDenied {
                capability: capability.clone(),
            });
        }
    }
    for executable in &intent.executables {
        if executable.inventory_hash_matches == Some(false) {
            deny_reasons.push(DecisionReason::ExecutableHashChanged {
                path: executable.resolved_path.clone().unwrap_or_default(),
            });
        }
    }
    if intent.parse_status == ExecutionParseStatus::Empty {
        deny_reasons.push(DecisionReason::EmptyCommand);
    }
    if !deny_reasons.is_empty() {
        return ExecutionDecision {
            disposition: ExecutionDisposition::Deny,
            reasons: deny_reasons,
        };
    }

    let mut reasons = Vec::new();
    if intent.mode == ExecutionMode::RedTeam {
        reasons.push(DecisionReason::ModeRequiresApproval);
    }
    if intent.parse_status == ExecutionParseStatus::Complex {
        reasons.push(DecisionReason::ComplexCommand);
    }
    if matches!(
        intent.risk,
        ExecutionRisk::Unknown | ExecutionRisk::High | ExecutionRisk::Critical
    ) {
        reasons.push(DecisionReason::RiskRequiresApproval { risk: intent.risk });
    }
    ExecutionDecision {
        disposition: if reasons.is_empty() {
            ExecutionDisposition::Allow
        } else {
            ExecutionDisposition::RequireApproval
        },
        reasons,
    }
}

fn argv(command: CommandSpec<'_>) -> (Vec<String>, bool) {
    match command {
        CommandSpec::CommandLine(command) => shlex::split(command)
            .map_or_else(|| (vec![command.to_string()], false), |argv| (argv, true)),
        CommandSpec::Argv(argv) => (argv.to_vec(), true),
    }
}

fn parsed_commands(
    command: CommandSpec<'_>,
    argv: &[String],
    split_ok: bool,
) -> (Vec<Vec<String>>, ExecutionParseStatus) {
    if argv.is_empty() || argv.iter().all(String::is_empty) {
        return (Vec::new(), ExecutionParseStatus::Empty);
    }
    if !split_ok {
        return (vec![argv.to_vec()], ExecutionParseStatus::Complex);
    }
    let commands = match command {
        CommandSpec::CommandLine(script) => parse_shell_script_into_commands(script),
        CommandSpec::Argv(argv) => Some(vec![argv.to_vec()]),
    };
    expand_shell_commands(commands.unwrap_or_default(), /*depth*/ 0).map_or_else(
        || (vec![argv.to_vec()], ExecutionParseStatus::Complex),
        |commands| (commands, ExecutionParseStatus::Parsed),
    )
}

fn expand_shell_commands(commands: Vec<Vec<String>>, depth: usize) -> Option<Vec<Vec<String>>> {
    if depth >= 8 || commands.is_empty() || commands.len() > 128 {
        return None;
    }
    let mut expanded = Vec::new();
    for command in commands {
        let nested = if extract_bash_command(&command).is_some() {
            parse_shell_lc_plain_commands(&command)
        } else if extract_powershell_command(&command).is_some() {
            parse_powershell_command_into_plain_commands(&command)
        } else if is_unparsed_shell(&command) {
            return None;
        } else {
            expanded.push(command);
            if expanded.len() > 128 {
                return None;
            }
            continue;
        };
        expanded.extend(expand_shell_commands(nested?, depth + 1)?);
        if expanded.len() > 128 {
            return None;
        }
    }
    Some(expanded)
}

fn is_unparsed_shell(argv: &[String]) -> bool {
    let Some(name) = argv
        .first()
        .and_then(|value| Path::new(value).file_name())
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
    else {
        return false;
    };
    matches!(name.as_str(), "cmd" | "cmd.exe" | "dash" | "fish")
        && argv.iter().skip(1).any(|argument| {
            argument.eq_ignore_ascii_case("/c")
                || argument.eq_ignore_ascii_case("-c")
                || argument.eq_ignore_ascii_case("-lc")
        })
}

fn executable(
    command: &[String],
    cwd: &Path,
    search_path: &[PathBuf],
    inventory: &ToolInventory,
) -> ExecutionExecutable {
    let requested_name = command.first().cloned().unwrap_or_default();
    let resolved_path = resolve_path(&requested_name, cwd, search_path);
    let sha256 = resolved_path
        .as_deref()
        .and_then(|path| hash_file(path).ok());
    let managed_tool = resolved_path
        .as_deref()
        .and_then(|path| inventory_tool(path, inventory));
    let (inventory_sha256, inventory_hash_matches, risk, risk_source, capabilities, managed) =
        match managed_tool {
            Some(tool) => {
                let (risk, source) = match tool.metadata.as_ref() {
                    Some(metadata) => metadata
                        .risk
                        .map_or((ExecutionRisk::Unknown, RiskSource::MissingRisk), |risk| {
                            (risk.into(), RiskSource::Declared)
                        }),
                    None => (ExecutionRisk::Unknown, RiskSource::MissingMetadata),
                };
                (
                    Some(tool.sha256.clone()),
                    Some(sha256.as_ref().is_some_and(|sha256| sha256 == &tool.sha256)),
                    risk,
                    source,
                    tool.metadata
                        .as_ref()
                        .map_or_else(Vec::new, |metadata| metadata.capabilities.clone()),
                    true,
                )
            }
            None if resolved_path.is_some() => (
                None,
                None,
                ExecutionRisk::Unknown,
                RiskSource::Unmanaged,
                Vec::new(),
                false,
            ),
            None => (
                None,
                None,
                ExecutionRisk::Unknown,
                RiskSource::Unresolved,
                Vec::new(),
                false,
            ),
        };
    ExecutionExecutable {
        requested_name,
        display_args: redact_argv(command.get(1..).unwrap_or_default()),
        resolved_path,
        sha256,
        inventory_sha256,
        inventory_hash_matches,
        risk,
        risk_source,
        capabilities,
        managed,
    }
}

fn aggregate_risk(executables: &[ExecutionExecutable]) -> ExecutionRisk {
    [
        ExecutionRisk::Critical,
        ExecutionRisk::High,
        ExecutionRisk::Unknown,
        ExecutionRisk::Medium,
        ExecutionRisk::Low,
    ]
    .into_iter()
    .find(|risk| {
        executables
            .iter()
            .any(|executable| executable.risk == *risk)
    })
    .unwrap_or(ExecutionRisk::Unknown)
}

fn combined_search_path(inventory: &ToolInventory, additional: &[PathBuf]) -> Vec<PathBuf> {
    let mut paths = inventory.path_entries.clone();
    paths.extend_from_slice(additional);
    paths
}

fn resolve_path(requested_name: &str, cwd: &Path, search_path: &[PathBuf]) -> Option<PathBuf> {
    let requested = Path::new(requested_name);
    if requested.is_absolute() || requested.components().count() > 1 {
        let candidate = if requested.is_absolute() {
            requested.to_path_buf()
        } else {
            cwd.join(requested)
        };
        return executable_file(candidate);
    }
    search_path.iter().find_map(|directory| {
        executable_candidates(directory.join(requested_name))
            .into_iter()
            .find_map(executable_file)
    })
}

fn executable_candidates(path: PathBuf) -> Vec<PathBuf> {
    #[cfg(windows)]
    {
        if path.extension().is_some() {
            return vec![path];
        }
        return std::env::var("PATHEXT")
            .unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".to_string())
            .split(';')
            .map(|extension| path.with_extension(extension.trim().trim_start_matches('.')))
            .collect();
    }
    #[cfg(not(windows))]
    {
        vec![path]
    }
}

fn executable_file(path: PathBuf) -> Option<PathBuf> {
    path.is_file().then(|| path.canonicalize().ok()).flatten()
}

fn inventory_tool<'a>(path: &Path, inventory: &'a ToolInventory) -> Option<&'a DiscoveredTool> {
    inventory.tools.iter().find(|tool| {
        tool.path == path
            || tool
                .path
                .canonicalize()
                .is_ok_and(|candidate| candidate == path)
    })
}

fn hash_file(path: &Path) -> std::io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            return Ok(hex_digest(hasher.finalize()));
        }
        hasher.update(&buffer[..count]);
    }
}

fn digest_arguments(arguments: &[String]) -> String {
    let mut hasher = Sha256::new();
    for argument in arguments {
        hasher.update(
            u64::try_from(argument.len())
                .unwrap_or(u64::MAX)
                .to_le_bytes(),
        );
        hasher.update(argument.as_bytes());
    }
    hex_digest(hasher.finalize())
}

fn redact_argv(argv: &[String]) -> Vec<String> {
    let mut redact_next = false;
    argv.iter()
        .map(|argument| {
            if redact_next {
                redact_next = false;
                return "[REDACTED]".to_string();
            }
            if let Some((key, _)) = argument.split_once('=')
                && is_secret_name(key)
            {
                return format!("{key}=[REDACTED]");
            }
            if argument.contains("://")
                && argument
                    .split_once("://")
                    .and_then(|(_, authority)| authority.split_once('@'))
                    .is_some_and(|(user_info, _)| user_info.contains(':'))
            {
                return "[REDACTED_URL]".to_string();
            }
            if argument
                .to_ascii_lowercase()
                .trim_start()
                .starts_with("bearer ")
            {
                return "[REDACTED]".to_string();
            }
            if is_secret_name(argument.trim_start_matches('-')) {
                redact_next = true;
            }
            argument.clone()
        })
        .collect()
}

fn is_secret_name(value: &str) -> bool {
    let normalized = value.to_ascii_lowercase().replace('_', "-");
    [
        "password",
        "passwd",
        "passphrase",
        "token",
        "secret",
        "api-key",
        "authorization",
        "credential",
        "cookie",
    ]
    .iter()
    .any(|marker| normalized.contains(marker))
}

fn mode_name(mode: ExecutionMode) -> &'static str {
    match mode {
        ExecutionMode::RedTeam => "redTeam",
        ExecutionMode::Pentest => "pentest",
        ExecutionMode::Auto => "auto",
    }
}

impl From<ToolRisk> for ExecutionRisk {
    fn from(value: ToolRisk) -> Self {
        match value {
            ToolRisk::Low => Self::Low,
            ToolRisk::Medium => Self::Medium,
            ToolRisk::High => Self::High,
            ToolRisk::Critical => Self::Critical,
        }
    }
}

fn hex_digest(bytes: impl AsRef<[u8]>) -> String {
    bytes
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
#[path = "execution_policy_tests.rs"]
mod tests;
