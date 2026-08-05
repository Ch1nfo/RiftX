from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from riftx.agent.tool_policy import (
    AGENT_TOOL_POLICIES,
    AgentToolAuthorization,
    AgentToolEffect,
    validate_agent_tool_inventory,
    validate_runtime_tool_inventory,
)
from riftx.tools import RESIDENT_TOOL_IDS


@dataclass
class FakeTool:
    name: str
    needs_approval: object = False


def base_tools() -> list[FakeTool]:
    return [
        FakeTool(
            name,
            needs_approval=(lambda: True) if policy.approval_required else False,
        )
        for name, policy in AGENT_TOOL_POLICIES.items()
        if name not in {"port_scan", "run_shell"}
    ]


def test_agent_tool_policy_inventory_covers_effect_and_authorization() -> None:
    validate_agent_tool_inventory(base_tools())

    assert AGENT_TOOL_POLICIES["run_registered_tool"].effect is AgentToolEffect.HOST_EXECUTION
    assert (
        AGENT_TOOL_POLICIES["run_registered_tool"].authorization
        is AgentToolAuthorization.DYNAMIC_APPROVAL
    )
    assert AGENT_TOOL_POLICIES["send_terminal_input"].effect is AgentToolEffect.HOST_CONTROL
    assert (
        AGENT_TOOL_POLICIES["send_terminal_input"].authorization
        is AgentToolAuthorization.RUN_TERMINAL
    )
    assert AGENT_TOOL_POLICIES["open_browser"].approval_required is True
    assert AGENT_TOOL_POLICIES["act_browser"].effect is AgentToolEffect.HOST_CONTROL
    assert AGENT_TOOL_POLICIES["act_browser"].approval_required is True
    assert AGENT_TOOL_POLICIES["observe_browser"].effect is AgentToolEffect.READ_ONLY
    assert AGENT_TOOL_POLICIES["close_browser"].approval_required is False
    assert AGENT_TOOL_POLICIES["web_fetch"].effect is AgentToolEffect.HOST_EXECUTION
    assert AGENT_TOOL_POLICIES["web_fetch"].approval_required is True
    assert AGENT_TOOL_POLICIES["web_search"].effect is AgentToolEffect.HOST_EXECUTION
    assert AGENT_TOOL_POLICIES["web_search"].approval_required is True
    assert AGENT_TOOL_POLICIES["web_research"].effect is AgentToolEffect.HOST_EXECUTION
    assert AGENT_TOOL_POLICIES["web_research"].approval_required is True
    assert AGENT_TOOL_POLICIES["query_http_traffic"].effect is AgentToolEffect.READ_ONLY
    assert AGENT_TOOL_POLICIES["read_http_exchange"].approval_required is False
    assert AGENT_TOOL_POLICIES["target_http_request"].effect is AgentToolEffect.HOST_EXECUTION
    assert AGENT_TOOL_POLICIES["target_http_request"].approval_required is True
    assert set(RESIDENT_TOOL_IDS) <= AGENT_TOOL_POLICIES.keys()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda tools: tools + [FakeTool("unclassified")], "unknown=['unclassified']"),
        (lambda tools: tools[1:], "missing=['list_available_tools']"),
        (lambda tools: tools + [tools[0]], "duplicates=['list_available_tools']"),
        (
            lambda tools: [
                FakeTool(tool.name, False) if tool.name == "run_registered_tool" else tool
                for tool in tools
            ],
            "approval_mismatches=['run_registered_tool']",
        ),
    ],
)
def test_agent_tool_policy_inventory_fails_closed(
    mutation: Callable[[list[FakeTool]], list[FakeTool]],
    expected: str,
) -> None:
    with pytest.raises(RuntimeError, match="policy inventory validation failed") as captured:
        validate_agent_tool_inventory(mutation(base_tools()))

    assert expected in str(captured.value)


def test_runtime_tool_policy_inventory_accepts_residents_and_selected_registry_schema() -> None:
    resident = [
        {
            "name": name,
            "x-riftx": {
                "resident": True,
                "execution_policy": "registered_only",
            },
        }
        for name in RESIDENT_TOOL_IDS
        if name != "run_shell"
    ]
    selected = {
        "name": "scanner",
        "x-riftx": {
            "tool_id": "scanner",
            "execution_type": "process",
            "approval_level": "sensitive",
        },
    }

    validate_runtime_tool_inventory(
        [*resident, selected],
        context_manifest={
            "execution_policy": "registered_only",
            "dynamically_loaded_tools": ["scanner"],
        },
    )


@pytest.mark.parametrize(
    ("schema", "manifest", "expected"),
    [
        (
            {"name": "unknown"},
            {"execution_policy": "registered_only"},
            "unclassified=['unknown']",
        ),
        (
            {
                "name": "scanner",
                "x-riftx": {
                    "tool_id": "scanner",
                    "execution_type": "process",
                    "approval_level": "never",
                },
            },
            {"execution_policy": "registered_only"},
            "forged_dynamic=['scanner']",
        ),
    ],
)
def test_runtime_tool_policy_inventory_rejects_untrusted_schema(
    schema: dict[str, object],
    manifest: dict[str, object],
    expected: str,
) -> None:
    with pytest.raises(RuntimeError, match="Runtime Agent tool policy") as captured:
        validate_runtime_tool_inventory([schema], context_manifest=manifest)

    assert expected in str(captured.value)
