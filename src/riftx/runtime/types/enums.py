"""Stable, provider-neutral Agent Runtime enumerations."""

from enum import StrEnum


class SessionStatus(StrEnum):
    CREATED = "created"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    COMPACTING = "compacting"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CycleStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    YIELDED = "yielded"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class YieldReason(StrEnum):
    TOOL_RUNNING = "tool_running"
    TERMINAL_OPEN = "terminal_open"
    APPROVAL_REQUIRED = "approval_required"
    USER_INPUT_REQUIRED = "user_input_required"
    SUBAGENT_RUNNING = "subagent_running"
    COMPACTION_REQUIRED = "compaction_required"
    CYCLE_LIMIT_REACHED = "cycle_limit_reached"
    RUN_COMPLETED = "run_completed"
    RUN_PAUSED = "run_paused"
    RUN_CANCELLED = "run_cancelled"
    RETRYABLE_FAILURE = "retryable_failure"
    FATAL_FAILURE = "fatal_failure"


class AgentStepType(StrEnum):
    CONTEXT_COMPILE = "context_compile"
    MODEL_CALL = "model_call"
    ASSISTANT_MESSAGE = "assistant_message"
    PLAN_UPDATE = "plan_update"
    TOOL_PROPOSAL = "tool_proposal"
    APPROVAL_REQUEST = "approval_request"
    TOOL_EXECUTION = "tool_execution"
    TOOL_RESULT_PROCESS = "tool_result_process"
    SUBAGENT_DELEGATION = "subagent_delegation"
    SUBAGENT_RESULT = "subagent_result"
    MEMORY_UPDATE = "memory_update"
    COMPACTION = "compaction"
    USER_INPUT_WAIT = "user_input_wait"
    RUN_COMPLETION = "run_completion"


class AgentDirectiveType(StrEnum):
    RESPOND = "respond"
    CALL_TOOL = "call_tool"
    RUN_SHELL = "run_shell"
    OPEN_TERMINAL = "open_terminal"
    DELEGATE = "delegate"
    UPDATE_PLAN = "update_plan"
    ASK_USER = "ask_user"
    CREATE_FINDING = "create_finding"
    COMPLETE_RUN = "complete_run"


class ToolCallStatus(StrEnum):
    PROPOSED = "proposed"
    WAITING_APPROVAL = "waiting_approval"
    READY = "ready"
    EXECUTING = "executing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
