"""Fail-closed policy inventory for every model-visible RiftX Tool."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from riftx.domain import ApprovalLevel, ExecutorType

from .discovery import RESIDENT_TOOL_IDS


class AgentToolEffect(StrEnum):
    READ_ONLY = "read_only"
    HOST_EXECUTION = "host_execution"
    HOST_CONTROL = "host_control"
    DURABLE_WRITE = "durable_write"
    RUN_LIFECYCLE = "run_lifecycle"


class AgentToolAuthorization(StrEnum):
    RUN_CONTEXT = "run_context"
    RUN_TERMINAL = "run_terminal"
    DYNAMIC_APPROVAL = "dynamic_approval"


@dataclass(frozen=True, slots=True)
class AgentToolPolicy:
    effect: AgentToolEffect
    authorization: AgentToolAuthorization
    approval_required: bool = False


AGENT_TOOL_POLICIES = MappingProxyType(
    {
        "list_available_tools": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "search_tools": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "list_tools": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "get_tool": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "search_skills": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "list_skills": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "load_skill": AgentToolPolicy(
            AgentToolEffect.DURABLE_WRITE,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "load_skill_references": AgentToolPolicy(
            AgentToolEffect.DURABLE_WRITE,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "unload_skill": AgentToolPolicy(
            AgentToolEffect.DURABLE_WRITE,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "list_files": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "read_file": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "read_many_files": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "grep": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "glob": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "symbol_search": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "find_references": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "call_hierarchy": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "diagnostics": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "apply_patch": AgentToolPolicy(
            AgentToolEffect.DURABLE_WRITE,
            AgentToolAuthorization.DYNAMIC_APPROVAL,
            approval_required=True,
        ),
        "revert_patch": AgentToolPolicy(
            AgentToolEffect.DURABLE_WRITE,
            AgentToolAuthorization.DYNAMIC_APPROVAL,
            approval_required=True,
        ),
        "git_status": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "git_diff": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "git_log": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "run_registered_tool": AgentToolPolicy(
            AgentToolEffect.HOST_EXECUTION,
            AgentToolAuthorization.DYNAMIC_APPROVAL,
            approval_required=True,
        ),
        "port_scan": AgentToolPolicy(
            AgentToolEffect.HOST_EXECUTION,
            AgentToolAuthorization.DYNAMIC_APPROVAL,
            approval_required=True,
        ),
        "run_shell": AgentToolPolicy(
            AgentToolEffect.HOST_EXECUTION,
            AgentToolAuthorization.DYNAMIC_APPROVAL,
            approval_required=True,
        ),
        "open_terminal": AgentToolPolicy(
            AgentToolEffect.HOST_EXECUTION,
            AgentToolAuthorization.DYNAMIC_APPROVAL,
            approval_required=True,
        ),
        "read_terminal": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_TERMINAL,
        ),
        "send_terminal_input": AgentToolPolicy(
            AgentToolEffect.HOST_CONTROL,
            AgentToolAuthorization.RUN_TERMINAL,
        ),
        "close_terminal": AgentToolPolicy(
            AgentToolEffect.HOST_CONTROL,
            AgentToolAuthorization.RUN_TERMINAL,
        ),
        "get_execution": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "wait_execution": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "cancel_execution": AgentToolPolicy(
            AgentToolEffect.HOST_CONTROL,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "read_artifact": AgentToolPolicy(
            AgentToolEffect.READ_ONLY,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "delegate": AgentToolPolicy(
            AgentToolEffect.RUN_LIFECYCLE,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "create_finding": AgentToolPolicy(
            AgentToolEffect.DURABLE_WRITE,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "add_artifact": AgentToolPolicy(
            AgentToolEffect.DURABLE_WRITE,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "update_plan": AgentToolPolicy(
            AgentToolEffect.DURABLE_WRITE,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
        "complete_run": AgentToolPolicy(
            AgentToolEffect.RUN_LIFECYCLE,
            AgentToolAuthorization.RUN_CONTEXT,
        ),
    }
)

_REQUIRED_LEGACY_AGENT_TOOLS = frozenset(
    {
        "list_available_tools",
        "run_registered_tool",
        "open_terminal",
        "read_terminal",
        "send_terminal_input",
        "close_terminal",
        "create_finding",
        "add_artifact",
        "update_plan",
        "complete_run",
    }
)


class AgentToolLike(Protocol):
    name: str
    needs_approval: object


def validate_agent_tool_inventory(tools: list[AgentToolLike]) -> None:
    """Reject missing, duplicate, or unclassified legacy model-visible tools."""

    names = [tool.name for tool in tools]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    unknown = sorted(set(names) - AGENT_TOOL_POLICIES.keys())
    missing = sorted(_REQUIRED_LEGACY_AGENT_TOOLS - set(names))
    approval_mismatches = sorted(
        tool.name
        for tool in tools
        if (
            (policy := AGENT_TOOL_POLICIES.get(tool.name)) is not None
            and policy.approval_required
            and not tool.needs_approval
        )
    )
    if duplicates or unknown or missing or approval_mismatches:
        raise RuntimeError(
            "Agent tool policy inventory validation failed: "
            f"duplicates={duplicates}, unknown={unknown}, missing={missing}, "
            f"approval_mismatches={approval_mismatches}"
        )


def validate_runtime_tool_inventory(
    schemas: list[dict[str, object]],
    *,
    context_manifest: Mapping[str, object],
) -> None:
    """Reject unclassified or forged schemas on the production Runtime path."""

    names = [schema.get("name") for schema in schemas]
    invalid_names = sorted(repr(name) for name in names if not isinstance(name, str) or not name)
    string_names = [name for name in names if isinstance(name, str) and name]
    duplicates = sorted({name for name in string_names if string_names.count(name) > 1})
    missing_policies = sorted(set(RESIDENT_TOOL_IDS) - AGENT_TOOL_POLICIES.keys())
    selected = _string_set(context_manifest.get("dynamically_loaded_tools"))
    execution_policy = str(context_manifest.get("execution_policy") or "registered_only")
    unclassified: list[str] = []
    forged_dynamic: list[str] = []
    invalid_shell: list[str] = []

    for schema in schemas:
        name = schema.get("name")
        if not isinstance(name, str) or not name:
            continue
        metadata = schema.get("x-riftx")
        if name in RESIDENT_TOOL_IDS:
            if name not in AGENT_TOOL_POLICIES:
                unclassified.append(name)
                continue
            if not isinstance(metadata, dict) or metadata.get("resident") is not True:
                forged_dynamic.append(name)
                continue
            if name == "run_shell" and (
                execution_policy != "open" or metadata.get("execution_policy") != "open"
            ):
                invalid_shell.append(name)
            continue

        if not isinstance(metadata, dict):
            unclassified.append(name)
            continue
        if (
            metadata.get("tool_id") != name
            or metadata.get("execution_type") not in {item.value for item in ExecutorType}
            or metadata.get("approval_level") not in {item.value for item in ApprovalLevel}
            or name not in selected
        ):
            forged_dynamic.append(name)

    if (
        invalid_names
        or duplicates
        or missing_policies
        or unclassified
        or forged_dynamic
        or invalid_shell
    ):
        raise RuntimeError(
            "Runtime Agent tool policy inventory validation failed: "
            f"invalid_names={invalid_names}, duplicates={duplicates}, "
            f"missing_policies={missing_policies}, unclassified={sorted(unclassified)}, "
            f"forged_dynamic={sorted(forged_dynamic)}, invalid_shell={sorted(invalid_shell)}"
        )


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}
