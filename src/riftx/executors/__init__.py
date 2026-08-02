"""Host-native execution adapters."""

from .containment import (
    LinuxCgroupV2Containment,
    LinuxCgroupV2Manager,
    ProcessContainmentError,
    ProcessContainmentTerminationError,
    ProcessContainmentUnavailableError,
)
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
from .process import (
    DirectProcessExecutor,
    ProcessHandle,
    ProcessStartError,
    ProcessTreeTerminationError,
    UnconfirmedProcessStartError,
    UnverifiedProcessTreeTerminationError,
)
from .shell import ShellExecutor, build_shell_argv

__all__ = [
    "DirectProcessExecutor",
    "EnvironmentMode",
    "LinuxCgroupV2Containment",
    "LinuxCgroupV2Manager",
    "PowerShellEdition",
    "PowerShellExecutable",
    "PowerShellExecutor",
    "PowerShellNotFoundError",
    "PowerShellResolver",
    "ProcessExecutionRequest",
    "ProcessHandle",
    "ProcessResult",
    "ProcessContainmentError",
    "ProcessContainmentTerminationError",
    "ProcessContainmentUnavailableError",
    "ProcessStartError",
    "ProcessTreeTerminationError",
    "ShellExecutionRequest",
    "ShellExecutor",
    "ShellKind",
    "UnconfirmedProcessStartError",
    "UnverifiedProcessTreeTerminationError",
    "build_powershell_argv",
    "build_shell_argv",
    "merge_environment",
]
