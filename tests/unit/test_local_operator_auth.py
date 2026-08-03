from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import riftx.security as security_module
from riftx.api import APISettings, create_app
from riftx.application.errors import AuthenticationError
from riftx.application.ports import AuditAuthorizationBinding
from riftx.browser.service import BrowserView
from riftx.domain import (
    BrowserMode,
    BrowserOwner,
    BrowserSession,
    BrowserSessionStatus,
    OperatorCapability,
    RunKind,
    TrustProfile,
)
from riftx.security import (
    DeploymentProfileError,
    LocalObjectAuthorizer,
    LocalPrincipalStore,
    ResourceNotAccessibleError,
)

_TEST_OPERATOR_TOKEN = "test-only-local-operator-token-0001"
_TEST_RUNNER_TOKEN = "test-only-runner-bootstrap-token-0002"


def _settings(tmp_path: Path, **changes: object) -> APISettings:
    settings = APISettings(
        listen_host="127.0.0.1",
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / "secrets" / "local-principal.json",
        admin_token=_TEST_OPERATOR_TOKEN,
        web_dist_path=tmp_path / "missing-web",
    )
    return replace(settings, **changes)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"trust_profile": None}, "trust_profile_required"),
        (
            {"trust_profile": TrustProfile.REMOTE_MULTIUSER},
            "remote_multiuser_not_available",
        ),
        ({"listen_host": "0.0.0.0"}, "local_profile_requires_loopback"),
        ({"listen_host": "example.test"}, "local_profile_requires_loopback"),
        ({"trust_proxy_auth": True}, "local_profile_rejects_remote_identity"),
        (
            {"cors_origins": ("https://remote.example.test",)},
            "local_profile_requires_loopback_origins",
        ),
    ],
)
def test_create_app_rejects_unsafe_or_unsupported_profile_configuration(
    tmp_path: Path,
    changes: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(DeploymentProfileError) as captured:
        create_app(settings=_settings(tmp_path, **changes))

    assert captured.value.code == code
    assert captured.value.message_en
    assert captured.value.message_zh


def test_create_app_rejects_multiple_profile_values(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    object.__setattr__(
        settings,
        "trust_profile",
        (
            TrustProfile.LOCAL_SINGLE_OPERATOR,
            TrustProfile.REMOTE_MULTIUSER,
        ),
    )

    with pytest.raises(DeploymentProfileError, match="trust_profile_ambiguous"):
        create_app(settings=settings)


@pytest.mark.parametrize(
    ("credential", "code"),
    [
        (None, "local_operator_credential_required"),
        ("", "local_operator_credential_required"),
        (" " * 32, "local_operator_credential_weak"),
        ("test-only-short-token", "local_operator_credential_weak"),
        (" test-only-local-operator-token-0001", "local_operator_credential_weak"),
        ("令" * 32, "local_operator_credential_weak"),
    ],
)
def test_create_app_rejects_missing_or_weak_operator_credential_before_state_write(
    tmp_path: Path,
    credential: str | None,
    code: str,
) -> None:
    settings = _settings(tmp_path, admin_token=credential)

    with pytest.raises(DeploymentProfileError) as captured:
        create_app(settings=settings)

    assert captured.value.code == code
    assert captured.value.message_en
    assert captured.value.message_zh
    assert not settings.local_principal_path.exists()


def test_create_app_accepts_minimum_length_operator_credential(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path, admin_token="x" * 32))

    assert app.state.local_operator_security.configured_token == "x" * 32


@pytest.mark.parametrize(
    "credential",
    [
        "",
        "r" * 31,
        " " * 32,
        " runner-bootstrap-token-with-space-0001",
        "引" * 32,
    ],
)
def test_create_app_rejects_weak_runner_bootstrap_before_state_write(
    tmp_path: Path,
    credential: str,
) -> None:
    settings = _settings(tmp_path, runner_registration_token=credential)

    with pytest.raises(DeploymentProfileError) as captured:
        create_app(settings=settings)

    assert captured.value.code == "runner_registration_credential_weak"
    assert captured.value.message_en
    assert captured.value.message_zh
    if credential:
        assert credential not in repr(captured.value)
    assert not settings.local_principal_path.exists()


def test_create_app_accepts_disabled_and_minimum_length_runner_registration(
    tmp_path: Path,
) -> None:
    disabled = create_app(settings=_settings(tmp_path / "disabled"))
    enabled = create_app(
        settings=_settings(
            tmp_path / "enabled",
            runner_registration_token="r" * 32,
        )
    )

    assert disabled.state.local_operator_security.configured_token == _TEST_OPERATOR_TOKEN
    assert enabled.state.local_operator_security.configured_token == _TEST_OPERATOR_TOKEN


def test_create_app_rejects_operator_and_runner_bootstrap_credential_reuse(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        runner_registration_token=_TEST_OPERATOR_TOKEN,
    )

    with pytest.raises(DeploymentProfileError) as captured:
        create_app(settings=settings)

    assert captured.value.code == "operator_runner_credential_reuse"
    assert captured.value.message_en
    assert captured.value.message_zh
    assert not settings.local_principal_path.exists()


def test_create_app_accepts_domain_separated_operator_and_runner_credentials(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=_settings(
            tmp_path,
            runner_registration_token=_TEST_RUNNER_TOKEN,
        )
    )

    assert app.state.local_operator_security.configured_token == _TEST_OPERATOR_TOKEN


def test_non_ascii_presented_credential_fails_with_stable_authentication_error(
    tmp_path: Path,
) -> None:
    app = create_app(settings=_settings(tmp_path))

    with pytest.raises(AuthenticationError) as captured:
        app.state.local_operator_security.authenticate("无效令牌")

    assert captured.value.code == "local_operator_authentication_failed"


def test_local_principal_is_stable_atomic_and_contains_no_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    first = create_app(settings=settings)
    second = create_app(settings=settings)
    first_principal = first.state.local_operator_security.principal
    second_principal = second.state.local_operator_security.principal

    assert first_principal == second_principal
    assert first_principal.profile is TrustProfile.LOCAL_SINGLE_OPERATOR
    payload = json.loads(settings.local_principal_path.read_text())
    assert payload == {
        "principal_id": first_principal.id,
        "profile": "local_single_operator",
        "schema_version": 1,
    }
    assert stat.S_IMODE(settings.local_principal_path.stat().st_mode) == 0o600
    assert _TEST_OPERATOR_TOKEN not in settings.local_principal_path.read_text()
    assert _TEST_OPERATOR_TOKEN not in repr(settings)


def test_corrupt_or_permission_unsafe_principal_state_fails_closed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.local_principal_path.parent.mkdir(parents=True)
    settings.local_principal_path.parent.chmod(0o700)
    settings.local_principal_path.write_text("not-json")
    settings.local_principal_path.chmod(0o600)

    with pytest.raises(DeploymentProfileError) as corrupt:
        create_app(settings=settings)
    assert corrupt.value.code == "local_principal_state_invalid"

    settings.local_principal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "local_single_operator",
                "principal_id": "principal-1",
            }
        )
    )
    settings.local_principal_path.chmod(0o644)
    with pytest.raises(DeploymentProfileError) as unsafe:
        create_app(settings=settings)
    assert unsafe.value.code == "local_principal_state_permissions_unsafe"


def test_concurrent_first_start_atomically_publishes_one_principal(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    def start(_: int) -> str:
        return create_app(settings=settings).state.local_operator_security.principal.id

    with ThreadPoolExecutor(max_workers=8) as executor:
        principal_ids = list(executor.map(start, range(32)))

    assert len(set(principal_ids)) == 1
    assert not list(settings.local_principal_path.parent.glob(".*.tmp"))


def test_principal_temp_collision_never_deletes_a_file_it_did_not_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "secrets"
    parent.mkdir(mode=0o700)
    path = parent / "local-principal.json"
    first_collision = parent / f".{path.name}.first.tmp"
    second_collision = parent / f".{path.name}.second.tmp"
    first_collision.write_text("first-owner")
    second_collision.write_text("second-owner")
    generated_values = iter(("first", "second", "third"))
    monkeypatch.setattr(security_module, "uuid4", lambda: next(generated_values))

    principal = LocalPrincipalStore(path).load_or_create(frozenset(OperatorCapability))

    assert principal.id == "local-principal:v1:first"
    assert first_collision.read_text() == "first-owner"
    assert second_collision.read_text() == "second-owner"
    assert not (parent / f".{path.name}.third.tmp").exists()


def test_principal_load_is_bound_to_open_descriptor_during_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "secrets"
    parent.mkdir(mode=0o700)
    path = parent / "local-principal.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "local_single_operator",
                "principal_id": "safe-principal",
            }
        )
    )
    path.chmod(0o600)
    replacement = tmp_path / "replacement.json"
    replacement.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "local_single_operator",
                "principal_id": "swapped-principal",
            }
        )
    )
    replacement.chmod(0o600)

    real_lstat = os.lstat
    real_open = os.open
    swapped = False

    def swap_after_lstat(candidate: os.PathLike[str] | str, *args: object) -> os.stat_result:
        nonlocal swapped
        metadata = real_lstat(candidate, *args)
        if Path(candidate) == path and not swapped:
            path.unlink()
            path.symlink_to(replacement)
            swapped = True
        return metadata

    def swap_after_open(
        candidate: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = real_open(candidate, flags, mode, dir_fd=dir_fd)
        if candidate == path.name and dir_fd is not None and not swapped:
            path.unlink()
            path.symlink_to(replacement)
            swapped = True
        return descriptor

    monkeypatch.setattr(security_module.os, "lstat", swap_after_lstat)
    monkeypatch.setattr(security_module.os, "open", swap_after_open)
    supported_dir_fd = set(os.supports_dir_fd)
    supported_dir_fd.discard(real_open)
    supported_dir_fd.add(swap_after_open)
    monkeypatch.setattr(security_module.os, "supports_dir_fd", supported_dir_fd)

    principal = LocalPrincipalStore(path).load_or_create(frozenset(OperatorCapability))

    assert swapped is True
    assert path.is_symlink()
    assert principal.id == "safe-principal"


def test_principal_parent_swap_is_validated_on_open_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "secrets"
    parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "moved-secrets"
    path = parent / "local-principal.json"
    real_open = os.open
    swapped = False

    def swap_before_parent_open(
        candidate: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        candidate_path = Path(candidate)
        targets_parent = candidate_path == parent or (
            dir_fd is not None and candidate_path == Path(parent.name)
        )
        if targets_parent and not swapped:
            parent.rename(moved_parent)
            parent.mkdir(mode=0o700)
            parent.chmod(0o777)
            swapped = True
        return real_open(candidate, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(security_module.os, "open", swap_before_parent_open)
    supported_dir_fd = set(os.supports_dir_fd)
    supported_dir_fd.discard(real_open)
    supported_dir_fd.add(swap_before_parent_open)
    monkeypatch.setattr(security_module.os, "supports_dir_fd", supported_dir_fd)

    with pytest.raises(DeploymentProfileError) as captured:
        LocalPrincipalStore(path).load_or_create(frozenset(OperatorCapability))

    assert swapped is True
    assert captured.value.code == "local_principal_parent_permissions_unsafe"
    assert not path.exists()


def test_principal_path_rejects_writable_but_allows_read_only_ancestors(
    tmp_path: Path,
) -> None:
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir(mode=0o755)
    path = ancestor / "secrets" / "local-principal.json"
    store = LocalPrincipalStore(path)
    principal = store.load_or_create(frozenset(OperatorCapability))

    assert principal.id.startswith("local-principal:v1:")
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    moved_ancestor = tmp_path / "original-ancestor"
    ancestor.rename(moved_ancestor)
    ancestor.mkdir(mode=0o700)
    ancestor.chmod(0o777)

    with pytest.raises(DeploymentProfileError) as captured:
        store.load_or_create(frozenset(OperatorCapability))

    assert captured.value.code == "local_principal_ancestor_permissions_unsafe"
    assert not path.parent.exists()
    assert (moved_ancestor / "secrets" / path.name).exists()


def test_principal_path_rejects_an_ancestor_owned_by_another_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ancestor = tmp_path / "precreated-ancestor"
    ancestor.mkdir(mode=0o755)
    ancestor_metadata = ancestor.stat()
    path = ancestor / "secrets" / "local-principal.json"
    real_fstat = os.fstat
    untrusted_uid = next(uid for uid in (1, 2, 3) if uid not in {0, os.geteuid()})

    def report_untrusted_owner(descriptor: int) -> os.stat_result:
        metadata = real_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (
            ancestor_metadata.st_dev,
            ancestor_metadata.st_ino,
        ):
            return metadata
        values = list(metadata)
        values[4] = untrusted_uid
        return os.stat_result(values)

    monkeypatch.setattr(security_module.os, "fstat", report_untrusted_owner)

    with pytest.raises(DeploymentProfileError) as captured:
        LocalPrincipalStore(path).load_or_create(frozenset(OperatorCapability))

    assert captured.value.code == "local_principal_ancestor_owner_invalid"
    assert not path.parent.exists()


def test_principal_path_allows_a_root_owned_sticky_system_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sticky_ancestor = tmp_path / "system-sticky"
    sticky_ancestor.mkdir(mode=0o700)
    sticky_ancestor.chmod(0o1777)
    sticky_metadata = sticky_ancestor.stat()
    path = sticky_ancestor / "owned" / "secrets" / "local-principal.json"
    real_fstat = os.fstat

    def report_root_owner(descriptor: int) -> os.stat_result:
        metadata = real_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (
            sticky_metadata.st_dev,
            sticky_metadata.st_ino,
        ):
            return metadata
        values = list(metadata)
        values[4] = 0
        return os.stat_result(values)

    monkeypatch.setattr(security_module.os, "fstat", report_root_owner)

    principal = LocalPrincipalStore(path).load_or_create(frozenset(OperatorCapability))

    assert principal.id.startswith("local-principal:v1:")
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_principal_creation_overrides_restrictive_umask(tmp_path: Path) -> None:
    parent = tmp_path / "secrets"
    parent.mkdir(mode=0o700)
    path = parent / "local-principal.json"
    store = LocalPrincipalStore(path)

    previous_umask = os.umask(0o777)
    try:
        principal = store.load_or_create(frozenset(OperatorCapability))
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load_or_create(frozenset(OperatorCapability)).id == principal.id


def test_principal_publication_verifies_the_linked_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "secrets"
    parent.mkdir(mode=0o700)
    path = parent / "local-principal.json"
    replacement = parent / "replacement.json"
    replacement.write_text("replacement")
    replacement.chmod(0o600)
    real_link = os.link

    def publish_different_inode(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        del source
        real_link(
            replacement.name,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(security_module.os, "link", publish_different_inode)
    supported_dir_fd = set(os.supports_dir_fd)
    supported_dir_fd.discard(real_link)
    supported_dir_fd.add(publish_different_inode)
    monkeypatch.setattr(security_module.os, "supports_dir_fd", supported_dir_fd)
    supported_follow_symlinks = set(os.supports_follow_symlinks)
    supported_follow_symlinks.discard(real_link)
    supported_follow_symlinks.add(publish_different_inode)
    monkeypatch.setattr(
        security_module.os,
        "supports_follow_symlinks",
        supported_follow_symlinks,
    )

    with pytest.raises(DeploymentProfileError) as captured:
        LocalPrincipalStore(path).load_or_create(frozenset(OperatorCapability))

    assert captured.value.code == "local_principal_state_invalid"
    assert not list(parent.glob(".*.tmp"))


def test_principal_store_fails_closed_without_secure_posix_primitives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "secrets" / "local-principal.json"
    monkeypatch.setattr(security_module.os, "supports_dir_fd", set())

    with pytest.raises(DeploymentProfileError) as captured:
        LocalPrincipalStore(path).load_or_create(frozenset(OperatorCapability))

    assert captured.value.code == "local_principal_platform_unsupported"
    assert not path.parent.exists()


def test_principal_parent_symlink_or_open_permissions_fail_closed(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-secrets"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-secrets"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(DeploymentProfileError) as linked:
        create_app(
            settings=_settings(
                tmp_path,
                local_principal_path=linked_parent / "principal.json",
            )
        )
    assert linked.value.code == "local_principal_parent_invalid"

    linked_ancestor = tmp_path / "linked-ancestor"
    linked_ancestor.symlink_to(real_parent, target_is_directory=True)
    nested_path = linked_ancestor / "nested" / "principal.json"
    with pytest.raises(DeploymentProfileError) as nested:
        create_app(
            settings=_settings(
                tmp_path,
                local_principal_path=nested_path,
            )
        )
    assert nested.value.code == "local_principal_parent_invalid"
    assert not (real_parent / "nested").exists()

    open_parent = tmp_path / "open-secrets"
    open_parent.mkdir(mode=0o755)
    with pytest.raises(DeploymentProfileError) as unsafe:
        create_app(
            settings=_settings(
                tmp_path,
                local_principal_path=open_parent / "principal.json",
            )
        )
    assert unsafe.value.code == "local_principal_parent_permissions_unsafe"


def test_create_app_rejects_split_settings_and_control_plane(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    control_plane = SimpleNamespace(settings=settings)
    with pytest.raises(DeploymentProfileError) as captured:
        create_app(
            control_plane=control_plane,  # type: ignore[arg-type]
            settings=settings,
        )
    assert captured.value.code == "control_plane_settings_ambiguous"


def test_local_operator_authentication_capabilities_and_identity_headers(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    control_plane = SimpleNamespace(settings=settings)
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]

    with TestClient(app) as client:
        missing = client.get("/api/v1/security/profile")
        wrong = client.get(
            "/api/v1/security/profile",
            headers={"Authorization": "Bearer old-revoked-token"},
        )
        malformed = client.get(
            "/api/v1/security/profile",
            headers={"Authorization": f"Basic {_TEST_OPERATOR_TOKEN}"},
        )
        duplicate = client.get(
            "/api/v1/security/profile",
            headers=[
                ("Authorization", f"Bearer {_TEST_OPERATOR_TOKEN}"),
                ("Authorization", "Basic smuggled-credential"),
            ],
        )
        accepted = client.get(
            "/api/v1/security/profile",
            headers={
                "Authorization": f"Bearer {_TEST_OPERATOR_TOKEN}",
                "X-Forwarded-User": "forged-user",
                "X-Forwarded-Role": "owner",
                "X-RiftX-User-ID": "forged-id",
                "Cookie": "actor=forged-cookie",
            },
        )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "local_operator_token_missing"
    assert missing.json()["error"]["details"]["messages"]["zh-CN"]
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "local_operator_authentication_failed"
    assert "old-revoked-token" not in wrong.text
    assert malformed.status_code == 401
    assert malformed.json()["error"]["code"] == "local_operator_authentication_failed"
    assert duplicate.status_code == 401
    assert duplicate.json()["error"]["code"] == "local_operator_authentication_failed"
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["profile"] == "local_single_operator"
    assert body["principal_id"] == app.state.local_operator_security.principal.id
    assert body["principal_id"] not in {"forged-user", "forged-id"}
    assert body["tenant_safe"] is False
    assert body["features"] == {
        "gateway": False,
        "remote_identity": False,
        "route": False,
        "traffic_body": False,
        "traffic_replay": False,
    }


def test_local_operator_capability_is_enforced_server_side(tmp_path: Path) -> None:
    settings = _settings(tmp_path, local_operator_capabilities=frozenset())
    app = create_app(
        control_plane=SimpleNamespace(settings=settings)  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/security/profile",
            headers={"Authorization": f"Bearer {_TEST_OPERATOR_TOKEN}"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "local_operator_capability_denied"


def test_unknown_api_route_authenticates_before_generic_not_found(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(
        control_plane=SimpleNamespace(settings=settings)  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        unauthenticated = client.get("/api/v2/not-implemented")
        authenticated = client.get(
            "/api/v2/not-implemented",
            headers={"Authorization": f"Bearer {_TEST_OPERATOR_TOKEN}"},
        )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 404
    assert authenticated.json()["error"]["code"] == "route_not_found"


class _BrowserSpy:
    def __init__(self) -> None:
        self.calls = 0
        self.view = BrowserView(
            session=BrowserSession(
                id="browser-1",
                run_id="run-1",
                agent_session_id="agent-session-1",
                node_id="local",
                mode=BrowserMode.MANAGED_EPHEMERAL,
                status=BrowserSessionStatus.ACTIVE,
                owner=BrowserOwner.AGENT,
            ),
            pages=[],
        )

    async def get(
        self,
        session_id: str,
        *,
        expected_run_id: str | None = None,
    ) -> object:
        assert session_id == "browser-1"
        assert expected_run_id == "run-1"
        self.calls += 1
        return self.view

    async def resolve_run_id(self, session_id: str) -> str:
        assert session_id == "browser-1"
        return "run-1"

    async def observations_after(
        self,
        session_id: str,
        version: int,
        *,
        limit: int = 100,
    ) -> list[object]:
        assert session_id == "browser-1"
        assert version == 0
        assert limit == 100
        return []


class _ControlPlaneSpy:
    def __init__(self, settings: APISettings, browser: _BrowserSpy) -> None:
        self.settings = settings
        self.browser_service = browser
        self.run_service = _GeneralRunSpy()
        self.audit_service = object()
        self.terminal_service_accesses = 0

    @property
    def terminal_service(self) -> object:
        self.terminal_service_accesses += 1
        raise AssertionError("terminal service reached before authentication")


class _GeneralRunSpy:
    async def resolve_kind(self, run_id: str) -> RunKind:
        assert run_id == "run-1"
        return RunKind.GENERAL

    async def get_run(self, run_id: str) -> object:
        assert run_id == "run-1"
        return SimpleNamespace(kind=RunKind.GENERAL)


def test_websocket_rejects_before_object_access_and_never_echoes_credential(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    browser = _BrowserSpy()
    control_plane = _ControlPlaneSpy(settings, browser)
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]
    credential_protocol = app.state.local_operator_security.websocket_protocol(_TEST_OPERATOR_TOKEN)
    wrong_credential_protocol = app.state.local_operator_security.websocket_protocol("wrong-token")

    with TestClient(app) as client:
        for path in (
            "/api/v1/browser/sessions/browser-1/stream",
            "/api/v1/terminals/terminal-1/ws",
        ):
            with pytest.raises(WebSocketDisconnect) as missing:
                with client.websocket_connect(path):
                    pass
            assert missing.value.code == 4401

            with pytest.raises(WebSocketDisconnect) as wrong:
                with client.websocket_connect(
                    path,
                    subprotocols=[
                        "riftx.local-operator.v1",
                        wrong_credential_protocol,
                    ],
                ):
                    pass
            assert wrong.value.code == 4401

            with pytest.raises(WebSocketDisconnect) as conflicting:
                with client.websocket_connect(
                    path,
                    subprotocols=[
                        "riftx.local-operator.v1",
                        wrong_credential_protocol,
                    ],
                    headers={"Authorization": f"Bearer {_TEST_OPERATOR_TOKEN}"},
                ):
                    pass
            assert conflicting.value.code == 4401

        invalid_protocol_sets = (
            ([credential_protocol], {}),
            (
                [
                    "riftx.local-operator.v1",
                    credential_protocol,
                    credential_protocol,
                ],
                {"Authorization": f"Bearer {_TEST_OPERATOR_TOKEN}"},
            ),
            (
                [
                    "riftx.local-operator.v1",
                    "riftx.local-operator.bearer.v1.!",
                ],
                {"Authorization": f"Bearer {_TEST_OPERATOR_TOKEN}"},
            ),
            (
                ["riftx.local-operator.v1", credential_protocol],
                {"Authorization": f"Basic {_TEST_OPERATOR_TOKEN}"},
            ),
        )
        for protocols, headers in invalid_protocol_sets:
            with pytest.raises(WebSocketDisconnect) as invalid_protocol:
                with client.websocket_connect(
                    "/api/v1/browser/sessions/browser-1/stream",
                    subprotocols=protocols,
                    headers=headers,
                ):
                    pass
            assert invalid_protocol.value.code == 4401

        assert browser.calls == 0
        assert control_plane.terminal_service_accesses == 0

        for origin in (
            "http://127.0.0.1:9999",
            "https://remote.example.test",
            "null",
        ):
            with pytest.raises(WebSocketDisconnect) as bad_origin:
                with client.websocket_connect(
                    "/api/v1/browser/sessions/browser-1/stream",
                    subprotocols=["riftx.local-operator.v1", credential_protocol],
                    headers={"Origin": origin},
                ):
                    pass
            assert bad_origin.value.code == 4403

        assert browser.calls == 0

    assert _TEST_OPERATOR_TOKEN not in credential_protocol


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8787",
    ],
)
def test_websocket_accepts_vite_and_production_origins_with_fixed_protocol(
    tmp_path: Path,
    origin: str,
) -> None:
    settings = _settings(tmp_path)
    browser = _BrowserSpy()
    control_plane = _ControlPlaneSpy(settings, browser)
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]
    credential_protocol = app.state.local_operator_security.websocket_protocol(_TEST_OPERATOR_TOKEN)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/browser/sessions/browser-1/stream",
            subprotocols=["riftx.local-operator.v1", credential_protocol],
            headers={"Origin": origin},
        ) as websocket:
            assert websocket.accepted_subprotocol == "riftx.local-operator.v1"
            assert credential_protocol not in websocket.accepted_subprotocol
            assert websocket.receive_json()["type"] == "browser_state"

    assert browser.calls == 1
    assert _TEST_OPERATOR_TOKEN not in credential_protocol


def test_websocket_capability_denial_precedes_service_access(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        local_operator_capabilities=frozenset({OperatorCapability.READ}),
    )
    browser = _BrowserSpy()
    control_plane = _ControlPlaneSpy(settings, browser)
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]
    credential_protocol = app.state.local_operator_security.websocket_protocol(_TEST_OPERATOR_TOKEN)

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as browser_denied:
            with client.websocket_connect(
                "/api/v1/browser/sessions/browser-1/stream",
                subprotocols=["riftx.local-operator.v1", credential_protocol],
                headers={"Origin": "http://127.0.0.1:8787"},
            ):
                pass
        assert browser_denied.value.code == 4403

        with pytest.raises(WebSocketDisconnect) as terminal_denied:
            with client.websocket_connect(
                "/api/v1/terminals/terminal-1/ws",
                headers={"Authorization": f"Bearer {_TEST_OPERATOR_TOKEN}"},
            ):
                pass
        assert terminal_denied.value.code == 4403

    assert browser.calls == 0
    assert control_plane.terminal_service_accesses == 0


def test_parent_run_authorizer_masks_mismatch_and_unavailable_resource(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    security = app.state.local_operator_security
    principal = security.principal
    authorizer = LocalObjectAuthorizer(security)

    authorizer.require_child_run(
        principal,
        parent_run_id="run-1",
        resource_run_id="run-1",
        capability=OperatorCapability.READ,
    )
    with pytest.raises(ResourceNotAccessibleError) as mismatch:
        authorizer.require_child_run(
            principal,
            parent_run_id="run-1",
            resource_run_id="run-2",
            capability=OperatorCapability.READ,
        )
    with pytest.raises(ResourceNotAccessibleError) as unavailable:
        authorizer.require_child_run(
            principal,
            parent_run_id="run-1",
            resource_run_id=None,
            capability=OperatorCapability.READ,
        )

    assert mismatch.value.code == unavailable.value.code == "resource_not_accessible"
    assert mismatch.value.message == unavailable.value.message


def _valid_audit_binding() -> AuditAuthorizationBinding:
    return AuditAuthorizationBinding(
        requested_audit_id="audit-1",
        audit_id="audit-1",
        scan_run_id="run-1",
        scan_project_id="project-1",
        scan_engagement_id="engagement-1",
        scan_contract_id="contract-1",
        scan_contract_digest="a" * 64,
        run_id="run-1",
        run_engagement_id="engagement-1",
        run_kind="code_audit",
        project_id="project-1",
        project_engagement_id="engagement-1",
        engagement_id="engagement-1",
        contract_id="contract-1",
        contract_audit_id="audit-1",
        contract_digest="a" * 64,
        request_audit_id="audit-1",
        request_run_id="run-1",
        request_project_id="project-1",
        request_engagement_id="engagement-1",
        request_contract_id="contract-1",
        request_contract_digest="a" * 64,
    )


@pytest.mark.parametrize(
    "field",
    [
        "requested_audit_id",
        "audit_id",
        "scan_run_id",
        "scan_project_id",
        "scan_engagement_id",
        "scan_contract_id",
        "scan_contract_digest",
        "run_id",
        "run_engagement_id",
        "run_kind",
        "project_id",
        "project_engagement_id",
        "engagement_id",
        "contract_id",
        "contract_audit_id",
        "contract_digest",
        "request_audit_id",
        "request_run_id",
        "request_project_id",
        "request_engagement_id",
        "request_contract_id",
        "request_contract_digest",
    ],
)
def test_audit_authorizer_rejects_every_cross_object_binding(
    tmp_path: Path,
    field: str,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    security = app.state.local_operator_security
    authorizer = LocalObjectAuthorizer(security)
    valid = _valid_audit_binding()

    authorizer.require_audit_binding(
        security.principal,
        valid,
        capability=OperatorCapability.READ,
    )
    with pytest.raises(ResourceNotAccessibleError) as captured:
        authorizer.require_audit_binding(
            security.principal,
            replace(valid, **{field: "tampered-binding"}),
            capability=OperatorCapability.READ,
        )

    assert captured.value.code == "resource_not_accessible"
    assert captured.value.message == "The requested resource was not found"


def test_audit_profile_a_scope_and_server_domain_reference_are_stable(tmp_path: Path) -> None:
    first = create_app(settings=_settings(tmp_path))
    second = create_app(settings=_settings(tmp_path))
    first_security = first.state.local_operator_security
    second_security = second.state.local_operator_security
    first_authorizer = LocalObjectAuthorizer(first_security)
    second_authorizer = LocalObjectAuthorizer(second_security)

    scope = first_authorizer.authorized_engagement_scope(
        first_security.principal,
        capability=OperatorCapability.READ,
    )
    first_reference = first_authorizer.draft_authorization_reference(
        first_security.principal,
        capability=OperatorCapability.WRITE,
    )
    second_reference = second_authorizer.draft_authorization_reference(
        second_security.principal,
        capability=OperatorCapability.WRITE,
    )

    assert scope.all_engagements is True
    assert scope.engagement_ids == frozenset()
    assert scope.can_create_engagement is True
    assert first_reference == second_reference
    assert len(first_reference) == 64
