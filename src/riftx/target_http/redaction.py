"""Minimal, value-free URL metadata for Target HTTP audit/read surfaces."""

from __future__ import annotations

from urllib.parse import urlsplit

_MAX_URL_INPUT = 16 * 1024
_MAX_ORIGIN = 512
_MAX_PATH_SEGMENTS = 4096


def safe_url_metadata(value: str) -> dict[str, object] | None:
    """Return an origin and path shape without userinfo, path values, query, or fragment."""

    if not isinstance(value, str) or not value or len(value) > _MAX_URL_INPUT:
        return None
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if scheme not in {"http", "https"} or not hostname:
        return None
    try:
        normalized_host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if not normalized_host:
        return None
    rendered_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    origin = f"{scheme}://{rendered_host}"
    default_port = 443 if scheme == "https" else 80
    if port is not None and port != default_port:
        origin = f"{origin}:{port}"
    if len(origin) > _MAX_ORIGIN:
        return None
    segment_count = len([segment for segment in parsed.path.split("/") if segment])
    if segment_count > _MAX_PATH_SEGMENTS:
        return None
    return {
        "scheme": scheme,
        "origin": origin,
        "path_shape": "/…" if segment_count else "/",
        "path_segment_count": segment_count,
    }


def safe_redirect_metadata(values: list[str]) -> dict[str, object] | None:
    if len(values) > 10:
        return None
    origins: list[str] = []
    for value in values:
        metadata = safe_url_metadata(value)
        if metadata is None:
            return None
        origin = metadata["origin"]
        if not isinstance(origin, str):
            return None
        origins.append(origin)
    return {"count": len(origins), "origins": origins}


__all__ = ["safe_redirect_metadata", "safe_url_metadata"]
