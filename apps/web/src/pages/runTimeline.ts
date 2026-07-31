import type { RunEvent } from "../api/types";

export type TimelineItem =
  | {
      kind: "event";
      key: string;
      event: RunEvent;
      startSequence: number;
      endSequence: number;
      createdAt: string;
    }
  | {
      kind: "stream";
      key: string;
      streamType: "assistant" | "tool_arguments";
      cycleId: string;
      callId: string | null;
      content: string;
      chunkCount: number;
      endEngineSequence: number;
      startSequence: number;
      endSequence: number;
      createdAt: string;
    };

type StreamDelta = Pick<
  Extract<TimelineItem, { kind: "stream" }>,
  "streamType" | "cycleId" | "callId" | "content" | "endEngineSequence"
>;

export function coalesceTimelineEvents(events: RunEvent[]): TimelineItem[] {
  const items: TimelineItem[] = [];

  for (const event of events) {
    const delta = streamDelta(event);
    if (!delta) {
      items.push({
        kind: "event",
        key: event.id,
        event,
        startSequence: event.sequence,
        endSequence: event.sequence,
        createdAt: event.created_at,
      });
      continue;
    }

    const previous = items.at(-1);
    if (
      previous?.kind === "stream" &&
      previous.streamType === delta.streamType &&
      previous.cycleId === delta.cycleId &&
      previous.callId === delta.callId &&
      previous.endSequence + 1 === event.sequence &&
      previous.endEngineSequence + 1 === delta.endEngineSequence
    ) {
      previous.content += delta.content;
      previous.chunkCount += 1;
      previous.endSequence = event.sequence;
      previous.endEngineSequence = delta.endEngineSequence;
      previous.createdAt = event.created_at;
      continue;
    }

    if (!delta.content) continue;
    items.push({
      kind: "stream",
      key: event.id,
      ...delta,
      chunkCount: 1,
      startSequence: event.sequence,
      endSequence: event.sequence,
      createdAt: event.created_at,
    });
  }

  return items;
}

function streamDelta(event: RunEvent): StreamDelta | null {
  if (event.event_type !== "runtime.engine_event") return null;
  const engineEventType = event.payload.event_type;
  if (
    engineEventType !== "assistant_delta" &&
    engineEventType !== "tool_call_argument_delta"
  ) {
    return null;
  }

  const data = event.payload.data;
  if (
    !isRecord(data) ||
    typeof data.delta !== "string" ||
    typeof event.payload.engine_sequence !== "number"
  ) {
    return null;
  }
  if (typeof event.payload.cycle_id !== "string") return null;
  const callId =
    engineEventType === "tool_call_argument_delta" && typeof data.call_id === "string"
      ? data.call_id
      : null;
  if (engineEventType === "tool_call_argument_delta" && !callId) return null;

  return {
    streamType: engineEventType === "assistant_delta" ? "assistant" : "tool_arguments",
    cycleId: event.payload.cycle_id,
    callId,
    content: data.delta,
    endEngineSequence: event.payload.engine_sequence,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
