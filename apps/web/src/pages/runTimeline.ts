import type { RunEvent } from "../api/types";
import { reduceRunEvents, type TimelineItem } from "./runStreamReducer";

export type { TimelineItem } from "./runStreamReducer";

export function coalesceTimelineEvents(events: RunEvent[]): TimelineItem[] {
  return reduceRunEvents(events).highLevelTimeline;
}
