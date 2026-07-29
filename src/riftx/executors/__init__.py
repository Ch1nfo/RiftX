"""Host-native execution adapters."""

from .environment import merge_environment
from .models import (
    EnvironmentMode,
    ProcessExecutionRequest,
    ProcessResult,
    ShellExecutionRequest,
    ShellKind,
)
from .powershell import (
    PowerShellEdition,
    PowerShellExecutable,
    PowerShellExecutor,
    PowerShellNotFoundError,
    PowerShellResolver,
    build_powershell_argv,
)
from .process import DirectProcessExecutor, ProcessHandle, ProcessStartError
from .shell import ShellExecutor, build_shell_argv

__all__ = [
    "DirectProcessExecutor",
    "EnvironmentMode",
    "PowerShellEdition",
    "PowerShellExecutable",
    "PowerShellExecutor",
    "PowerShellNotFoundError",
    "PowerShellResolver",
    "ProcessExecutionRequest",
    "ProcessHandle",
    "ProcessResult",
    "ProcessStartError",
    "ShellExecutionRequest",
    "ShellExecutor",
    "ShellKind",
    "build_powershell_argv",
    "build_shell_argv",
    "merge_environment",
]
