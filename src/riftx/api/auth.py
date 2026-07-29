"""Authentication header parsing shared by Runner endpoints."""

from riftx.application.errors import AuthenticationError


def bearer_token(authorization: str | None) -> str:
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise AuthenticationError(
            "runner_authentication_failed",
            "Authorization must use a Runner Bearer token",
        )
    return token
