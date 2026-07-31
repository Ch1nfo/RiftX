import { describe, expect, it } from "vitest";

import type { RunEvent } from "../api/types";
import { coalesceTimelineEvents } from "./runTimeline";

function engineEvent(
  sequence: number,
  eventType: string,
  data: Record<string, unknown>,
  cycleId = "cycle-1",
  engineSequence = sequence,
): RunEvent {
  return {
    id: `event-${sequence}`,
    run_id: "run-1",
    sequence,
    event_type: "runtime.engine_event",
    payload: { cycle_id: cycleId, engine_sequence: engineSequence, event_type: eventType, data },
    created_at: `2026-07-31T02:35:${String(sequence).padStart(2, "0")}Z`,
  };
}

describe("coalesceTimelineEvents", () => {
  it("combines adjacent assistant deltas from the same cycle", () => {
    const items = coalesceTimelineEvents([
      engineEvent(19, "assistant_delta", { delta: "主" }),
      engineEvent(20, "assistant_delta", { delta: "代理" }),
      engineEvent(21, "assistant_delta", { delta: "。" }),
    ]);

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: "stream",
      streamType: "assistant",
      content: "主代理。",
      chunkCount: 3,
      startSequence: 19,
      endSequence: 21,
    });
  });

  it("keeps stream boundaries across cycles and non-delta events", () => {
    const items = coalesceTimelineEvents([
      engineEvent(1, "assistant_delta", { delta: "first" }),
      engineEvent(2, "run_completed", { status: "completed" }),
      engineEvent(3, "assistant_delta", { delta: "second" }),
      engineEvent(4, "assistant_delta", { delta: "third" }, "cycle-2"),
    ]);

    expect(items.map((item) => item.kind)).toEqual(["stream", "event", "stream", "stream"]);
  });

  it("does not merge interleaved cycles or non-contiguous engine sequences", () => {
    const items = coalesceTimelineEvents([
      engineEvent(1, "assistant_delta", { delta: "A1" }, "cycle-a", 1),
      engineEvent(2, "assistant_delta", { delta: "B1" }, "cycle-b", 1),
      engineEvent(3, "assistant_delta", { delta: "A2" }, "cycle-a", 2),
      engineEvent(4, "assistant_delta", { delta: "A3" }, "cycle-a", 4),
    ]);

    expect(items).toHaveLength(4);
  });

  it("combines streamed tool arguments by call id", () => {
    const items = coalesceTimelineEvents([
      engineEvent(8, "tool_call_argument_delta", { call_id: "call-1", delta: "{\"script\":" }),
      engineEvent(9, "tool_call_argument_delta", { call_id: "call-1", delta: "\"pwd\"}" }),
      engineEvent(10, "tool_call_argument_delta", { call_id: "call-2", delta: "{}" }),
    ]);

    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({
      kind: "stream",
      streamType: "tool_arguments",
      callId: "call-1",
      content: '{"script":"pwd"}',
      startSequence: 8,
      endSequence: 9,
    });
  });
});
