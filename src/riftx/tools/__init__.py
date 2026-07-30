"""Tool configuration, detection, and capability lookup."""

from .adapters import (
    ToolOutputParseError,
    parse_generic_json,
    parse_masscan_json,
    parse_nmap_xml,
    parse_nuclei_jsonl,
    parse_tool_output,
)
from .config import ToolConfigError, load_tool_config, parse_tool_config
from .discovery import (
    RESIDENT_TOOL_IDS,
    DynamicToolIndex,
    ToolContextManager,
    ToolDetail,
    ToolIndexEntry,
    ToolSchema,
    ToolSearchRequest,
    ToolSearchResult,
    ToolSelection,
    ToolVisibilitySnapshot,
)
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
    "RESIDENT_TOOL_IDS",
    "DynamicToolIndex",
    "ToolContextManager",
    "ToolDetail",
    "ToolIndexEntry",
    "ToolSchema",
    "ToolSelection",
    "ToolSearchRequest",
    "ToolSearchResult",
    "ToolVisibilitySnapshot",
    "PlatformShells",
    "RawToolDefinition",
    "ShellConfig",
    "ToolConfigError",
    "ToolDefinition",
    "ToolNotFoundError",
    "ToolOutputConfig",
    "ToolOutputParseError",
    "ToolRegistry",
    "ToolRegistryConfig",
    "ToolSnapshot",
    "ToolUnavailableError",
    "VersionProbe",
    "load_tool_config",
    "parse_generic_json",
    "parse_masscan_json",
    "parse_nmap_xml",
    "parse_nuclei_jsonl",
    "parse_tool_output",
    "parse_tool_config",
]
