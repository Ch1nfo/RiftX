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
      streamType: "assistant";
      cycleId: string;
      streamId: string;
      content: string;
      chunkCount: number;
      endEngineSequence: number;
      startSequence: number;
      endSequence: number;
      createdAt: string;
    };

export interface ConversationMessage {
  key: string;
  role: "user" | "assistant";
  content: string;
  startSequence: number;
  endSequence: number;
}

export interface RunStreamProjection {
  conversationMessages: ConversationMessage[];
  highLevelTimeline: TimelineItem[];
  rawEvents: RunEvent[];
}

interface EngineEvent {
  eventType: string;
  cycleId: string | null;
  engineSequence: number | null;
  data: Record<string, unknown>;
}

interface AssistantUpdate {
  content: string;
  authoritative: boolean;
}

interface AssistantStreamState {
  timeline: Extract<TimelineItem, { kind: "stream" }>;
  conversation: ConversationMessage;
  seenEngineSequences: Set<number>;
  finalized: boolean;
}

const FINAL_ASSISTANT_EVENT_TYPES = new Set([
  "agent.message",
  "agent.assistant_message",
]);

export function reduceRunEvents(events: RunEvent[]): RunStreamProjection {
  const rawEvents = dedupeRunEvents(events);
  const highLevelTimeline: TimelineItem[] = [];
  const conversationMessages: ConversationMessage[] = [];
  const assistantStreams = new Map<string, AssistantStreamState>();
  const fallbackStreamOrdinals = new Map<string, number>();

  for (const event of rawEvents) {
    const engineEvent = parseEngineEvent(event);
    if (
      engineEvent?.eventType === "assistant_delta" ||
      engineEvent?.eventType === "assistant_message"
    ) {
      const update = assistantUpdate(engineEvent);
      if (
        engineEvent.cycleId &&
        (update || engineEvent.eventType === "assistant_message")
      ) {
        applyAssistantUpdate({
          event,
          engineEvent,
          update,
          assistantStreams,
          fallbackStreamOrdinals,
          highLevelTimeline,
          conversationMessages,
        });
      }
      continue;
    }
    if (engineEvent && isProviderToolEvent(engineEvent.eventType)) {
      continue;
    }
    if (isActionFamilyEvent(event.event_type)) {
      continue;
    }

    const finalAssistantContent = assistantEventContent(event);
    if (
      finalAssistantContent &&
      reconcileFinalAssistantEvent(event, finalAssistantContent, assistantStreams)
    ) {
      // Keep the durable final event in rawEvents, but fold it into the
      // streamed response in both user-facing projections. Transitional
      // runtimes can persist assistant deltas and an agent.message summary for
      // the same turn; rendering both would duplicate one Agent response.
      continue;
    }

    highLevelTimeline.push(eventTimelineItem(event));
    const conversation = conversationMessage(event);
    if (conversation) conversationMessages.push(conversation);
  }

  return { conversationMessages, highLevelTimeline, rawEvents };
}

function isProviderToolEvent(eventType: string): boolean {
  return eventType.startsWith("tool_call") || eventType.startsWith("tool_result");
}

function isActionFamilyEvent(eventType: string): boolean {
  return (
    eventType.startsWith("agent.tool_") ||
    eventType.startsWith("action.") ||
    eventType.startsWith("tool.") ||
    eventType.startsWith("execution.") ||
    eventType.startsWith("target_http.")
  );
}

export function dedupeRunEvents(events: RunEvent[]): RunEvent[] {
  const seenSequences = new Set<string>();
  const unique: RunEvent[] = [];
  for (const event of events) {
    const key = `${event.run_id}:${event.sequence}`;
    if (seenSequences.has(key)) continue;
    seenSequences.add(key);
    unique.push(event);
  }
  return unique.sort(
    (left, right) =>
      left.sequence - right.sequence || left.created_at.localeCompare(right.created_at),
  );
}

function applyAssistantUpdate({
  event,
  engineEvent,
  update,
  assistantStreams,
  fallbackStreamOrdinals,
  highLevelTimeline,
  conversationMessages,
}: {
  event: RunEvent;
  engineEvent: EngineEvent;
  update: AssistantUpdate | null;
  assistantStreams: Map<string, AssistantStreamState>;
  fallbackStreamOrdinals: Map<string, number>;
  highLevelTimeline: TimelineItem[];
  conversationMessages: ConversationMessage[];
}) {
  const cycleId = engineEvent.cycleId;
  if (!cycleId) return;
  const streamId = assistantStreamId({
    event,
    engineEvent,
    assistantStreams,
    fallbackStreamOrdinals,
  });
  const streamKey = `${event.run_id}:${cycleId}:${streamId}`;
  let state = assistantStreams.get(streamKey);

  if (
    state &&
    engineEvent.engineSequence !== null &&
    state.seenEngineSequences.has(engineEvent.engineSequence)
  ) {
    return;
  }

  const currentContent = state?.timeline.content ?? "";
  const nextContent = update
    ? update.authoritative
      ? update.content || currentContent
      : `${currentContent}${update.content}`
    : currentContent;
  if (!state && !nextContent) return;

  if (!state) {
    const key = `assistant:${streamKey}`;
    const engineSequence = engineEvent.engineSequence ?? event.sequence;
    const timeline: Extract<TimelineItem, { kind: "stream" }> = {
      kind: "stream",
      key,
      streamType: "assistant",
      cycleId,
      streamId,
      content: nextContent,
      chunkCount: 1,
      endEngineSequence: engineSequence,
      startSequence: event.sequence,
      endSequence: event.sequence,
      createdAt: event.created_at,
    };
    const conversation: ConversationMessage = {
      key,
      role: "assistant",
      content: nextContent,
      startSequence: event.sequence,
      endSequence: event.sequence,
    };
    state = {
      timeline,
      conversation,
      seenEngineSequences: new Set<number>(),
      finalized: false,
    };
    assistantStreams.set(streamKey, state);
    highLevelTimeline.push(timeline);
    conversationMessages.push(conversation);
  } else {
    state.timeline.content = nextContent;
    if (update) state.timeline.chunkCount += 1;
    state.timeline.endSequence = event.sequence;
    state.timeline.createdAt = event.created_at;
    state.conversation.content = nextContent;
    state.conversation.endSequence = event.sequence;
    if (engineEvent.engineSequence !== null) {
      state.timeline.endEngineSequence = Math.max(
        state.timeline.endEngineSequence,
        engineEvent.engineSequence,
      );
    }
  }

  if (engineEvent.engineSequence !== null) {
    state.seenEngineSequences.add(engineEvent.engineSequence);
  }
  if (engineEvent.eventType === "assistant_message") {
    state.finalized = true;
  }
}

function eventTimelineItem(event: RunEvent): Extract<TimelineItem, { kind: "event" }> {
  return {
    kind: "event",
    key: event.id,
    event,
    startSequence: event.sequence,
    endSequence: event.sequence,
    createdAt: event.created_at,
  };
}

function conversationMessage(event: RunEvent): ConversationMessage | null {
  if (event.event_type === "user.message_queued") {
    const content = event.payload.message;
    if (typeof content === "string" && content.trim()) {
      return {
        key: event.id,
        role: "user",
        content,
        startSequence: event.sequence,
        endSequence: event.sequence,
      };
    }
    return null;
  }
  const content = assistantEventContent(event);
  if (!content) return null;
  return {
    key: event.id,
    role: "assistant",
    content,
    startSequence: event.sequence,
    endSequence: event.sequence,
  };
}

function assistantEventContent(event: RunEvent): string | null {
  if (!FINAL_ASSISTANT_EVENT_TYPES.has(event.event_type)) return null;
  return firstString(
    event.payload.assistant_message,
    event.payload.message,
    event.payload.summary,
  )?.trim() || null;
}

function reconcileFinalAssistantEvent(
  event: RunEvent,
  content: string,
  assistantStreams: Map<string, AssistantStreamState>,
): boolean {
  const runPrefix = `${event.run_id}:`;
  const cycleId = firstString(event.payload.cycle_id, event.payload.agent_step_id);
  const cyclePrefix = cycleId ? `${runPrefix}${cycleId}:` : null;
  const runCandidates = [...assistantStreams.entries()].filter(([key]) =>
    key.startsWith(runPrefix),
  );
  const cycleCandidates = cyclePrefix
    ? runCandidates.filter(([key]) => key.startsWith(cyclePrefix))
    : [];
  // agent_step_id and runtime cycle_id are aligned in the current runtime,
  // but transitional/replayed events may not carry the same correlation ID.
  // An unmatched explicit correlation may only use Run-wide streams for
  // exact-content deduplication; treating a unique stream as authoritative in
  // that case can overwrite a completed reply from another cycle.
  const candidates = cycleCandidates.length ? cycleCandidates : runCandidates;
  if (!candidates.length) return false;

  const unfinished = candidates.filter(([, state]) => !state.finalized);
  const unfinishedExact = unfinished.filter(
    ([, state]) => state.conversation.content.trim() === content,
  );
  const exact = candidates.filter(([, state]) => state.conversation.content.trim() === content);
  const unmatchedExplicitCycle = cyclePrefix !== null && cycleCandidates.length === 0;
  const state = unmatchedExplicitCycle
    ? (unfinishedExact.at(-1)?.[1] ?? exact.at(-1)?.[1] ?? null)
    : (unfinishedExact.at(-1)?.[1] ??
      (unfinished.length === 1 ? unfinished[0][1] : null) ??
      exact.at(-1)?.[1] ??
      (cyclePrefix && candidates.length === 1 ? candidates[0][1] : null));
  if (!state) return false;

  state.timeline.content = content;
  state.timeline.chunkCount += 1;
  state.timeline.endSequence = event.sequence;
  state.timeline.createdAt = event.created_at;
  state.conversation.content = content;
  state.conversation.endSequence = event.sequence;
  state.finalized = true;
  return true;
}

function parseEngineEvent(event: RunEvent): EngineEvent | null {
  if (event.event_type !== "runtime.engine_event") return null;
  const eventType = event.payload.event_type;
  if (typeof eventType !== "string") return null;
  return {
    eventType,
    cycleId: typeof event.payload.cycle_id === "string" ? event.payload.cycle_id : null,
    engineSequence:
      typeof event.payload.engine_sequence === "number"
        ? event.payload.engine_sequence
        : null,
    data: isRecord(event.payload.data) ? event.payload.data : {},
  };
}

function assistantUpdate(event: EngineEvent): AssistantUpdate | null {
  if (event.eventType === "assistant_delta") {
    const accumulated = firstString(
      event.data.accumulated,
      event.data.accumulated_text,
      event.data.text_so_far,
    );
    if (accumulated !== null) {
      return { content: accumulated, authoritative: true };
    }
    const delta = firstString(event.data.delta);
    return delta === null ? null : { content: delta, authoritative: false };
  }

  const content = extractText(event.data);
  return content === null ? null : { content, authoritative: true };
}

function assistantStreamId({
  event,
  engineEvent,
  assistantStreams,
  fallbackStreamOrdinals,
}: {
  event: RunEvent;
  engineEvent: EngineEvent;
  assistantStreams: Map<string, AssistantStreamState>;
  fallbackStreamOrdinals: Map<string, number>;
}): string {
  const data = engineEvent.data;
  const explicit = firstString(
    data.stream_id,
    data.message_id,
    data.item_id,
    data.response_id,
    event.payload.stream_id,
    event.payload.message_id,
  );
  if (explicit) return explicit;
  const outputIndex = data.output_index;
  if (typeof outputIndex === "number") return `output-${outputIndex}`;

  const cycleKey = `${event.run_id}:${engineEvent.cycleId ?? "unknown"}`;
  const activeStreams = [...assistantStreams.entries()].filter(
    ([key, state]) => key.startsWith(`${cycleKey}:`) && !state.finalized,
  );
  if (activeStreams.length === 1) {
    return activeStreams[0][1].timeline.streamId;
  }
  let ordinal = fallbackStreamOrdinals.get(cycleKey) ?? 1;
  let streamId = `primary-${ordinal}`;
  const current = assistantStreams.get(`${cycleKey}:${streamId}`);
  if (engineEvent.eventType === "assistant_delta" && current?.finalized) {
    ordinal += 1;
    fallbackStreamOrdinals.set(cycleKey, ordinal);
    streamId = `primary-${ordinal}`;
  } else if (!fallbackStreamOrdinals.has(cycleKey)) {
    fallbackStreamOrdinals.set(cycleKey, ordinal);
  }
  return streamId;
}

function extractText(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    const parts = value
      .map((item) => extractText(item))
      .filter((item): item is string => item !== null);
    return parts.length ? parts.join("") : null;
  }
  if (!isRecord(value)) return null;
  for (const key of [
    "text",
    "output_text",
    "content",
    "message",
    "output",
    "raw_item",
    "item",
    "value",
  ]) {
    const content = extractText(value[key]);
    if (content !== null) return content;
  }
  return null;
}

function firstString(...values: unknown[]): string | null {
  return values.find((value): value is string => typeof value === "string") ?? null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
