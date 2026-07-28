# ADR 0003: Linux Headless LLM Secrets

- Status: Accepted
- Date: 2026-07-28
- Decision owners: RiftX maintainers
- Relates to: [ADR 0001](0001-v0.7-local-native-execution.md)

## Context

RiftX 1.0 treats Linux CLI as a formal product entry point. A desktop Secret Service is not always
available on headless hosts, but provider API keys must not fall back to plaintext configuration,
command-line arguments, ordinary files, audit records, or tool subprocess environments.

Desktop already launches `riftxd` with a private length-prefixed stdin frame. Exposing that internal
framing as the headless user contract would be hard to produce safely and would couple operators to
the Desktop sidecar protocol.

## Decision

1. Keyring remains the default and preferred `LlmApiKeySource` on every platform.
2. Linux headless deployments may explicitly use either:
   - an environment-variable source named in `riftx.toml`; or
   - `riftxd --llm-api-key-stdin-json` with redirected or piped stdin.
3. The public stdin payload is a JSON object mapping configured Profile names to API keys. It is
   bounded to 2 MiB, rejects an empty object, rejects unknown Profiles, and may inject only Profiles
   configured with a Keyring source.
4. `riftxd` rejects public stdin mode when stdin is a terminal, so API keys are not echoed during
   interactive entry. Operators should use a trusted secret producer rather than shell literals.
5. The raw input buffer is zeroed after decoding. Keys remain process memory only and are converted
   into per-Profile Runtime specifications without being persisted.
6. Supplying a partial map leaves omitted Keyring Profiles `unconfigured`. Runtime processes remain
   lazy and start only when the selected Profile is first used.
7. The hidden Desktop `--llm-api-key-stdin` length-prefixed frame remains separate and unchanged.
8. Every configured environment-source variable is removed from Agent tool subprocess environments,
   including variables belonging to other Profiles.

## Consequences

- Headless Linux can start without Secret Service while preserving the no-plaintext-file rule.
- A stdin producer must provide valid JSON and close the pipe before daemon startup continues.
- Stdin injection is intentionally one-shot; rotating a key requires a controlled daemon restart.
- Environment mode remains less isolated than stdin because the key exists in the daemon environment,
  but it is explicit and is scrubbed before tool execution.
