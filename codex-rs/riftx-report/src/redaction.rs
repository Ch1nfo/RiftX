use crate::EngagementReport;
use codex_riftx_domain::SuccessPredicate;

const REDACTED: &str = "[REDACTED]";
const REDACTED_PRIVATE_KEY: &str = "[REDACTED_PRIVATE_KEY]";
const REDACTED_URL: &str = "[REDACTED_URL]";

pub(crate) fn redact_report(report: &mut EngagementReport) {
    report.engagement.name = redact_text(&report.engagement.name);
    report.engagement.objective.summary = redact_text(&report.engagement.objective.summary);
    redact_strings(&mut report.engagement.objective.success_criteria);
    for criterion in &mut report.engagement.objective.structured_criteria {
        criterion.description = redact_text(&criterion.description);
        if let SuccessPredicate::AttackPath {
            destination_role,
            access_level,
            ..
        } = &mut criterion.predicate
        {
            *destination_role = redact_text(destination_role);
            *access_level = redact_text(access_level);
        }
    }
    redact_strings(&mut report.engagement.entry_points);
    redact_strings(&mut report.engagement.authorization.network.domains);
    redact_strings(&mut report.engagement.authorization.capabilities);
    for identity in &mut report.engagement.authorization.identities {
        redact_optional(&mut identity.domain);
        redact_optional(&mut identity.tenant);
        redact_optional(&mut identity.account);
    }
    if let Some(profile) = &mut report.llm_profile {
        profile.name = redact_text(&profile.name);
    }
    if let Some(run) = &mut report.auto_run {
        redact_optional(&mut run.current_subgoal);
        redact_strings(&mut run.unavailable_tools);
    }
    redact_strings(&mut report.limitations);
    for asset in &mut report.assets {
        asset.kind = redact_text(&asset.kind);
        asset.value = redact_text(&asset.value);
    }
    for relation in &mut report.asset_relations {
        relation.kind = redact_text(&relation.kind);
    }
    for service in &mut report.services {
        service.transport = redact_text(&service.transport);
        redact_optional(&mut service.name);
        redact_optional(&mut service.version);
    }
    for identity in &mut report.identities {
        identity.kind = redact_text(&identity.kind);
        identity.principal = redact_text(&identity.principal);
        redact_optional(&mut identity.domain);
        redact_optional(&mut identity.tenant);
    }
    for observation in &mut report.observations {
        observation.source = redact_text(&observation.source);
        observation.kind = redact_text(&observation.kind);
        observation.summary = redact_text(&observation.summary);
    }
    for hypothesis in &mut report.hypotheses {
        hypothesis.statement = redact_text(&hypothesis.statement);
    }
    for test_case in &mut report.test_cases {
        test_case.capability = redact_text(&test_case.capability);
        test_case.expected_evidence = redact_text(&test_case.expected_evidence);
    }
    for execution in &mut report.executions {
        execution.runner = redact_text(&execution.runner);
        execution.argv = redact_arguments(&execution.argv);
        execution.cwd = redact_text(&execution.cwd);
        if let Some(tool) = &mut execution.tool {
            tool.requested_name = redact_text(&tool.requested_name);
            redact_optional(&mut tool.resolved_path);
            redact_optional(&mut tool.version);
        }
    }
    for finding in &mut report.findings {
        finding.title = redact_text(&finding.title);
        finding.description = redact_text(&finding.description);
        redact_optional(&mut finding.remediation);
    }
    for evidence in &mut report.evidence {
        evidence.summary = redact_text(&evidence.summary);
    }
    for attack_path in &mut report.attack_paths {
        attack_path.destination_role = redact_text(&attack_path.destination_role);
        attack_path.access_level = redact_text(&attack_path.access_level);
        for hop in &mut attack_path.hops {
            hop.capability = redact_text(&hop.capability);
        }
    }
    for coverage in &mut report.coverage {
        coverage.dimension = redact_text(&coverage.dimension);
    }
    for task in &mut report.tasks {
        task.kind = redact_text(&task.kind);
        redact_optional(&mut task.error);
    }
    for artifact in &mut report.artifacts {
        artifact.path = redact_text(&artifact.path);
        artifact.media_type = redact_text(&artifact.media_type);
    }
    for approval in &mut report.approvals {
        approval.display_argv = redact_arguments(&approval.display_argv);
        redact_optional(&mut approval.cwd);
        redact_strings(&mut approval.executable_names);
    }
    for tool in &mut report.tool_snapshot.tools {
        tool.name = redact_text(&tool.name);
        redact_strings(&mut tool.capabilities);
    }
    for skill in &mut report.skill_snapshot.skills {
        skill.name = redact_text(&skill.name);
    }
}

fn redact_strings(values: &mut [String]) {
    for value in values {
        *value = redact_text(value);
    }
}

fn redact_optional(value: &mut Option<String>) {
    if let Some(value) = value {
        *value = redact_text(value);
    }
}

fn redact_arguments(arguments: &[String]) -> Vec<String> {
    let mut redact_next = false;
    arguments
        .iter()
        .map(|argument| {
            if redact_next {
                redact_next = false;
                return REDACTED.to_string();
            }
            let trimmed = argument
                .trim_matches(|character: char| matches!(character, '-' | '/' | '\'' | '"'));
            if !trimmed.contains(['=', ':']) && is_secret_name(trimmed) {
                redact_next = true;
                return argument.clone();
            }
            redact_text(argument)
        })
        .collect()
}

fn redact_text(value: &str) -> String {
    if contains_private_key(value) {
        return REDACTED_PRIVATE_KEY.to_string();
    }
    let mut redact_next = false;
    let mut output = String::with_capacity(value.len());
    for segment in value.split_inclusive(char::is_whitespace) {
        let token = segment.trim_end_matches(char::is_whitespace);
        let suffix = &segment[token.len()..];
        if redact_next {
            if token.eq_ignore_ascii_case("bearer") {
                output.push_str(token);
            } else {
                output.push_str(REDACTED);
                redact_next = false;
            }
            output.push_str(suffix);
            continue;
        }
        if token.eq_ignore_ascii_case("bearer") {
            output.push_str(token);
            output.push_str(suffix);
            redact_next = true;
            continue;
        }
        if let Some(redacted) = redact_assignment(token) {
            output.push_str(&redacted);
        } else if contains_url_credentials(token) {
            output.push_str(REDACTED_URL);
        } else if is_known_secret_token(token) {
            output.push_str(REDACTED);
        } else {
            output.push_str(token);
            if is_secret_label(token) {
                redact_next = true;
            }
        }
        output.push_str(suffix);
    }
    output
}

fn redact_assignment(token: &str) -> Option<String> {
    for separator in ['=', ':'] {
        if let Some((key, value)) = token.split_once(separator)
            && !value.is_empty()
            && is_secret_name(
                key.trim_matches(|character: char| matches!(character, '-' | '/' | '\'' | '"')),
            )
        {
            return Some(format!("{key}{separator}{REDACTED}"));
        }
    }
    None
}

fn is_secret_label(token: &str) -> bool {
    let trimmed = token.trim_matches(|character: char| matches!(character, '\'' | '"'));
    trimmed
        .strip_suffix(':')
        .is_some_and(|key| is_secret_name(key.trim_start_matches(['-', '/'])))
}

fn contains_private_key(value: &str) -> bool {
    value.contains("-----BEGIN") && value.contains("PRIVATE KEY-----")
}

fn contains_url_credentials(value: &str) -> bool {
    value
        .split_once("://")
        .and_then(|(_, authority)| authority.split_once('@'))
        .is_some_and(|(user_info, _)| user_info.contains(':'))
}

fn is_known_secret_token(value: &str) -> bool {
    let value = value.trim_matches(|character: char| {
        matches!(character, '\'' | '"' | ',' | ';' | ')' | ']' | '}')
    });
    (value.starts_with("sk-") && value.len() >= 20)
        || (value.starts_with("AKIA")
            && value.len() == 20
            && value
                .bytes()
                .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit()))
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
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "cookie",
        "private-key",
        "client-key",
        "access-key",
        "secret-key",
        "aws-secret-access-key",
    ]
    .iter()
    .any(|name| {
        normalized == *name
            || normalized
                .strip_suffix(name)
                .is_some_and(|prefix| prefix.ends_with('-'))
    })
}
