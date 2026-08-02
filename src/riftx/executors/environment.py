"""Deterministic environment construction for host executions."""

from __future__ import annotations

import os
from collections.abc import Mapping

from .models import EnvironmentMode

_PRIVATE_EXACT_NAMES = frozenset(
    {
        "DATABASE_URL",
        "DOCKER_HOST",
        "GPG_AGENT_INFO",
        "KUBECONFIG",
        "SSH_AGENT_PID",
        "SSH_AUTH_SOCK",
    }
)
_PRIVATE_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_OPENAI_",
    "CLAUDE_",
    "CODEX_",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "OPENAI_",
    "RIFTX_",
    "TEMPORAL_",
)
_PRIVATE_SUFFIXES = (
    "_ACCESS_KEY",
    "_API_KEY",
    "_AUTH_TOKEN",
    "_CREDENTIAL",
    "_CREDENTIALS",
    "_PASSWORD",
    "_PRIVATE_KEY",
    "_SECRET",
    "_SESSION_TOKEN",
    "_TOKEN",
)


def merge_environment(
    *layers: Mapping[str, str | None],
    mode: EnvironmentMode = EnvironmentMode.INHERIT,
    host_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge environment layers from lowest to highest precedence.

    A ``None`` value explicitly removes a variable inherited from an earlier layer.
    """

    if mode is EnvironmentMode.INHERIT:
        inherited = host_environment if host_environment is not None else os.environ
        # Tool processes need ordinary host ergonomics such as PATH and locale,
        # but never receive Control Plane/model credentials merely because the
        # Runner inherited them.  A higher, explicit layer can intentionally
        # re-add a required value for one registered tool.
        environment = {
            key: value
            for key, value in inherited.items()
            if not _is_private_inherited_name(key)
        }
    else:
        environment = {}

    for layer in layers:
        for key, value in layer.items():
            if not key or "=" in key or "\x00" in key:
                raise ValueError(f"invalid environment variable name: {key!r}")
            if value is None:
                environment.pop(key, None)
            else:
                if "\x00" in value:
                    raise ValueError(f"environment variable {key!r} contains a null byte")
                environment[key] = value
    return environment


def _is_private_inherited_name(name: str) -> bool:
    normalized = name.upper()
    return (
        normalized in _PRIVATE_EXACT_NAMES
        or normalized.startswith(_PRIVATE_PREFIXES)
        or normalized.endswith(_PRIVATE_SUFFIXES)
    )
