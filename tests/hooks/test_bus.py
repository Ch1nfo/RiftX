from __future__ import annotations

import asyncio
import json
import sys

import httpx

from riftx.domain import Engagement, Objective, Run
from riftx.hooks import (
    CommandHook,
    HookBus,
    HookDecision,
    HookFailurePolicy,
    HookPoint,
    HookRegistration,
    HookRequest,
    HookResult,
    HTTPHook,
    PythonHook,
    RunEventHookAuditSink,
)
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
)


def request() -> HookRequest:
    return HookRequest(
        id="request-1",
        point=HookPoint.BEFORE_TOOL_EXECUTION,
        run_id="run-1",
        session_id="session-1",
        payload={"target": "127.0.0.1", "timeout": 30},
    )


async def test_decision_priority_and_modify_conflicts_are_deterministic() -> None:
    bus = HookBus()
    bus.register(
        HookRegistration(
            "high-modifier",
            HookPoint.BEFORE_TOOL_EXECUTION,
            PythonHook(
                lambda _: HookResult(
                    decision=HookDecision.MODIFY,
                    modified_payload={"timeout": 10},
                )
            ),
            priority=20,
        )
    )
    bus.register(
        HookRegistration(
            "lower-modifier",
            HookPoint.BEFORE_TOOL_EXECUTION,
            PythonHook(
                lambda _: HookResult(
                    decision=HookDecision.MODIFY,
                    modified_payload={"timeout": 5, "mode": "safe"},
                )
            ),
            priority=10,
        )
    )
    bus.register(
        HookRegistration(
            "approval",
            HookPoint.BEFORE_TOOL_EXECUTION,
            PythonHook(
                lambda _: HookResult(decision=HookDecision.REQUIRE_APPROVAL)
            ),
        )
    )

    outcome = await bus.dispatch(request())

    assert outcome.decision is HookDecision.REQUIRE_APPROVAL
    assert outcome.payload == {"target": "127.0.0.1", "timeout": 10, "mode": "safe"}
    assert [audit.hook_id for audit in outcome.audits] == [
        "high-modifier",
        "lower-modifier",
        "approval",
    ]

    conflicting = HookBus()
    for hook_id, value in (("one", 10), ("two", 20)):
        conflicting.register(
            HookRegistration(
                hook_id,
                HookPoint.BEFORE_TOOL_EXECUTION,
                PythonHook(
                    lambda _, value=value: HookResult(
                        decision=HookDecision.MODIFY,
                        modified_payload={"timeout": value},
                    )
                ),
                priority=10,
            )
        )
    assert (await conflicting.dispatch(request())).decision is HookDecision.BLOCK


async def test_timeout_respects_warn_and_block_failure_policies() -> None:
    async def slow(_: HookRequest) -> HookResult:
        await asyncio.sleep(1)
        return HookResult(decision=HookDecision.CONTINUE)

    warn = HookBus()
    warn.register(
        HookRegistration(
            "warn-timeout",
            HookPoint.BEFORE_TOOL_EXECUTION,
            slow,
            timeout_seconds=0.01,
        )
    )
    blocked = HookBus()
    blocked.register(
        HookRegistration(
            "block-timeout",
            HookPoint.BEFORE_TOOL_EXECUTION,
            slow,
            timeout_seconds=0.01,
            failure_policy=HookFailurePolicy.BLOCK,
        )
    )

    warn_result = await warn.dispatch(request())
    block_result = await blocked.dispatch(request())

    assert warn_result.decision is HookDecision.ABSTAIN
    assert warn_result.audits[0].error is not None
    assert block_result.decision is HookDecision.BLOCK


async def test_command_and_http_adapters_return_validated_results() -> None:
    script = (
        "import json,sys; request=json.load(sys.stdin); "
        "print(json.dumps({'decision':'modify','modified_payload':"
        "{'command_seen': request['point']}}))"
    )
    command = CommandHook([sys.executable, "-c", script])
    command_result = await command(request())

    async def respond(http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={"decision": "continue", "reason": payload["run_id"]},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="http://hook.test",
    ) as client:
        http_result = await HTTPHook("/hook", client=client)(request())

    assert command_result.modified_payload == {"command_seen": "before_tool_execution"}
    assert http_result.reason == "run-1"


async def test_hook_audit_sink_persists_digest_and_modified_fields(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'hooks.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Test hooks"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    events = SQLAlchemyRunEventRepository(database.session_factory)
    bus = HookBus(audit_sink=RunEventHookAuditSink(events))
    bus.register(
        HookRegistration(
            "audited",
            HookPoint.BEFORE_TOOL_EXECUTION,
            PythonHook(
                lambda _: HookResult(
                    decision=HookDecision.MODIFY,
                    modified_payload={"timeout": 5},
                    reason="Bound execution duration",
                )
            ),
        )
    )

    await bus.dispatch(request())
    persisted = list(await events.list_after("run-1"))

    audit_event = persisted[-1]
    assert audit_event.event_type == "runtime.hook_evaluated"
    assert audit_event.payload["hook_id"] == "audited"
    assert audit_event.payload["modified_fields"] == ["timeout"]
    assert len(audit_event.payload["input_digest"]) == 64
    await database.dispose()
