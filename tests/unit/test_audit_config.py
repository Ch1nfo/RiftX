from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from riftx import config as config_module
from riftx.api import APISettings
from riftx.api import runtime as api_runtime
from riftx.config import (
    AuditConfig,
    AuditSourceIngestConfig,
    RiftXConfigError,
    audit_config_digest,
    audit_source_ingest_policy_digest,
    load_riftx_config,
)


def _write_yaml(path: Path, payload: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _load(
    tmp_path: Path,
    *,
    payload: dict[str, object] | None = None,
    environment: dict[str, str] | None = None,
    cli_overrides: dict[str, object] | None = None,
    run_overrides: dict[str, object] | None = None,
):
    explicit_path = None
    if payload is not None:
        explicit_path = tmp_path / "riftx.yaml"
        _write_yaml(explicit_path, payload)
    return load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        explicit_path=explicit_path,
        environment=environment or {},
        cli_overrides=cli_overrides,
        run_overrides=run_overrides,
    )


def _safe_deployment_payload(tmp_path: Path) -> tuple[dict[str, object], Path]:
    source = tmp_path / "authorized-source"
    source.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return (
        {
            "database": {
                "url": f"sqlite+aiosqlite:///{state / 'riftx.db'}",
            },
            "workspace": {"root": str(state / "workspaces")},
            "runner": {
                "state_path": str(state / "runner"),
                "credential_path": str(state / "secrets" / "runner.json"),
            },
            "models": {"secrets_path": str(state / "secrets" / "models.json")},
            "security": {
                "local_principal_path": str(state / "secrets" / "principal.json")
            },
            "audit": {
                "source_roots": [str(source)],
                "snapshot_root": str(state / "audit" / "snapshots"),
                "temp_root": str(state / "audit" / "tmp"),
                "fix_root": str(state / "audit" / "fixes"),
            },
        },
        source,
    )


def _set_nested(target: dict[str, object], path: tuple[str, ...], value: object) -> None:
    current = target
    for item in path[:-1]:
        child = current.setdefault(item, {})
        assert isinstance(child, dict)
        current = child
    current[path[-1]] = value


def _audit_leaf_paths(
    model: type[BaseModel],
    prefix: tuple[str, ...] = ("audit",),
) -> set[tuple[str, ...]]:
    leaves: set[tuple[str, ...]] = set()
    for name, field in model.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            leaves.update(_audit_leaf_paths(annotation, (*prefix, name)))
        else:
            leaves.add((*prefix, name))
    return leaves


def test_audit_defaults_are_backward_compatible_deny_all_and_side_effect_free(
    tmp_path: Path,
) -> None:
    roots = {
        "snapshot_root": tmp_path / "uncreated" / "snapshots",
        "temp_root": tmp_path / "uncreated" / "tmp",
        "fix_root": tmp_path / "uncreated" / "fixes",
    }
    config = _load(
        tmp_path,
        payload={"audit": {name: str(path) for name, path in roots.items()}},
        environment={
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_ADMIN_TOKEN": "test-only-audit-config-admin-token-0001",
        },
    )
    settings = APISettings.from_config(config)

    assert config.audit.enabled is False
    assert config.audit.node_mode == "local_same_node"
    assert config.audit.allowed_node_ids == ("local",)
    assert config.audit.source_roots == ()
    assert Path.cwd() not in config.audit.source_roots
    assert settings.audit == config.audit
    assert settings.audit is not config.audit
    assert all(not path.exists() for path in roots.values())

    assert settings.audit.budget is not config.audit.budget
    with pytest.raises(ValidationError, match="frozen"):
        config.audit.budget.max_model_calls = 1
    assert settings.audit.budget.max_model_calls == 100


def test_example_config_contains_the_complete_safe_audit_defaults(tmp_path: Path) -> None:
    config = load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        explicit_path=Path("configs/riftx.example.yaml"),
        environment={},
    )

    assert config.audit.enabled is False
    assert config.audit.source_roots == ()
    assert config.audit.snapshot_root == Path("/var/lib/riftx/audit/snapshots").resolve()
    assert config.audit.temp_root == Path("/var/lib/riftx/audit/tmp").resolve()
    assert config.audit.fix_root == Path("/var/lib/riftx/audit/fixes").resolve()
    assert config.audit.default_mode == "standard"
    assert config.audit.default_analysis_profile == "deterministic"
    assert config.audit.preflight_token_key_id == "primary"
    assert config.audit.preflight_token_key is None
    assert config.audit.source_ingest.backend_id == "linux_container"
    assert config.audit.source_ingest.runtime == "docker"
    assert config.audit.source_ingest.image_digest is None
    assert config.audit.source_ingest.policy_version == (
        "riftx.audit-source-ingest-policy/v1"
    )
    assert config.audit.model_egress.default_mode == "local_only"
    assert config.audit.model_egress.allow_remote_origins == ()
    assert config.audit.validation.default_policy == "static_only"
    assert config.audit.validation.require_sandbox is True
    assert config.audit.validation.default_network == "none"


def test_every_audit_leaf_has_one_environment_mapping() -> None:
    mapped = set(config_module._AUDIT_ENVIRONMENT_PATHS.values())

    assert mapped == _audit_leaf_paths(AuditConfig)
    assert len(mapped) == len(config_module._AUDIT_ENVIRONMENT_PATHS)


def test_complete_audit_environment_mapping_is_strict_and_reaches_api_settings(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    environment = {
        "RIFTX_DATABASE_URL": f"sqlite+aiosqlite:///{state / 'riftx.db'}",
        "RIFTX_WORKSPACE_ROOT": str(state / "workspaces"),
        "RIFTX_RUNNER_STATE": str(state / "runner"),
        "RIFTX_RUNNER_CREDENTIALS": str(state / "secrets" / "runner.json"),
        "RIFTX_MODEL_SECRETS": str(state / "secrets" / "models.json"),
        "RIFTX_LOCAL_PRINCIPAL_PATH": str(state / "secrets" / "principal.json"),
        "RIFTX_TRUST_PROFILE": "local_single_operator",
        "RIFTX_ADMIN_TOKEN": "test-only-audit-config-admin-token-0001",
        "RIFTX_AUDIT_ENABLED": "true",
        "RIFTX_AUDIT_PREFLIGHT_TOKEN_KEY_ID": "rotation-2026-08",
        "RIFTX_AUDIT_PREFLIGHT_TOKEN_KEY": "A" * 43,
        "RIFTX_AUDIT_SOURCE_ROOTS": json.dumps([str(source)]),
        "RIFTX_AUDIT_SNAPSHOT_ROOT": str(state / "audit" / "snapshots"),
        "RIFTX_AUDIT_TEMP_ROOT": str(state / "audit" / "tmp"),
        "RIFTX_AUDIT_FIX_ROOT": str(state / "audit" / "fixes"),
        "RIFTX_AUDIT_DEFAULT_MODE": "deep",
        "RIFTX_AUDIT_DEFAULT_ANALYSIS_PROFILE": "hybrid",
        "RIFTX_AUDIT_MODEL_EGRESS_DEFAULT_MODE": "remote_redacted",
        "RIFTX_AUDIT_MODEL_EGRESS_MAX_BYTES_PER_CALL": "65536",
        "RIFTX_AUDIT_MODEL_EGRESS_MAX_BYTES_PER_AUDIT": "1000000",
        "RIFTX_AUDIT_MODEL_EGRESS_ALLOW_REMOTE_ORIGINS": json.dumps(
            ["https://MODEL.EXAMPLE:443/"]
        ),
        "RIFTX_AUDIT_MAX_REPOSITORY_BYTES": "1000000",
        "RIFTX_AUDIT_MAX_FILE_BYTES": "100000",
        "RIFTX_AUDIT_MAX_FILES": "1000",
        "RIFTX_AUDIT_MAX_ARTIFACT_BYTES": "1000",
        "RIFTX_AUDIT_MAX_TOTAL_ARTIFACT_BYTES": "2000",
        "RIFTX_AUDIT_WORKERS_MAX_PARALLEL": "2",
        "RIFTX_AUDIT_WORKERS_MAX_EPOCHS": "4",
        "RIFTX_AUDIT_WORKERS_SATURATION_EPOCHS": "1",
        "RIFTX_AUDIT_BUDGET_MAX_WALL_SECONDS": "600",
        "RIFTX_AUDIT_BUDGET_MAX_MODEL_CALLS": "10",
        "RIFTX_AUDIT_BUDGET_MAX_INPUT_TOKENS": "1000",
        "RIFTX_AUDIT_BUDGET_MAX_OUTPUT_TOKENS": "500",
        "RIFTX_AUDIT_BUDGET_MAX_WORKER_JOBS": "8",
        "RIFTX_AUDIT_BUDGET_MAX_CANDIDATES": "50",
        "RIFTX_AUDIT_VALIDATION_DEFAULT_POLICY": "isolated_build",
        "RIFTX_AUDIT_VALIDATION_REQUIRE_SANDBOX": "true",
        "RIFTX_AUDIT_VALIDATION_DEFAULT_NETWORK": "none",
        "RIFTX_AUDIT_VALIDATION_MAX_WALL_SECONDS": "300",
        "RIFTX_AUDIT_VALIDATION_MAX_MEMORY_MIB": "512",
        "RIFTX_AUDIT_VALIDATION_MAX_PIDS": "32",
    }

    config = _load(tmp_path, environment=environment)
    settings = APISettings.from_config(config)
    audit = config.audit

    assert audit.enabled is True
    assert audit.preflight_token_key_id == "rotation-2026-08"
    assert audit.preflight_token_key is not None
    assert audit.preflight_token_key.get_secret_value() == "A" * 43
    assert audit.source_roots == (source.resolve(),)
    assert audit.default_mode == "deep"
    assert audit.default_analysis_profile == "hybrid"
    assert audit.model_egress.default_mode == "remote_redacted"
    assert audit.model_egress.max_bytes_per_call == 65_536
    assert audit.model_egress.max_bytes_per_audit == 1_000_000
    assert audit.model_egress.allow_remote_origins == ("https://model.example",)
    assert audit.max_repository_bytes == 1_000_000
    assert audit.max_file_bytes == 100_000
    assert audit.max_files == 1_000
    assert audit.max_artifact_bytes == 1_000
    assert audit.max_total_artifact_bytes == 2_000
    assert audit.workers.model_dump() == {
        "max_parallel": 2,
        "max_epochs": 4,
        "saturation_epochs": 1,
    }
    assert audit.budget.model_dump() == {
        "max_wall_seconds": 600,
        "max_model_calls": 10,
        "max_input_tokens": 1_000,
        "max_output_tokens": 500,
        "max_worker_jobs": 8,
        "max_candidates": 50,
    }
    assert audit.validation.model_dump() == {
        "default_policy": "isolated_build",
        "require_sandbox": True,
        "default_network": "none",
        "max_wall_seconds": 300,
        "max_memory_mib": 512,
        "max_pids": 32,
    }
    assert settings.audit == audit
    assert str(source.resolve()) not in repr(config)
    assert str(source.resolve()) not in repr(settings)


@pytest.mark.parametrize(
    ("path", "maximum"),
    [
        (("model_egress", "max_bytes_per_call"), 131_072),
        (("model_egress", "max_bytes_per_audit"), 16_777_216),
        (("max_repository_bytes",), 2_147_483_648),
        (("max_file_bytes",), 5_242_880),
        (("max_files",), 200_000),
        (("max_artifact_bytes",), 67_108_864),
        (("max_total_artifact_bytes",), 268_435_456),
        (("workers", "max_parallel"), 4),
        (("workers", "max_epochs"), 8),
        (("workers", "saturation_epochs"), 2),
        (("budget", "max_wall_seconds"), 7_200),
        (("budget", "max_model_calls"), 100),
        (("budget", "max_input_tokens"), 2_000_000),
        (("budget", "max_output_tokens"), 200_000),
        (("budget", "max_worker_jobs"), 64),
        (("budget", "max_candidates"), 1_000),
        (("validation", "max_wall_seconds"), 900),
        (("validation", "max_memory_mib"), 2_048),
        (("validation", "max_pids"), 128),
    ],
)
@pytest.mark.parametrize("invalid", [0, "over_maximum"])
def test_audit_numeric_limits_are_positive_and_cannot_exceed_safe_defaults(
    tmp_path: Path,
    path: tuple[str, ...],
    maximum: int,
    invalid: int | str,
) -> None:
    audit: dict[str, object] = {}
    _set_nested(audit, path, maximum + 1 if invalid == "over_maximum" else invalid)

    with pytest.raises(RiftXConfigError, match="invalid RiftX configuration"):
        _load(tmp_path, payload={"audit": audit})


@pytest.mark.parametrize("invalid", [True, 1.5, "1.0", "+1"])
def test_audit_integer_fields_reject_ambiguous_coercions(
    tmp_path: Path,
    invalid: object,
) -> None:
    with pytest.raises(RiftXConfigError, match="base-10 integer"):
        _load(tmp_path, payload={"audit": {"max_files": invalid}})


@pytest.mark.parametrize("invalid", [1, 0, "yes", "on", ""])
def test_audit_boolean_fields_accept_only_true_or_false(
    tmp_path: Path,
    invalid: object,
) -> None:
    with pytest.raises(RiftXConfigError, match="boolean true or false"):
        _load(tmp_path, payload={"audit": {"enabled": invalid}})


@pytest.mark.parametrize(
    "audit",
    [
        {"default_mode": "quick"},
        {"default_analysis_profile": "remote"},
        {"model_egress": {"default_mode": "unrestricted"}},
        {"validation": {"default_policy": "host_test"}},
        {"validation": {"default_network": "host"}},
        {"unexpected": True},
    ],
)
def test_audit_enums_and_unknown_fields_fail_closed(
    tmp_path: Path,
    audit: dict[str, object],
) -> None:
    with pytest.raises(RiftXConfigError, match="invalid RiftX configuration"):
        _load(tmp_path, payload={"audit": audit})


@pytest.mark.parametrize(
    "audit",
    [
        {"default_mode": "deep"},
        {
            "model_egress": {
                "max_bytes_per_call": 101,
                "max_bytes_per_audit": 100,
            }
        },
        {"max_repository_bytes": 99, "max_file_bytes": 100},
        {"max_artifact_bytes": 100, "max_total_artifact_bytes": 99},
        {"workers": {"max_parallel": 2}, "budget": {"max_worker_jobs": 1}},
        {"workers": {"max_epochs": 1, "saturation_epochs": 2}},
        {
            "budget": {"max_wall_seconds": 899},
            "validation": {"max_wall_seconds": 900},
        },
        {"enabled": True, "validation": {"require_sandbox": False}},
        {"model_egress": {"default_mode": "remote_redacted"}},
    ],
)
def test_audit_cross_field_invariants_fail_closed(
    tmp_path: Path,
    audit: dict[str, object],
) -> None:
    with pytest.raises(RiftXConfigError, match="invalid RiftX configuration"):
        _load(tmp_path, payload={"audit": audit})


@pytest.mark.parametrize(
    "origin",
    [
        "http://model.example",
        "https://user@model.example",
        "https://model.example/path",
        "https://model.example?query=yes",
        "https://model.example#fragment",
        "https://*.model.example",
        "https://model.example:0",
        "https://",
        "https://good.example\\evil.example",
        "https://bad host.example",
        "https://bad\x00host.example",
        "https://bad_host.example",
        "https://-bad.example",
        "https://bad-.example",
        "https://single-label",
        "https://127.1",
    ],
)
def test_remote_model_origin_allowlist_accepts_only_fixed_https_origins(
    tmp_path: Path,
    origin: str,
) -> None:
    with pytest.raises(RiftXConfigError, match="remote model origin"):
        _load(
            tmp_path,
            payload={"audit": {"model_egress": {"allow_remote_origins": [origin]}}},
        )


def test_remote_model_origin_normalizes_standard_dns_and_ip_literals(
    tmp_path: Path,
) -> None:
    config = _load(
        tmp_path,
        payload={
            "audit": {
                "model_egress": {
                    "allow_remote_origins": [
                        "https://MODEL.EXAMPLE.:443/",
                        "https://192.0.2.10:8443",
                        "https://[2001:db8::1]:8443",
                    ]
                }
            }
        },
    )

    assert config.audit.model_egress.allow_remote_origins == (
        "https://192.0.2.10:8443",
        "https://[2001:db8::1]:8443",
        "https://model.example",
    )


@pytest.mark.parametrize("source_kind", ["relative", "missing", "file"])
def test_source_roots_must_be_existing_absolute_directories_without_path_leaks(
    tmp_path: Path,
    source_kind: str,
) -> None:
    canary = tmp_path / "SENSITIVE_SOURCE_ROOT_CANARY"
    if source_kind == "relative":
        source = Path("SENSITIVE_SOURCE_ROOT_CANARY")
    elif source_kind == "file":
        canary.write_text("not a directory", encoding="utf-8")
        source = canary
    else:
        source = canary

    with pytest.raises(RiftXConfigError) as captured:
        _load(tmp_path, payload={"audit": {"source_roots": [str(source)]}})

    assert str(source) not in str(captured.value)
    assert "SENSITIVE_SOURCE_ROOT_CANARY" not in repr(captured.value)


def test_audit_storage_roots_are_absolute_non_overlapping_and_resolvable(
    tmp_path: Path,
) -> None:
    with pytest.raises(RiftXConfigError, match="absolute path"):
        _load(tmp_path, payload={"audit": {"snapshot_root": "relative/snapshots"}})

    with pytest.raises(RiftXConfigError, match="distinct and non-overlapping"):
        _load(
            tmp_path,
            payload={
                "audit": {
                    "snapshot_root": str(tmp_path / "audit"),
                    "temp_root": str(tmp_path / "audit" / "tmp"),
                    "fix_root": str(tmp_path / "fixes"),
                }
            },
        )

    broken = tmp_path / "broken-storage-link"
    broken.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    with pytest.raises(RiftXConfigError, match="unresolved symlink"):
        _load(
            tmp_path,
            payload={"audit": {"snapshot_root": str(broken / "snapshots")}},
        )


@pytest.mark.parametrize(
    "protected",
    [
        "audit.snapshot_root",
        "audit.temp_root",
        "audit.fix_root",
        "workspace.root",
        "runner.state_path",
        "runner.credential_path",
        "models.secrets_path",
        "security.local_principal_path",
        "temporal.tls_server_root_ca_path",
        "temporal.tls_client_cert_path",
        "temporal.tls_client_private_key_path",
        "database.url",
    ],
)
def test_source_root_cannot_overlap_any_protected_storage(
    tmp_path: Path,
    protected: str,
) -> None:
    payload, source = _safe_deployment_payload(tmp_path)
    overlapping = source / "protected"
    if protected == "database.url":
        database = payload["database"]
        assert isinstance(database, dict)
        database["url"] = f"sqlite+aiosqlite:///{overlapping}"
    elif protected.startswith("temporal."):
        temporal = payload.setdefault("temporal", {})
        assert isinstance(temporal, dict)
        temporal["tls_enabled"] = True
        if protected == "temporal.tls_server_root_ca_path":
            temporal["tls_server_root_ca_path"] = str(overlapping)
        else:
            temporal["tls_client_cert_path"] = str(tmp_path / "state" / "client-cert.pem")
            temporal["tls_client_private_key_path"] = str(
                tmp_path / "state" / "client-key.pem"
            )
            temporal[protected.removeprefix("temporal.")] = str(overlapping)
    else:
        _set_nested(payload, tuple(protected.split(".")), str(overlapping))

    with pytest.raises(RiftXConfigError) as captured:
        _load(tmp_path, payload=payload)

    assert "must not overlap protected storage" in str(captured.value)
    assert str(source) not in str(captured.value)


def test_source_storage_overlap_checks_both_directions_and_symlink_aliases(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot-parent"
    source = snapshot / "repository"
    source.mkdir(parents=True)
    state = tmp_path / "state"
    payload, _ = _safe_deployment_payload(tmp_path)
    audit = payload["audit"]
    assert isinstance(audit, dict)
    audit.update(
        {
            "source_roots": [str(source)],
            "snapshot_root": str(snapshot),
            "temp_root": str(state / "audit" / "tmp"),
            "fix_root": str(state / "audit" / "fixes"),
        }
    )
    with pytest.raises(RiftXConfigError, match="must not overlap protected storage"):
        _load(tmp_path, payload=payload)

    real_source = tmp_path / "real-source"
    real_source.mkdir()
    alias = tmp_path / "source-alias"
    alias.symlink_to(real_source, target_is_directory=True)
    payload, _ = _safe_deployment_payload(tmp_path)
    audit = payload["audit"]
    assert isinstance(audit, dict)
    audit["source_roots"] = [str(real_source)]
    audit["snapshot_root"] = str(alias)
    with pytest.raises(RiftXConfigError, match="must not overlap protected storage"):
        _load(tmp_path, payload=payload)


def test_nonexistent_storage_root_nearest_existing_parent_cannot_contain_source(
    tmp_path: Path,
) -> None:
    payload, _ = _safe_deployment_payload(tmp_path)
    shared_parent = tmp_path / "shared-parent"
    source = shared_parent / "repository"
    source.mkdir(parents=True)
    audit = payload["audit"]
    assert isinstance(audit, dict)
    audit["source_roots"] = [str(source)]
    audit["snapshot_root"] = str(shared_parent / "uncreated" / "snapshots")

    with pytest.raises(RiftXConfigError, match="must not overlap protected storage"):
        _load(tmp_path, payload=payload)


@pytest.mark.parametrize("layer", ["cli", "run"])
def test_request_and_cli_override_layers_cannot_change_audit_deployment_policy(
    tmp_path: Path,
    layer: str,
) -> None:
    cli_overrides: dict[str, object] | None = (
        {"audit": {"enabled": True}} if layer == "cli" else None
    )
    run_overrides: dict[str, object] | None = (
        {"audit": {"source_roots": []}} if layer == "run" else None
    )
    with pytest.raises(RiftXConfigError, match="Audit deployment policy cannot be changed"):
        _load(
            tmp_path,
            cli_overrides=cli_overrides,
            run_overrides=run_overrides,
        )


@pytest.mark.parametrize(
    "raw",
    ["", "/tmp/source", "[\"\"]", "{}", "[1]", "not-json"],
)
def test_audit_environment_lists_require_unambiguous_json_arrays(
    tmp_path: Path,
    raw: str,
) -> None:
    with pytest.raises(RiftXConfigError, match="JSON array"):
        _load(tmp_path, environment={"RIFTX_AUDIT_SOURCE_ROOTS": raw})


def test_empty_json_source_root_array_remains_deny_all(tmp_path: Path) -> None:
    config = _load(tmp_path, environment={"RIFTX_AUDIT_SOURCE_ROOTS": "[]"})

    assert config.audit.source_roots == ()


def test_unknown_audit_environment_variable_fails_closed_without_value_leak(
    tmp_path: Path,
) -> None:
    canary = "AUDIT_UNKNOWN_ENV_SECRET_CANARY"
    with pytest.raises(RiftXConfigError) as captured:
        _load(tmp_path, environment={"RIFTX_AUDIT_REGISTRY_TOKEN": canary})

    assert "RIFTX_AUDIT_REGISTRY_TOKEN" in str(captured.value)
    assert canary not in str(captured.value)


def test_audit_config_digest_is_versioned_stable_and_keys_sensitive_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sensitive-source-canary"
    source.mkdir()
    config = AuditConfig(
        source_roots=(source,),
        snapshot_root=tmp_path / "state" / "snapshots",
        temp_root=tmp_path / "state" / "tmp",
        fix_root=tmp_path / "state" / "fixes",
    )
    key = b"k" * 32

    first = audit_config_digest(config, path_digest_key=key)
    second = audit_config_digest(config, path_digest_key=key)
    changed = audit_config_digest(
        AuditConfig(
            source_roots=(source,),
            snapshot_root=tmp_path / "state" / "snapshots",
            temp_root=tmp_path / "state" / "tmp",
            fix_root=tmp_path / "state" / "fixes",
            max_files=199_999,
        ),
        path_digest_key=key,
    )

    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert first != changed
    assert str(source) not in first
    assert first != audit_config_digest(config, path_digest_key=b"z" * 32)
    with pytest.raises(ValueError, match="at least 32 bytes"):
        audit_config_digest(config, path_digest_key=b"short")


def test_source_ingest_config_is_same_node_digest_pinned_and_versioned() -> None:
    image_digest = "a" * 64
    source_ingest = AuditSourceIngestConfig(image_digest=image_digest)

    digest = audit_source_ingest_policy_digest(source_ingest)

    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == audit_source_ingest_policy_digest(source_ingest)
    assert digest != audit_source_ingest_policy_digest(
        AuditSourceIngestConfig(image_digest="b" * 64)
    )
    with pytest.raises(ValidationError):
        AuditSourceIngestConfig(image_digest="latest")
    with pytest.raises(ValidationError):
        AuditConfig(allowed_node_ids=())
    with pytest.raises(ValidationError):
        AuditConfig(allowed_node_ids=("remote",))


@pytest.mark.parametrize(
    "token_key",
    ["short", "A" * 42, "A" * 44, "=" * 43, "é" * 43],
)
def test_preflight_token_key_requires_canonical_256_bit_base64url(
    token_key: str,
) -> None:
    with pytest.raises(ValidationError, match="preflight token key"):
        AuditConfig(preflight_token_key=token_key)


def test_api_runtime_rejects_direct_settings_path_overlap_before_side_effect(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    overlapping_workspace = source / "workspaces"
    settings = APISettings(
        workspace_root=overlapping_workspace,
        runner_state_path=state / "runner",
        runner_credential_path=state / "runner-credentials.json",
        model_secrets_path=state / "model-secrets.json",
        local_principal_path=state / "principal.json",
        database_url=f"sqlite+aiosqlite:///{state / 'riftx.db'}",
        audit=AuditConfig(
            source_roots=(source,),
            snapshot_root=state / "snapshots",
            temp_root=state / "tmp",
            fix_root=state / "fixes",
        ),
    )

    with pytest.raises(RiftXConfigError, match="workspace.root"):
        api_runtime._prepare_local_paths(settings)

    assert not overlapping_workspace.exists()
    assert not settings.runner_state_path.exists()


def test_api_runtime_revalidates_paths_after_directory_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    workspace = state / "workspaces"
    settings = APISettings(
        workspace_root=workspace,
        runner_state_path=state / "runner",
        runner_credential_path=state / "runner-credentials.json",
        model_secrets_path=state / "model-secrets.json",
        local_principal_path=state / "principal.json",
        database_url=f"sqlite+aiosqlite:///{state / 'riftx.db'}",
        audit=AuditConfig(
            source_roots=(source,),
            snapshot_root=state / "snapshots",
            temp_root=state / "tmp",
            fix_root=state / "fixes",
        ),
    )
    original_mkdir = Path.mkdir

    def racing_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if path == workspace:
            path.symlink_to(source, target_is_directory=True)
            return
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    with pytest.raises(RiftXConfigError, match="workspace.root"):
        api_runtime._prepare_local_paths(settings)

    assert workspace.is_symlink()


def test_api_runtime_rejects_source_root_replaced_with_storage_symlink_before_mkdir(
    tmp_path: Path,
) -> None:
    payload, source = _safe_deployment_payload(tmp_path)
    config = _load(
        tmp_path,
        payload=payload,
        environment={
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_ADMIN_TOKEN": "test-only-audit-config-admin-token-0001",
        },
    )
    settings = APISettings.from_config(config)
    state = tmp_path / "state"

    source.rmdir()
    source.symlink_to(state, target_is_directory=True)

    with pytest.raises(RiftXConfigError, match="protected storage"):
        api_runtime._prepare_local_paths(settings)

    assert source.is_symlink()
    assert not settings.workspace_root.exists()
    assert not settings.runner_state_path.exists()


def test_api_runtime_revalidates_source_root_after_first_directory_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, source = _safe_deployment_payload(tmp_path)
    config = _load(
        tmp_path,
        payload=payload,
        environment={
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_ADMIN_TOKEN": "test-only-audit-config-admin-token-0001",
        },
    )
    settings = APISettings.from_config(config)
    state = tmp_path / "state"
    original_mkdir = Path.mkdir
    source_replaced = False

    def racing_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        nonlocal source_replaced
        if not source_replaced and path == settings.workspace_root:
            source.rmdir()
            source.symlink_to(state, target_is_directory=True)
            source_replaced = True
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    with pytest.raises(RiftXConfigError, match="protected storage"):
        api_runtime._prepare_local_paths(settings)

    assert source_replaced is True
    assert source.is_symlink()
    assert settings.workspace_root.is_dir()
