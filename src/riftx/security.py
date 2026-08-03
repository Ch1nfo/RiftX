"""Fail-closed deployment profile and local operator security boundary."""

from __future__ import annotations

import base64
import errno
import hashlib
import ipaddress
import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import uuid4

from riftx.application.errors import (
    AuthenticationError,
    AuthorizationError,
    ResourceNotAccessibleError,
)
from riftx.application.ports.audits import (
    AuditAuthorizationBinding,
    AuditEngagementScope,
)
from riftx.domain import LocalPrincipal, OperatorCapability, TrustProfile

LOCAL_OPERATOR_WEBSOCKET_PROTOCOL = "riftx.local-operator.v1"
MIN_SECURITY_CREDENTIAL_LENGTH = 32
MIN_LOCAL_OPERATOR_CREDENTIAL_LENGTH = MIN_SECURITY_CREDENTIAL_LENGTH
_WEBSOCKET_CREDENTIAL_PREFIX = "riftx.local-operator.bearer.v1."
_PRINCIPAL_SCHEMA_VERSION = 1
_PRINCIPAL_STATE_MAX_BYTES = 4096
_PRINCIPAL_TEMPORARY_ATTEMPTS = 16

ALL_LOCAL_OPERATOR_CAPABILITIES = frozenset(OperatorCapability)
LOCAL_HIGH_RISK_FEATURES = MappingProxyType(
    {
        "gateway": False,
        "remote_identity": False,
        "route": False,
        "traffic_body": False,
        "traffic_replay": False,
    }
)


class DeploymentProfileError(RuntimeError):
    """A stable, bilingual startup failure for an unsafe deployment boundary."""

    def __init__(self, code: str, message_en: str, message_zh: str) -> None:
        super().__init__(f"[{code}] {message_en} / {message_zh}")
        self.code = code
        self.message_en = message_en
        self.message_zh = message_zh


def validate_deployment_profile(
    *,
    trust_profile: object,
    listen_host: object,
    trust_proxy_auth: bool,
    cors_origins: tuple[str, ...],
) -> TrustProfile:
    """Validate the only supported profile at every application assembly boundary."""

    if trust_profile is None or trust_profile == "":
        raise DeploymentProfileError(
            "trust_profile_required",
            "Select exactly one RiftX trust profile before starting the Control Plane",
            "启动控制平面前必须明确且仅选择一个 RiftX 信任配置",
        )
    if isinstance(trust_profile, (list, tuple, set, frozenset)):
        raise DeploymentProfileError(
            "trust_profile_ambiguous",
            "Multiple trust profiles cannot be selected at the same time",
            "不能同时选择多个信任配置",
        )
    try:
        profile = TrustProfile(trust_profile)
    except (TypeError, ValueError) as exc:
        raise DeploymentProfileError(
            "trust_profile_unknown",
            "The configured RiftX trust profile is unknown",
            "配置的 RiftX 信任配置未知",
        ) from exc
    if profile is TrustProfile.REMOTE_MULTIUSER:
        raise DeploymentProfileError(
            "remote_multiuser_not_available",
            "The remote_multiuser profile is not implemented and remains unavailable",
            "remote_multiuser 配置尚未实现，当前不可用",
        )
    if not isinstance(listen_host, str) or not is_loopback_host(listen_host):
        raise DeploymentProfileError(
            "local_profile_requires_loopback",
            "The local_single_operator profile requires a loopback listen address",
            "local_single_operator 配置要求监听回环地址",
        )
    if trust_proxy_auth:
        raise DeploymentProfileError(
            "local_profile_rejects_remote_identity",
            "The local_single_operator profile rejects proxy or remote identity configuration",
            "local_single_operator 配置拒绝代理或远程身份设置",
        )
    if any(not is_loopback_origin(origin) for origin in cors_origins):
        raise DeploymentProfileError(
            "local_profile_requires_loopback_origins",
            "The local_single_operator profile permits only loopback browser origins",
            "local_single_operator 配置只允许回环浏览器来源",
        )
    return profile


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def is_loopback_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        return is_loopback_host(parsed.hostname)
    except ValueError:
        return False


def validate_local_operator_credential(configured_token: object) -> str:
    """Reject missing or trivially weak operator credentials before API assembly."""

    if not isinstance(configured_token, str) or not configured_token:
        raise DeploymentProfileError(
            "local_operator_credential_required",
            "Set RIFTX_ADMIN_TOKEN before starting the Control Plane",
            "启动控制平面前必须设置 RIFTX_ADMIN_TOKEN",
        )
    if not _credential_has_required_shape(configured_token):
        raise DeploymentProfileError(
            "local_operator_credential_weak",
            "RIFTX_ADMIN_TOKEN must contain at least 32 non-whitespace printable ASCII characters",
            "RIFTX_ADMIN_TOKEN 必须至少包含 32 个非空白 ASCII 可打印字符",
        )
    return configured_token


def validate_runner_registration_credential(configured_token: object) -> str | None:
    """Reject weak configured Runner bootstrap credentials; ``None`` disables registration."""

    if configured_token is None:
        return None
    if not isinstance(configured_token, str) or not _credential_has_required_shape(
        configured_token
    ):
        raise DeploymentProfileError(
            "runner_registration_credential_weak",
            "RIFTX_RUNNER_REGISTRATION_TOKEN must contain at least 32 "
            "non-whitespace printable ASCII characters",
            "RIFTX_RUNNER_REGISTRATION_TOKEN 必须至少包含 32 个非空白 ASCII 可打印字符",
        )
    return configured_token


def validate_operator_runner_credential_separation(
    operator_token: object,
    runner_registration_token: object,
) -> None:
    """Keep the inbound operator boundary distinct from Runner bootstrap."""

    validated_operator_token = validate_local_operator_credential(operator_token)
    validated_runner_token = validate_runner_registration_credential(runner_registration_token)
    if validated_runner_token is not None and _credentials_match(
        validated_operator_token,
        validated_runner_token,
    ):
        raise DeploymentProfileError(
            "operator_runner_credential_reuse",
            "RIFTX_ADMIN_TOKEN and RIFTX_RUNNER_REGISTRATION_TOKEN must be different credentials",
            "RIFTX_ADMIN_TOKEN 与 RIFTX_RUNNER_REGISTRATION_TOKEN 必须使用不同凭据",
        )


@dataclass(frozen=True, slots=True)
class LocalPrincipalStore:
    path: Path

    def load_or_create(
        self,
        capabilities: frozenset[OperatorCapability],
    ) -> LocalPrincipal:
        _require_secure_principal_filesystem()
        directory_descriptor = self._open_and_validate_parent()
        try:
            try:
                return self._load(directory_descriptor, capabilities)
            except FileNotFoundError:
                return self._create(directory_descriptor, capabilities)
        finally:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass

    def _load(
        self,
        directory_descriptor: int,
        capabilities: frozenset[OperatorCapability],
    ) -> LocalPrincipal:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.path.name,
                _principal_file_open_flags(),
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            raise
        except NotImplementedError as exc:
            raise _principal_platform_error() from exc
        except OSError as exc:
            raise _principal_state_error() from exc
        try:
            metadata = os.fstat(descriptor)
            _validate_principal_file_metadata(metadata)
            handle = os.fdopen(descriptor, "rb")
            descriptor = None
            with handle:
                raw = handle.read(_PRINCIPAL_STATE_MAX_BYTES + 1)
            if len(raw) > _PRINCIPAL_STATE_MAX_BYTES:
                raise ValueError("principal state is too large")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "schema_version",
                "profile",
                "principal_id",
            }:
                raise ValueError("principal state schema mismatch")
            if payload["schema_version"] != _PRINCIPAL_SCHEMA_VERSION:
                raise ValueError("principal state version mismatch")
            if payload["profile"] != TrustProfile.LOCAL_SINGLE_OPERATOR.value:
                raise ValueError("principal state profile mismatch")
            principal_id = payload["principal_id"]
            if not isinstance(principal_id, str):
                raise ValueError("principal ID must be a string")
            return LocalPrincipal(id=principal_id, capabilities=capabilities)
        except (
            OSError,
            RecursionError,
            UnicodeError,
            ValueError,
            TypeError,
        ) as exc:
            raise _principal_state_error() from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _create(
        self,
        directory_descriptor: int,
        capabilities: frozenset[OperatorCapability],
    ) -> LocalPrincipal:
        temporary_name: str | None = None
        descriptor: int | None = None
        operation_completed = False
        winner_exists = False
        try:
            principal = LocalPrincipal(
                id=f"local-principal:v1:{uuid4()}",
                capabilities=capabilities,
            )
            payload = json.dumps(
                {
                    "principal_id": principal.id,
                    "profile": principal.profile.value,
                    "schema_version": _PRINCIPAL_SCHEMA_VERSION,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            temporary_name, descriptor = self._open_temporary(directory_descriptor)
            os.fchmod(descriptor, 0o600)
            _validate_principal_file_metadata(os.fstat(descriptor))
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = None
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                try:
                    os.link(
                        temporary_name,
                        self.path.name,
                        src_dir_fd=directory_descriptor,
                        dst_dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    winner_exists = True
                else:
                    self._verify_published_inode(
                        directory_descriptor,
                        handle.fileno(),
                    )
                    os.fsync(directory_descriptor)
            operation_completed = True
        except NotImplementedError as exc:
            raise _principal_platform_error() from exc
        except DeploymentProfileError:
            raise
        except (OSError, ValueError) as exc:
            raise _principal_state_error() from exc
        finally:
            cleanup_error: OSError | None = None
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    cleanup_error = exc
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                    os.fsync(directory_descriptor)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    cleanup_error = cleanup_error or exc
            if operation_completed and cleanup_error is not None:
                raise _principal_state_error() from cleanup_error

        if winner_exists:
            try:
                return self._load(directory_descriptor, capabilities)
            except FileNotFoundError as exc:
                raise _principal_state_error() from exc
        return principal

    def _verify_published_inode(
        self,
        directory_descriptor: int,
        source_descriptor: int,
    ) -> None:
        published_descriptor: int | None = None
        try:
            published_descriptor = os.open(
                self.path.name,
                _principal_file_open_flags(),
                dir_fd=directory_descriptor,
            )
            source_metadata = os.fstat(source_descriptor)
            published_metadata = os.fstat(published_descriptor)
            _validate_principal_file_metadata(published_metadata)
            if (published_metadata.st_dev, published_metadata.st_ino) != (
                source_metadata.st_dev,
                source_metadata.st_ino,
            ):
                raise _principal_state_error()
        finally:
            if published_descriptor is not None:
                try:
                    os.close(published_descriptor)
                except OSError:
                    pass

    def _open_temporary(self, directory_descriptor: int) -> tuple[str, int]:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        for _ in range(_PRINCIPAL_TEMPORARY_ATTEMPTS):
            temporary_name = f".{self.path.name}.{uuid4()}.tmp"
            try:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
            return temporary_name, descriptor
        raise _principal_state_error()

    def _open_and_validate_parent(self) -> int:
        if not self.path.name or self.path.name in {".", ".."}:
            raise _principal_parent_invalid()

        flags = _principal_directory_open_flags()
        parent = self.path.parent
        try:
            if parent.is_absolute():
                directory_descriptor = os.open(os.sep, flags)
                raw_components = parent.parts[1:]
            else:
                directory_descriptor = os.open(".", flags)
                raw_components = parent.parts
        except NotImplementedError as exc:
            raise _principal_platform_error() from exc
        except OSError as exc:
            raise _principal_state_error() from exc

        components = tuple(component for component in raw_components if component not in {"", "."})
        if ".." in components:
            os.close(directory_descriptor)
            raise _principal_parent_invalid()

        try:
            if components:
                _validate_principal_ancestor_metadata(os.fstat(directory_descriptor))
            for index, component in enumerate(components):
                next_descriptor = self._open_or_create_directory_component(
                    directory_descriptor,
                    component,
                    flags,
                )
                try:
                    metadata = os.fstat(next_descriptor)
                    if not stat.S_ISDIR(metadata.st_mode):
                        raise _principal_parent_invalid()
                    if index < len(components) - 1:
                        _validate_principal_ancestor_metadata(metadata)
                except Exception:
                    os.close(next_descriptor)
                    raise
                os.close(directory_descriptor)
                directory_descriptor = next_descriptor
            _validate_principal_parent_metadata(os.fstat(directory_descriptor))
            return directory_descriptor
        except Exception:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass
            raise

    def _open_or_create_directory_component(
        self,
        directory_descriptor: int,
        component: str,
        flags: int,
    ) -> int:
        created = False
        try:
            return os.open(component, flags, dir_fd=directory_descriptor)
        except FileNotFoundError:
            try:
                os.mkdir(component, 0o700, dir_fd=directory_descriptor)
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise _principal_state_error() from exc
        except NotImplementedError as exc:
            raise _principal_platform_error() from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _principal_parent_invalid() from exc
            raise _principal_state_error() from exc

        try:
            next_descriptor = os.open(component, flags, dir_fd=directory_descriptor)
        except NotImplementedError as exc:
            raise _principal_platform_error() from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _principal_parent_invalid() from exc
            raise _principal_state_error() from exc

        if created:
            try:
                os.fchmod(next_descriptor, 0o700)
                os.fsync(next_descriptor)
                os.fsync(directory_descriptor)
            except OSError as exc:
                os.close(next_descriptor)
                raise _principal_state_error() from exc
        return next_descriptor


def _require_secure_principal_filesystem() -> None:
    required_dir_fd_functions = (os.open, os.mkdir, os.link, os.unlink)
    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "fchmod")
        or not hasattr(os, "geteuid")
        or any(function not in os.supports_dir_fd for function in required_dir_fd_functions)
        or os.link not in os.supports_follow_symlinks
    ):
        raise _principal_platform_error()


def _principal_directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _principal_file_open_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _validate_principal_parent_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise _principal_parent_invalid()
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise DeploymentProfileError(
            "local_principal_parent_permissions_unsafe",
            "The local principal parent directory must have mode 0700",
            "本地 Principal 父目录权限必须为 0700",
        )
    if metadata.st_uid != os.geteuid():
        raise DeploymentProfileError(
            "local_principal_parent_owner_invalid",
            "The local principal parent directory is not owned by the current user",
            "本地 Principal 父目录不属于当前用户",
        )


def _validate_principal_ancestor_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise _principal_parent_invalid()
    mode = stat.S_IMODE(metadata.st_mode)
    trusted_sticky_system_directory = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
    if mode & 0o022 and not trusted_sticky_system_directory:
        raise DeploymentProfileError(
            "local_principal_ancestor_permissions_unsafe",
            "Local principal path ancestors must not be writable by group or others "
            "unless they are root-owned sticky system directories",
            "除 root 所有的 sticky 系统目录外，本地 Principal 路径的祖先目录"
            "不能允许组用户或其他用户写入",
        )
    if metadata.st_uid not in {0, os.geteuid()}:
        raise DeploymentProfileError(
            "local_principal_ancestor_owner_invalid",
            "Local principal path ancestors must be owned by root or the current user",
            "本地 Principal 路径的祖先目录必须属于 root 或当前用户",
        )


def _validate_principal_file_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise _principal_state_error()
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise DeploymentProfileError(
            "local_principal_state_permissions_unsafe",
            "The local principal state file must have mode 0600",
            "本地 Principal 状态文件权限必须为 0600",
        )
    if metadata.st_uid != os.geteuid():
        raise DeploymentProfileError(
            "local_principal_state_owner_invalid",
            "The local principal state file is not owned by the current user",
            "本地 Principal 状态文件不属于当前用户",
        )


def _principal_parent_invalid() -> DeploymentProfileError:
    return DeploymentProfileError(
        "local_principal_parent_invalid",
        "The local principal parent must be a real directory without symbolic links",
        "本地 Principal 父路径必须是不含符号链接的真实目录",
    )


def _principal_platform_error() -> DeploymentProfileError:
    return DeploymentProfileError(
        "local_principal_platform_unsupported",
        "The local principal store requires secure POSIX filesystem primitives",
        "本地 Principal 存储需要安全的 POSIX 文件系统原语",
    )


def _principal_state_error() -> DeploymentProfileError:
    return DeploymentProfileError(
        "local_principal_state_invalid",
        "The local principal state is missing a valid, trusted schema",
        "本地 Principal 状态缺少有效且可信的结构",
    )


@dataclass(frozen=True, slots=True)
class LocalOperatorSecurity:
    principal: LocalPrincipal
    configured_token: str | None = field(default=None, repr=False)
    allowed_origins: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def create(
        cls,
        *,
        principal_path: Path,
        configured_token: str | None,
        capabilities: frozenset[OperatorCapability],
        allowed_origins: tuple[str, ...],
    ) -> LocalOperatorSecurity:
        validated_token = validate_local_operator_credential(configured_token)
        principal = LocalPrincipalStore(principal_path).load_or_create(capabilities)
        return cls(
            principal=principal,
            configured_token=validated_token,
            allowed_origins=frozenset(origin.rstrip("/") for origin in allowed_origins),
        )

    @property
    def features(self) -> MappingProxyType[str, bool]:
        return LOCAL_HIGH_RISK_FEATURES

    def authenticate(self, token: str | None) -> LocalPrincipal:
        if token is None:
            raise AuthenticationError(
                "local_operator_token_missing",
                "A local operator Bearer token is required",
                details=_localized("需要本地操作员 Bearer 令牌"),
            )
        if not self.configured_token:
            raise AuthenticationError(
                "local_operator_authentication_not_configured",
                "Local operator authentication is not configured",
                details=_localized("尚未配置本地操作员认证"),
            )
        if not _credentials_match(token, self.configured_token):
            raise _local_operator_authentication_failed()
        return self.principal

    def require_capability(
        self,
        principal: LocalPrincipal,
        capability: OperatorCapability,
    ) -> None:
        if principal != self.principal or capability not in principal.capabilities:
            raise AuthorizationError(
                "local_operator_capability_denied",
                "The local operator lacks the required server capability",
                details=_localized("本地操作员缺少所需的服务端能力"),
            )

    def require_websocket_origin(self, origin: str | None) -> None:
        if origin is None:
            return
        if origin.rstrip("/") not in self.allowed_origins:
            raise AuthorizationError(
                "local_operator_origin_denied",
                "The browser origin is not allowed by the local trust profile",
                details=_localized("本地信任配置不允许该浏览器来源"),
            )

    def websocket_protocol(self, token: str) -> str:
        encoded = base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{_WEBSOCKET_CREDENTIAL_PREFIX}{encoded}"

    def token_from_websocket_protocols(self, header: str | None) -> str | None:
        protocols = [item.strip() for item in (header or "").split(",") if item.strip()]
        credential_protocols = [
            item for item in protocols if item.startswith(_WEBSOCKET_CREDENTIAL_PREFIX)
        ]
        if not credential_protocols:
            return None
        if len(credential_protocols) != 1 or LOCAL_OPERATOR_WEBSOCKET_PROTOCOL not in protocols:
            raise _local_operator_authentication_failed()
        encoded = credential_protocols[0].removeprefix(_WEBSOCKET_CREDENTIAL_PREFIX)
        if not encoded:
            raise _local_operator_authentication_failed()
        try:
            padding = "=" * (-len(encoded) % 4)
            token = base64.b64decode(
                encoded + padding,
                altchars=b"-_",
                validate=True,
            ).decode("utf-8")
            if not token:
                raise ValueError("empty local operator credential")
            return token
        except (ValueError, UnicodeDecodeError):
            raise _local_operator_authentication_failed() from None


class LocalObjectAuthorizer:
    """Profile-A parent-resource check that can be replaced by a future ACL backend."""

    def __init__(self, security: LocalOperatorSecurity) -> None:
        self._security = security

    def require_child_run(
        self,
        principal: LocalPrincipal,
        *,
        parent_run_id: str,
        resource_run_id: str | None,
        capability: OperatorCapability,
    ) -> None:
        self._security.require_capability(principal, capability)
        if resource_run_id is None or not secrets.compare_digest(parent_run_id, resource_run_id):
            raise _resource_not_accessible()

    def authorized_engagement_scope(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> AuditEngagementScope:
        """Return Profile A's explicit scope through a future-ACL-ready seam."""

        self._security.require_capability(principal, capability)
        return AuditEngagementScope.profile_a()

    def require_audit_binding(
        self,
        principal: LocalPrincipal,
        binding: AuditAuthorizationBinding,
        *,
        capability: OperatorCapability,
    ) -> None:
        """Prove the raw Audit graph before a Contract is loaded or parsed."""

        self._security.require_capability(principal, capability)
        expected = (
            binding.audit_id,
            binding.scan_run_id,
            binding.scan_project_id,
            binding.scan_engagement_id,
            binding.scan_contract_id,
            binding.scan_contract_digest,
        )
        actual = (
            binding.requested_audit_id,
            binding.run_id,
            binding.project_id,
            binding.engagement_id,
            binding.contract_id,
            binding.contract_digest,
        )
        request_binding = (
            binding.request_audit_id,
            binding.request_run_id,
            binding.request_project_id,
            binding.request_engagement_id,
            binding.request_contract_id,
            binding.request_contract_digest,
        )
        owner_binding = (
            binding.audit_id,
            binding.scan_run_id,
            binding.scan_project_id,
            binding.scan_engagement_id,
            binding.scan_contract_id,
            binding.scan_contract_digest,
        )
        if (
            binding.run_kind != "code_audit"
            or not _constant_time_tuple_equal(expected, actual)
            or binding.contract_audit_id is None
            or not secrets.compare_digest(binding.audit_id, binding.contract_audit_id)
            or binding.run_engagement_id is None
            or binding.project_engagement_id is None
            or not secrets.compare_digest(
                binding.scan_engagement_id,
                binding.run_engagement_id,
            )
            or not secrets.compare_digest(
                binding.scan_engagement_id,
                binding.project_engagement_id,
            )
            or not _constant_time_tuple_equal(owner_binding, request_binding)
        ):
            raise _resource_not_accessible()

    def draft_authorization_reference(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> str:
        """Derive a stable server-owned Profile-A domain label.

        This digest is not an ACL credential or a preflight proof. It only
        prevents an HTTP caller from choosing the persistence domain label
        used by the draft-only AUD-104 creation path.
        """

        self._security.require_capability(principal, capability)
        payload = "\0".join(
            (
                "riftx.audit-local-authorization-reference/v1",
                principal.profile.value,
                principal.namespace_id,
                principal.id,
            )
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _constant_time_tuple_equal(
    left: tuple[str, ...],
    right: tuple[str | None, ...],
) -> bool:
    return len(left) == len(right) and all(
        candidate is not None and secrets.compare_digest(expected, candidate)
        for expected, candidate in zip(left, right, strict=True)
    )


def _resource_not_accessible() -> ResourceNotAccessibleError:
    return ResourceNotAccessibleError(
        "resource_not_accessible",
        "The requested resource was not found",
        details=_localized("未找到请求的资源"),
    )


def _localized(message_zh: str) -> dict[str, object]:
    return {"messages": {"zh-CN": message_zh}}


def _credentials_match(presented: str, configured: str) -> bool:
    return secrets.compare_digest(
        presented.encode("utf-8"),
        configured.encode("utf-8"),
    )


def _credential_has_required_shape(credential: str) -> bool:
    return (
        len(credential) >= MIN_SECURITY_CREDENTIAL_LENGTH
        and credential.isascii()
        and all(0x21 <= ord(character) <= 0x7E for character in credential)
    )


def _local_operator_authentication_failed() -> AuthenticationError:
    return AuthenticationError(
        "local_operator_authentication_failed",
        "The local operator credential is missing, invalid, or revoked",
        details=_localized("本地操作员凭据缺失、无效或已撤销"),
    )
