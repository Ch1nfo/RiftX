"""Stable enumerations shared across the RiftX domain."""

from enum import StrEnum


class RunKind(StrEnum):
    GENERAL = "general"
    PENTEST = "pentest"
    CODE_AUDIT = "code_audit"


class PentestProhibitedAction(StrEnum):
    DENIAL_OF_SERVICE = "denial_of_service"
    DESTRUCTIVE_DATA_MODIFICATION = "destructive_data_modification"
    PERSISTENCE = "persistence"
    OUT_OF_SCOPE_LATERAL_MOVEMENT = "out_of_scope_lateral_movement"


class PentestStopCondition(StrEnum):
    SCOPE_VIOLATION = "scope_violation"
    SCOPE_WINDOW_EXPIRED = "scope_window_expired"
    BUDGET_EXHAUSTED = "budget_exhausted"
    OPERATOR_STOP = "operator_stop"
    RUN_CANCELLED = "run_cancelled"


class RunStatus(StrEnum):
    CREATED = "created"
    INITIALIZING = "initializing"
    READY = "ready"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER = "waiting_user"
    PAUSING = "pausing"
    PAUSED = "paused"
    COMPACTING = "compacting"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"

    # Compatibility state used by the completed V2 control-plane workflow. New
    # Agent Runtime code uses INITIALIZING -> READY instead.
    PREPARING = "preparing"


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    HARD_TIMEOUT = "hard_timeout"
    LOST = "lost"

    # V2 compatibility states. New Agent Runtime executions use QUEUED/COMPLETED.
    CREATED = "created"
    EXITED = "exited"


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


class ApprovalDecision(StrEnum):
    APPROVE_ONCE = "approve_once"
    APPROVE_TOOL_FOR_RUN = "approve_tool_for_run"
    REJECT = "reject"
    REJECT_WITH_FEEDBACK = "reject_with_feedback"


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


class RunnerCommandKind(StrEnum):
    EXECUTE = "execute"
    CANCEL = "cancel"
    TARGET_HTTP = "target_http"
    TARGET_HTTP_CANCEL = "target_http_cancel"
    BROWSER = "browser"
    BROWSER_CLOSE = "browser_close"
    TERMINAL_START = "terminal_start"
    TERMINAL_WRITE = "terminal_write"
    TERMINAL_RESIZE = "terminal_resize"
    TERMINAL_INTERRUPT = "terminal_interrupt"
    TERMINAL_CLOSE = "terminal_close"


class RunnerCommandStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"


class RunnerCommandOwnershipState(StrEnum):
    """Durable admission state for one Runner command ownership binding."""

    VERIFIED = "verified"
    QUARANTINED = "quarantined"


class RunnerCommandOrigin(StrEnum):
    """Effect producers allowed to create durable Runner commands."""

    APPLICATION_SERVICE = "application_service"
    TEMPORAL_WORKER = "temporal_worker"
    CONTROL_PLANE_RECONCILER = "control_plane_reconciler"
    WORKER_RECONCILER = "worker_reconciler"
    SAFETY_RECONCILER = "safety_reconciler"


class RunnerOperationFamily(StrEnum):
    """Typed effect families carried by Runner ownership envelopes."""

    EXECUTION = "execution"
    TERMINAL = "terminal"
    BROWSER = "browser"
    TARGET_HTTP = "target_http"
    CONNECTOR = "connector"
    SAFETY_STOP = "safety_stop"


class RunnerResourceKind(StrEnum):
    """Authoritative resource roots a Runner command may bind."""

    EXECUTION = "execution"
    TERMINAL_SESSION = "terminal_session"
    BROWSER_SESSION = "browser_session"
    TARGET_HTTP_INTENT = "target_http_intent"
    CONNECTOR_SESSION = "connector_session"


class BrowserMode(StrEnum):
    MANAGED_EPHEMERAL = "managed_ephemeral"
    MANAGED_PERSISTENT = "managed_persistent"
    ATTACHED_CDP = "attached_cdp"


class BrowserSessionStatus(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    ACTIVE = "active"
    CLOSED = "closed"
    LOST = "lost"


class BrowserOwner(StrEnum):
    AGENT = "agent"
    USER = "user"
    SHARED_READ_ONLY = "shared_read_only"


class BrowserPageStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class BrowserActionType(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    TYPE = "type"
    SELECT = "select"
    PRESS = "press"
    SCROLL = "scroll"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    WAIT = "wait"
    EVALUATE = "evaluate"
    GO_BACK = "go_back"
    RELOAD = "reload"


class BrowserActionStatus(StrEnum):
    PROPOSED = "proposed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


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
    SHARED_READ_ONLY = "shared_read_only"
    # Legacy V2 value retained for persisted rows; both shared modes are read-only.
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


class MessageType(StrEnum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT_REFERENCE = "tool_result_reference"
    SUBAGENT_DELEGATION = "subagent_delegation"
    SUBAGENT_RESULT = "subagent_result"
    APPROVAL = "approval"
    CHECKPOINT_BOUNDARY = "checkpoint_boundary"


class MessageVisibility(StrEnum):
    USER_VISIBLE = "user_visible"
    AGENT_ONLY = "agent_only"
    INTERNAL_STATE = "internal_state"
    SUBAGENT_PRIVATE = "subagent_private"


class TrustProfile(StrEnum):
    """Explicit deployment trust boundary selected by the operator."""

    LOCAL_SINGLE_OPERATOR = "local_single_operator"
    REMOTE_MULTIUSER = "remote_multiuser"


class OperatorCapability(StrEnum):
    """Server-owned capabilities for the local single-operator principal."""

    READ = "local.read"
    WRITE = "local.write"
    CONTROL = "local.control"
    HOST_EXECUTE = "local.host.execute"
    HOST_CONTROL = "local.host.control"
