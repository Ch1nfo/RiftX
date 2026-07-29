"""Deterministic environment construction for host executions."""

from __future__ import annotations

import os
from collections.abc import Mapping

from .models import EnvironmentMode


def merge_environment(
    *layers: Mapping[str, str | None],
    mode: EnvironmentMode = EnvironmentMode.INHERIT,
    host_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge environment layers from lowest to highest precedence.

    A ``None`` value explicitly removes a variable inherited from an earlier layer.
    """

    if mode is EnvironmentMode.INHERIT:
        environment = dict(host_environment if host_environment is not None else os.environ)
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
