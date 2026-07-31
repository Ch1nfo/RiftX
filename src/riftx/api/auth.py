"""Authentication helpers for Runner and administration endpoints."""

import secrets

from fastapi import Request

from riftx.application.errors import AuthenticationError


def bearer_token(authorization: str | None) -> str:
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise AuthenticationError(
            "runner_authentication_failed",
            "Authorization must use a Runner Bearer token",
        )
    return token


def require_admin_token(request: Request, authorization: str | None) -> None:
    """Require the configured admin token for every management request."""

    configured = request.app.state.control_plane.settings.admin_token
    if not configured:
        raise AuthenticationError(
            "admin_authentication_not_configured",
            "Set RIFTX_ADMIN_TOKEN before using administration endpoints",
        )

    scheme, separator, token = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not token
        or not secrets.compare_digest(token, configured)
    ):
        raise AuthenticationError(
            "admin_authentication_failed",
            "A valid RiftX admin Bearer token is required",
        )
