"""Authentication helpers for local operators, Runners, and administration."""

import secrets

from fastapi import Request, WebSocket, WebSocketException
from starlette.requests import HTTPConnection

from riftx.application.errors import AuthenticationError, AuthorizationError
from riftx.domain import LocalPrincipal, OperatorCapability
from riftx.security import LOCAL_OPERATOR_WEBSOCKET_PROTOCOL, LocalOperatorSecurity


def bearer_token(authorization: str | None) -> str:
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise AuthenticationError(
            "runner_authentication_failed",
            "Authorization must use a Runner Bearer token",
        )
    return token


def require_admin_token(
    request: Request,
    authorization: str | None,
) -> LocalPrincipal:
    """Authenticate the shared operator credential and enforce the admin route effect."""

    authorization_values = request.headers.getlist("authorization")
    if len(authorization_values) > 1:
        raise _admin_authentication_failed()
    resolved_authorization = authorization_values[0] if authorization_values else authorization
    if (
        authorization_values
        and authorization is not None
        and not _credentials_match(authorization_values[0], authorization)
    ):
        raise _admin_authentication_failed()
    scheme, separator, token = (resolved_authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise _admin_authentication_failed()
    security = _local_security(request)
    try:
        principal = security.authenticate(token)
    except AuthenticationError:
        raise _admin_authentication_failed() from None
    security.require_capability(principal, _required_admin_capability(request))
    request.state.local_principal = principal
    return principal


async def authorize_local_operator(connection: HTTPConnection) -> LocalPrincipal:
    """Authenticate one policy-classified local route before endpoint code runs."""

    security = _local_security(connection)
    try:
        token = _local_operator_token(connection, security)
        principal = security.authenticate(token)
        if connection.scope["type"] == "websocket":
            security.require_websocket_origin(connection.headers.get("origin"))
        security.require_capability(principal, _required_local_capability(connection))
    except AuthenticationError as exc:
        if connection.scope["type"] == "websocket":
            raise WebSocketException(code=4401, reason=exc.code) from None
        raise
    except AuthorizationError as exc:
        if connection.scope["type"] == "websocket":
            raise WebSocketException(code=4403, reason=exc.code) from None
        raise
    connection.state.local_principal = principal
    return principal


def get_authenticated_local_principal(connection: HTTPConnection) -> LocalPrincipal:
    principal = getattr(connection.state, "local_principal", None)
    if not isinstance(principal, LocalPrincipal):
        raise AuthenticationError(
            "local_operator_authentication_required",
            "A server-authenticated local operator principal is required",
            details={"messages": {"zh-CN": "需要服务端认证的本地操作员 Principal"}},
        )
    return principal


async def accept_local_operator_websocket(websocket: WebSocket) -> None:
    """Accept after dependency authentication, echoing only the fixed protocol marker."""

    proposed = {
        item.strip()
        for item in (websocket.headers.get("sec-websocket-protocol") or "").split(",")
        if item.strip()
    }
    protocol = (
        LOCAL_OPERATOR_WEBSOCKET_PROTOCOL if LOCAL_OPERATOR_WEBSOCKET_PROTOCOL in proposed else None
    )
    await websocket.accept(subprotocol=protocol)


def _local_security(connection: HTTPConnection) -> LocalOperatorSecurity:
    security = getattr(connection.app.state, "local_operator_security", None)
    if not isinstance(security, LocalOperatorSecurity):
        raise AuthenticationError(
            "local_operator_security_unavailable",
            "The local operator security boundary is unavailable",
            details={"messages": {"zh-CN": "本地操作员安全边界不可用"}},
        )
    return security


def _local_operator_token(
    connection: HTTPConnection,
    security: LocalOperatorSecurity,
) -> str | None:
    authorization_values = connection.headers.getlist("authorization")
    if len(authorization_values) > 1:
        raise _local_operator_authentication_failed()
    header_token = _optional_bearer_token(authorization_values[0] if authorization_values else None)
    protocol_token = (
        security.token_from_websocket_protocols(
            ",".join(connection.headers.getlist("sec-websocket-protocol"))
        )
        if connection.scope["type"] == "websocket"
        else None
    )
    if header_token is not None and protocol_token is not None:
        if not _credentials_match(header_token, protocol_token):
            return "\0conflicting-local-operator-credentials"
    return header_token if header_token is not None else protocol_token


def _optional_bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise _local_operator_authentication_failed()
    return token


def _local_operator_authentication_failed() -> AuthenticationError:
    return AuthenticationError(
        "local_operator_authentication_failed",
        "The local operator credential is missing, invalid, or revoked",
        details={"messages": {"zh-CN": "本地操作员凭据缺失、无效或已撤销"}},
    )


def _admin_authentication_failed() -> AuthenticationError:
    return AuthenticationError(
        "admin_authentication_failed",
        "A valid RiftX admin Bearer token is required",
        details={"messages": {"zh-CN": "需要有效的 RiftX 管理员 Bearer 令牌"}},
    )


def _credentials_match(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _required_local_capability(connection: HTTPConnection) -> OperatorCapability:
    from .policy import RouteAuthorization

    return _required_operator_capability(
        connection,
        expected_authorization=RouteAuthorization.LOCAL_OPERATOR,
    )


def _required_admin_capability(connection: HTTPConnection) -> OperatorCapability:
    from .policy import RouteAuthorization

    return _required_operator_capability(
        connection,
        expected_authorization=RouteAuthorization.ADMIN_TOKEN,
    )


def _required_operator_capability(
    connection: HTTPConnection,
    *,
    expected_authorization: object,
) -> OperatorCapability:
    # Imported lazily to keep the policy inventory's dependency audit free of
    # an import cycle while still deriving capability from the server route.
    from .policy import ROUTE_POLICIES, RouteAuthorization, RouteEffect

    route = connection.scope.get("route")
    route_name = getattr(route, "name", None)
    policy = ROUTE_POLICIES.get(route_name)
    if (
        policy is None
        or expected_authorization
        not in {RouteAuthorization.LOCAL_OPERATOR, RouteAuthorization.ADMIN_TOKEN}
        or policy.authorization is not expected_authorization
    ):
        raise _local_operator_policy_denied()
    capability_by_effect = {
        RouteEffect.READ_ONLY: OperatorCapability.READ,
        RouteEffect.DURABLE_WRITE: OperatorCapability.WRITE,
        RouteEffect.WORKFLOW_CONTROL: OperatorCapability.CONTROL,
        RouteEffect.HOST_EXECUTION: OperatorCapability.HOST_EXECUTE,
        RouteEffect.HOST_CONTROL: OperatorCapability.HOST_CONTROL,
    }
    if policy.authorization is RouteAuthorization.ADMIN_TOKEN:
        capability_by_effect = {
            RouteEffect.READ_ONLY: OperatorCapability.READ,
            RouteEffect.DURABLE_WRITE: OperatorCapability.WRITE,
            RouteEffect.WORKFLOW_CONTROL: OperatorCapability.CONTROL,
        }
    try:
        return capability_by_effect[policy.effect]
    except KeyError:
        raise _local_operator_policy_denied() from None


def _local_operator_policy_denied() -> AuthorizationError:
    return AuthorizationError(
        "local_operator_policy_denied",
        "The route policy does not permit local operator access",
        details={"messages": {"zh-CN": "路由策略不允许本地操作员访问"}},
    )
