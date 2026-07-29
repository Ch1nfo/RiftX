"""Tool configuration, detection, and capability lookup."""

from .config import ToolConfigError, load_tool_config, parse_tool_config
from .models import (
    ExecutionPolicy,
    PlatformShells,
    RawToolDefinition,
    ShellConfig,
    ToolDefinition,
    ToolOutputConfig,
    ToolRegistryConfig,
    ToolSnapshot,
    VersionProbe,
)
from .registry import ToolNotFoundError, ToolRegistry, ToolUnavailableError

__all__ = [
    "ExecutionPolicy",
    "PlatformShells",
    "RawToolDefinition",
    "ShellConfig",
    "ToolConfigError",
    "ToolDefinition",
    "ToolNotFoundError",
    "ToolOutputConfig",
    "ToolRegistry",
    "ToolRegistryConfig",
    "ToolSnapshot",
    "ToolUnavailableError",
    "VersionProbe",
    "load_tool_config",
    "parse_tool_config",
]
