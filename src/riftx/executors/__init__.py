"""Host-native execution adapters."""

from .environment import merge_environment
from .models import (
    EnvironmentMode,
    ProcessExecutionRequest,
    ProcessResult,
    ShellExecutionRequest,
    ShellKind,
)
from .process import DirectProcessExecutor, ProcessHandle, ProcessStartError
from .shell import ShellExecutor, build_shell_argv

__all__ = [
    "DirectProcessExecutor",
    "EnvironmentMode",
    "ProcessExecutionRequest",
    "ProcessHandle",
    "ProcessResult",
    "ProcessStartError",
    "ShellExecutionRequest",
    "ShellExecutor",
    "ShellKind",
    "build_shell_argv",
    "merge_environment",
]
