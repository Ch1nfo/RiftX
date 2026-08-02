import { describe, expect, it } from "vitest";

import type { RunEvent } from "../api/types";
import { reduceRunEvents } from "./runStreamReducer";

function engineEvent(
  sequence: number,
  eventType: string,
  data: Record<string, unknown>,
  cycleId = "cycle-1",
  engineSequence = sequence,
): RunEvent {
  return {
    id: `event-${sequence}-${eventType}`,
    run_id: "run-1",
    sequence,
    event_type: "runtime.engine_event",
    payload: {
      cycle_id: cycleId,
      engine_sequence: engineSequence,
      event_type: eventType,
      data,
    },
    created_at: `2026-07-31T02:35:${String(sequence).padStart(2, "0")}Z`,
  };
}

function runEvent(
  sequence: number,
  eventType: string,
  payload: Record<string, unknown>,
): RunEvent {
  return {
    id: `event-${sequence}-${eventType}`,
    run_id: "run-1",
    sequence,
    event_type: eventType,
    payload,
    created_at: `2026-07-31T02:35:${String(sequence).padStart(2, "0")}Z`,
  };
}

describe("reduceRunEvents", () => {
  it("keeps one assistant message when high-level events interrupt the token stream", () => {
    const projection = reduceRunEvents([
      engineEvent(19, "assistant_delta", { delta: "主" }, "cycle-1", 9),
      runEvent(20, "run.status_changed", { from: "preparing", to: "running" }),
      engineEvent(21, "assistant_delta", { delta: "代理。" }, "cycle-1", 10),
      engineEvent(22, "assistant_delta", { delta: "我现在开始。" }, "cycle-1", 11),
    ]);

    expect(projection.conversationMessages).toHaveLength(1);
    expect(projection.conversationMessages[0]).toMatchObject({
      role: "assistant",
      content: "主代理。我现在开始。",
      startSequence: 19,
      endSequence: 22,
    });
    expect(projection.highLevelTimeline.map((item) => item.kind)).toEqual([
      "stream",
      "event",
    ]);
    expect(projection.highLevelTimeline[0]).toMatchObject({
      kind: "stream",
      content: "主代理。我现在开始。",
      startSequence: 19,
      endSequence: 22,
    });
  });

  it("deduplicates durable and engine sequences without losing raw audit events", () => {
    const first = engineEvent(1, "assistant_delta", { delta: "A" }, "cycle-1", 1);
    const duplicateDurable = {
      ...engineEvent(1, "assistant_delta", { delta: "ignored" }, "cycle-1", 99),
      id: "duplicate-durable-sequence",
    };
    const projection = reduceRunEvents([
      first,
      duplicateDurable,
      engineEvent(2, "assistant_delta", { delta: "ignored" }, "cycle-1", 1),
      engineEvent(3, "assistant_delta", { delta: "C" }, "cycle-1", 2),
    ]);

    expect(projection.conversationMessages[0]?.content).toBe("AC");
    expect(projection.rawEvents.map((event) => event.sequence)).toEqual([1, 2, 3]);
    expect(projection.rawEvents[1]?.payload.engine_sequence).toBe(1);
  });

  it("uses accumulated and final content as authoritative snapshots", () => {
    const projection = reduceRunEvents([
      engineEvent(1, "assistant_delta", { delta: "主" }, "cycle-1", 1),
      engineEvent(
        2,
        "assistant_delta",
        { delta: "代理", accumulated: "主代理" },
        "cycle-1",
        2,
      ),
      engineEvent(
        3,
        "assistant_message",
        { content: [{ type: "output_text", text: "最终回复" }] },
        "cycle-1",
        3,
      ),
    ]);

    expect(projection.conversationMessages[0]?.content).toBe("最终回复");
    expect(projection.highLevelTimeline[0]).toMatchObject({
      kind: "stream",
      content: "最终回复",
      chunkCount: 3,
    });
  });

  it("folds a durable agent.message final into its streamed response", () => {
    const projection = reduceRunEvents([
      engineEvent(1, "assistant_delta", { delta: "draft " }, "cycle-1", 1),
      engineEvent(2, "assistant_delta", { delta: "reply" }, "cycle-1", 2),
      runEvent(3, "agent.message", {
        agent_step_id: "cycle-1",
        message: "Final reply",
      }),
    ]);

    expect(projection.conversationMessages).toHaveLength(1);
    expect(projection.conversationMessages[0]).toMatchObject({
      role: "assistant",
      content: "Final reply",
      startSequence: 1,
      endSequence: 3,
    });
    expect(projection.highLevelTimeline).toHaveLength(1);
    expect(projection.highLevelTimeline[0]).toMatchObject({
      kind: "stream",
      content: "Final reply",
      startSequence: 1,
      endSequence: 3,
    });
    expect(projection.rawEvents.map((event) => event.sequence)).toEqual([1, 2, 3]);
  });

  it("deduplicates an exact durable final even when transitional IDs differ", () => {
    const projection = reduceRunEvents([
      engineEvent(1, "assistant_delta", { delta: "One reply" }, "runtime-cycle", 1),
      runEvent(2, "agent.message", {
        agent_step_id: "legacy-step",
        message: "One reply",
      }),
    ]);

    expect(projection.conversationMessages).toEqual([
      expect.objectContaining({
        role: "assistant",
        content: "One reply",
        startSequence: 1,
        endSequence: 2,
      }),
    ]);
    expect(projection.highLevelTimeline).toHaveLength(1);
    expect(projection.rawEvents).toHaveLength(2);
  });

  it("keeps a durable reply whose explicit cycle has no matching stream", () => {
    const projection = reduceRunEvents([
      engineEvent(1, "assistant_delta", { delta: "old reply" }, "cycle-1", 1),
      engineEvent(2, "assistant_message", { content: "old reply" }, "cycle-1", 2),
      runEvent(3, "agent.message", {
        agent_step_id: "cycle-2",
        message: "new reply",
      }),
    ]);

    expect(projection.conversationMessages).toEqual([
      expect.objectContaining({
        role: "assistant",
        content: "old reply",
        startSequence: 1,
        endSequence: 2,
      }),
      expect.objectContaining({
        role: "assistant",
        content: "new reply",
        startSequence: 3,
        endSequence: 3,
      }),
    ]);
    expect(projection.highLevelTimeline).toEqual([
      expect.objectContaining({
        kind: "stream",
        cycleId: "cycle-1",
        content: "old reply",
        endSequence: 2,
      }),
      expect.objectContaining({
        kind: "event",
        event: expect.objectContaining({
          event_type: "agent.message",
          payload: expect.objectContaining({ agent_step_id: "cycle-2" }),
        }),
      }),
    ]);
  });

  it("finalizes the current stream before an older equal reply in the same cycle", () => {
    const projection = reduceRunEvents([
      engineEvent(1, "assistant_delta", { delta: "Same reply" }, "cycle-1", 1),
      runEvent(2, "agent.message", {
        agent_step_id: "cycle-1",
        message: "Same reply",
      }),
      engineEvent(3, "assistant_delta", { delta: "draft" }, "cycle-1", 3),
      runEvent(4, "agent.message", {
        agent_step_id: "cycle-1",
        message: "Same reply",
      }),
    ]);

    expect(projection.conversationMessages).toEqual([
      expect.objectContaining({ content: "Same reply", startSequence: 1, endSequence: 2 }),
      expect.objectContaining({ content: "Same reply", startSequence: 3, endSequence: 4 }),
    ]);
    expect(
      projection.conversationMessages.some((message) => message.content === "draft"),
    ).toBe(false);
  });

  it("does not reconcile an explicitly correlated final into another active cycle", () => {
    const projection = reduceRunEvents([
      engineEvent(1, "assistant_delta", { delta: "cycle one" }, "cycle-1", 1),
      runEvent(2, "agent.message", {
        agent_step_id: "cycle-1",
        message: "cycle one",
      }),
      engineEvent(3, "assistant_delta", { delta: "cycle two draft" }, "cycle-2", 1),
      runEvent(4, "agent.message", {
        agent_step_id: "cycle-1",
        message: "cycle one",
      }),
    ]);

    expect(projection.conversationMessages).toEqual([
      expect.objectContaining({ content: "cycle one", endSequence: 4 }),
      expect.objectContaining({ content: "cycle two draft", endSequence: 3 }),
    ]);
  });

  it("keeps plan updates in the timeline instead of presenting them as replies", () => {
    const projection = reduceRunEvents([
      runEvent(1, "user.message_queued", { message: "Inspect the endpoint" }),
      runEvent(2, "agent.plan_updated", { plan_summary: "First inspect, then verify." }),
    ]);

    expect(projection.conversationMessages).toEqual([
      expect.objectContaining({ role: "user", content: "Inspect the endpoint" }),
    ]);
    expect(projection.highLevelTimeline).toHaveLength(2);
    expect(projection.highLevelTimeline[1]).toMatchObject({
      kind: "event",
      event: { event_type: "agent.plan_updated" },
    });
  });

  it("keeps an interleaved plan separate while folding the final assistant message", () => {
    const projection = reduceRunEvents([
      engineEvent(1, "assistant_delta", { delta: "Working " }, "cycle-1", 1),
      runEvent(2, "agent.plan_updated", {
        agent_step_id: "cycle-1",
        plan_summary: "Inspect, then verify.",
      }),
      engineEvent(3, "assistant_delta", { delta: "now." }, "cycle-1", 2),
      runEvent(4, "agent.message", {
        agent_step_id: "cycle-1",
        message: "Working now.",
      }),
    ]);

    expect(projection.conversationMessages).toEqual([
      expect.objectContaining({ role: "assistant", content: "Working now.", endSequence: 4 }),
    ]);
    expect(projection.highLevelTimeline).toEqual([
      expect.objectContaining({ kind: "stream", content: "Working now." }),
      expect.objectContaining({
        kind: "event",
        event: expect.objectContaining({ event_type: "agent.plan_updated" }),
      }),
    ]);
  });

  it("keeps cycles and explicit streams isolated while allowing interleaving", () => {
    const projection = reduceRunEvents([
      engineEvent(1, "assistant_delta", { stream_id: "stream-a", delta: "A1" }),
      engineEvent(2, "assistant_delta", { stream_id: "stream-b", delta: "B1" }),
      engineEvent(3, "assistant_delta", { stream_id: "stream-a", delta: "A2" }),
      engineEvent(4, "assistant_delta", { delta: "C1" }, "cycle-2", 1),
    ]);

    expect(projection.conversationMessages.map((message) => message.content)).toEqual([
      "A1A2",
      "B1",
      "C1",
    ]);
    expect(projection.highLevelTimeline).toHaveLength(3);
  });

  it("uses assistant message boundaries as fallback stream ordinals", () => {
    const projection = reduceRunEvents([
      engineEvent(1, "assistant_delta", { delta: "first" }, "cycle-1", 1),
      engineEvent(2, "assistant_message", { content: "first" }, "cycle-1", 2),
      engineEvent(3, "tool_call_ready", { call_id: "call-1", tool_id: "scan" }),
      engineEvent(4, "assistant_delta", { delta: "second" }, "cycle-1", 4),
      engineEvent(5, "assistant_message", { content: "second" }, "cycle-1", 5),
    ]);

    expect(projection.conversationMessages.map((message) => message.content)).toEqual([
      "first",
      "second",
    ]);
    expect(
      projection.highLevelTimeline.filter((item) => item.kind === "stream"),
    ).toHaveLength(2);
  });

  it("reconciles a final message without an id to the only active explicit stream", () => {
    const projection = reduceRunEvents([
      engineEvent(1, "assistant_delta", { item_id: "message-1", delta: "draft" }),
      engineEvent(2, "assistant_message", { content: "final" }),
    ]);

    expect(projection.conversationMessages).toHaveLength(1);
    expect(projection.conversationMessages[0]?.content).toBe("final");
  });

  it("keeps all provider tool records out of user-facing projections", () => {
    const projection = reduceRunEvents([
      engineEvent(8, "tool_call_argument_delta", {
        call_id: "call-1",
        delta: '{"target":',
      }),
      engineEvent(9, "tool_result_delta", { call_id: "call-1", delta: "partial output" }),
      engineEvent(10, "tool_call_ready", {
        call_id: "call-1",
        tool_id: "scan",
        arguments: '{"target":"127.0.0.1"}',
      }),
    ]);

    expect(projection.conversationMessages).toEqual([]);
    expect(projection.highLevelTimeline).toEqual([]);
    expect(projection.rawEvents.map((event) => event.sequence)).toEqual([8, 9, 10]);
  });

  it("routes Action-family payloads only to Raw Events", () => {
    const canary = "ACTION_EVENT_SECRET_CANARY";
    const projection = reduceRunEvents([
      runEvent(1, "agent.tool_started", {
        tool_call_intent_id: "action-1",
        command: canary,
      }),
      runEvent(2, "tool.approval_required", {
        tool_call_intent_id: "action-1",
        env_diff: { TOKEN: canary },
      }),
      runEvent(3, "execution.submitted", {
        execution_id: "execution-1",
        output: canary,
      }),
      runEvent(4, "target_http.request_started", {
        request_id: "request-1",
        url: `https://example.test/?signature=${canary}`,
      }),
      runEvent(5, "action.updated", {
        action_id: "action-1",
        summary: canary,
      }),
      runEvent(6, "run.status_changed", { from: "running", to: "waiting_tool" }),
    ]);

    expect(projection.highLevelTimeline).toEqual([
      expect.objectContaining({
        kind: "event",
        event: expect.objectContaining({ event_type: "run.status_changed" }),
      }),
    ]);
    expect(JSON.stringify(projection.highLevelTimeline)).not.toContain(canary);
    expect(projection.rawEvents.map((event) => event.sequence)).toEqual([1, 2, 3, 4, 5, 6]);
  });
});
