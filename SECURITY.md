# RiftX Security Policy

RiftX is a local, API-key-only agent for authorized security testing. It executes operator-installed
tools on the same computer as `riftxd`; its declared target Scope is a policy and audit boundary, not
an unbypassable operating-system network sandbox.

## Supported Versions

| Version | Security support |
| --- | --- |
| 1.0.x | Supported after 1.0 general availability |
| 1.0 pre-release builds | Best effort; upgrade to the latest commit or release candidate before reporting a regression |
| 0.8.x | Critical security fixes until 90 days after 1.0 general availability |
| Earlier versions | Not supported |

Security fixes may require coordinated Desktop, CLI, and `riftxd` upgrades because incompatible local
IPC versions fail closed rather than negotiate silently.

## Reporting a Vulnerability

Do **not** open a public issue for a suspected vulnerability or include API keys, credentials, target
data, audit logs, reports, or artifacts in a public discussion.

Use [GitHub private vulnerability reporting](https://github.com/Ch1nfo/RiftX/security/advisories/new).
Include, when possible:

- affected commit, tag, operating system, and installation form;
- impact and the trust boundary crossed;
- minimal reproduction steps using harmless local fixtures;
- whether secrets, authorization records, audit data, or artifact integrity are affected;
- suggested mitigations or patches.

If private reporting is unavailable, contact the maintainers through a private channel listed on the
repository owner profile and request a secure disclosure channel. Do not send secrets in the initial
message.

## Response Targets

These are targets, not a bug-bounty promise:

- acknowledge receipt within 3 business days;
- complete initial severity and scope triage within 7 business days;
- provide a status update at least every 14 days while remediation is active;
- coordinate disclosure after a fix or mitigation is available.

Reports involving active secret exposure, unauthorized local IPC access, authorization bypass,
artifact integrity failure, or uncontrolled Auto execution receive priority.

## Security Boundaries

- Tools and Skills are operator-supplied and are not trusted merely because RiftX discovered them.
- RedTeam approvals, execution intents, audit, expiry, Pause, and Kill reduce risk but do not turn a
  malicious local binary into a sandboxed program.
- API keys and assessment credentials must not be placed in issues, test fixtures, command lines,
  plaintext configuration, reports, or ordinary logs.
- Provider endpoints receive the model context required for the selected task. Operators are
  responsible for the provider and for the sensitivity of submitted target data.
- RiftX does not add product telemetry or automatically upload reports and artifacts.

See [RiftX 1.0 Threat Model](architecture/threat-model-1.0.md) for detailed assumptions, mitigations,
and residual risks.
