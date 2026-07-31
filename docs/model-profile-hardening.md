# Model Profile security hardening

This report records the Model Profile safety contract implemented on 2026-07-31. It
covers model configuration, the management API, CLI presentation, local credential
storage, and provider construction. It does not change Runner, Browser, Target HTTP,
Temporal, or WebUI behavior.

## Enforced invariants

1. `provider: openai_compatible` requires a non-empty `base_url` in operator YAML, API
   requests, and CLI configuration. `provider: openai` may continue using the SDK
   default endpoint.
2. `chat_completions` remains the default request mode, and explicit `responses`
   profiles remain supported.
3. `timeout_seconds` must be finite, greater than zero, and at most 600 seconds. YAML,
   Pydantic API schemas, and CLI input enforce the same bound.
4. A Profile with `requires_api_key: false` does not query its credential environment
   variable or local Secret Store. The OpenAI SDK receives the fixed value
   `not-required`, never a real credential. API requests cannot attach a key while this
   flag is false. Credential-looking environment interpolation in its Base URL is also
   rejected before any environment lookup.
5. Pydantic, YAML, and HTTP validation responses omit input values and redact fields
   named like `api_key`, `token`, `password`, or `secret`, including values embedded in
   model-level validation context.
6. `riftx model show` displays `requires_api_key`, `timeout_seconds`, and `max_retries`
   in addition to the existing endpoint and credential metadata. It never displays a
   key.

## Cross-process consistency

Every registry instance derives the same lock file from `models.secrets_path` and holds
an OS-level exclusive lock while reading the metadata/credential pair or committing and
rolling back a mutation. A process-local lock coordinates multiple registry instances
inside one process.

Stored credentials use format version 2 and carry a SHA-256 digest of the exact Profile
metadata. Provider construction passes its already-selected Profile snapshot into the
credential lookup. This produces two defenses:

- cooperative readers never observe the writer's key-first/config-second commit window;
- after a writer crash, stale or mismatched metadata makes the key unavailable, so the
  request fails closed instead of sending a new key to an old endpoint.

Changing `provider` or `base_url` invalidates the stored key unless the mutation also
submits a new key. Changes that keep the same destination, such as model, request mode,
timeout, or retry policy, may safely rebind the stored key to the new Profile digest.

The deterministic regression test opens that exact interleaving window, starts a
separate Python process, proves the reader cannot finish while the writer holds the
shared lock, then confirms it observes only the new endpoint and new key after commit.

## Executable evidence

All commands run in the repository-required Conda environment and start no persistent
service:

```bash
conda run --no-capture-output -n agent pytest -q \
  tests/unit/models/test_config.py \
  tests/unit/models/test_provider.py \
  tests/unit/models/test_provider_wire.py \
  tests/unit/models/test_registry.py \
  tests/unit/models/test_api_schemas.py \
  tests/unit/models/test_service.py
# 73 passed

conda run --no-capture-output -n agent pytest -q \
  tests/unit/cli/test_app.py \
  tests/unit/cli/test_client.py \
  tests/unit/cli/test_i18n.py \
  tests/unit/test_runtime_config.py
# 71 passed

conda run --no-capture-output -n agent pytest -q \
  tests/integration/api/test_control_plane.py::test_model_profile_configuration_is_redacted_and_drives_run_defaults \
  tests/integration/api/test_control_plane.py::test_model_profile_administration_requires_configured_admin_token \
  tests/integration/api/test_control_plane.py::test_model_profile_override_is_effective_and_cannot_be_removed
# 3 passed
```
