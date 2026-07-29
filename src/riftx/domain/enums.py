"""Stable enumerations shared across the RiftX domain."""

from enum import StrEnum


class RunStatus(StrEnum):
    CREATED = "created"
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionStatus(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"


class ApprovalMode(StrEnum):
    AUTO = "auto"
    BALANCED = "balanced"
    MANUAL = "manual"


class ApprovalLevel(StrEnum):
    NEVER = "never"
    SENSITIVE = "sensitive"
    ALWAYS = "always"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ToolAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MISCONFIGURED = "misconfigured"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class ExecutorType(StrEnum):
    PROCESS = "process"
    SHELL = "shell"
    PTY = "pty"


class NodeStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    LOST = "lost"
    UNKNOWN = "unknown"


class AgentStepStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class TerminalStatus(StrEnum):
    CREATED = "created"
    OPEN = "open"
    CLOSED = "closed"
    LOST = "lost"


class TerminalOwner(StrEnum):
    AGENT = "agent"
    USER = "user"
    SHARED = "shared"


class FindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"


class ReportFormat(StrEnum):
    MARKDOWN = "markdown"
    HTML = "html"
    JSON = "json"


class EntryPointKind(StrEnum):
    CIDR = "cidr"
    IP = "ip"
    DOMAIN = "domain"
    URL = "url"
    FILE = "file"
    TEXT = "text"


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
