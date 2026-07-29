"""Strict YAML loading for Tool Registry configuration."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import ToolRegistryConfig


class ToolConfigError(ValueError):
    """Raised when tools.yaml cannot be parsed or validated."""


def load_tool_config(path: Path) -> ToolRegistryConfig:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ToolConfigError(f"could not read tool config {path}: {exc}") from exc
    return parse_tool_config(content, source=str(path))


def parse_tool_config(content: bytes | str, *, source: str = "<memory>") -> ToolRegistryConfig:
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ToolConfigError(f"invalid YAML in {source}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ToolConfigError(f"tool config {source} must contain a mapping")
    try:
        return ToolRegistryConfig.model_validate(raw)
    except ValidationError as exc:
        raise ToolConfigError(f"invalid tool config {source}: {exc}") from exc
