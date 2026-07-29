import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api } from "../api/client";
import type { RunEvent, RunEventList } from "../api/types";
import { queryKeys } from "./queries";

export function useEventStream(runId: string, enabled = true) {
  const queryClient = useQueryClient();
  const lastSequence = useRef(0);

  useEffect(() => {
    if (!enabled || !runId || typeof EventSource === "undefined") {
      return undefined;
    }
    const cached = queryClient.getQueryData<RunEventList>(queryKeys.events(runId));
    lastSequence.current = Math.max(
      lastSequence.current,
      ...(cached?.items.map((item) => item.sequence) ?? [0]),
    );
    const source = new EventSource(api.eventStreamUrl(runId, lastSequence.current));
    source.onmessage = (message) => ingest(message.data);
    const knownEventTypes = [
      "run.created",
      "run.status_changed",
      "run.prepared",
      "run.pause_requested",
      "run.resume_requested",
      "workflow.started",
      "workflow.start_failed",
      "user.message_queued",
      "agent.cycle_started",
      "agent.cycle_failed",
      "agent.cycle_interrupted",
      "agent.cycle_completed",
      "agent.message",
      "agent.plan_updated",
      "agent.completion_requested",
      "agent.context_compacted",
      "agent.tool_started",
      "agent.tool_completed",
      "agent.tool_failed",
      "tool.approval_required",
      "tool.approved",
      "tool.rejected",
      "execution.cancel_requested",
      "terminal.opened",
      "terminal.resized",
      "terminal.interrupted",
      "terminal.taken_over",
      "terminal.released",
      "terminal.closed",
      "terminal.lost",
      "artifact.registered",
      "finding.created",
      "report.generation_requested",
      "run.cleaned_up",
    ];
    for (const eventType of knownEventTypes) {
      source.addEventListener(eventType, (message) => {
        ingest((message as MessageEvent<string>).data);
      });
    }

    function ingest(raw: string) {
      const event = JSON.parse(raw) as RunEvent;
      if (event.sequence <= lastSequence.current) {
        return;
      }
      lastSequence.current = event.sequence;
      queryClient.setQueryData<RunEventList>(queryKeys.events(runId), (current) => ({
        after_sequence: current?.after_sequence ?? 0,
        items: [...(current?.items ?? []), event].sort(
          (left, right) => left.sequence - right.sequence,
        ),
      }));
      void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      if (event.event_type === "finding.created") {
        void queryClient.invalidateQueries({ queryKey: queryKeys.findings(runId) });
      }
      if (event.event_type === "artifact.registered") {
        void queryClient.invalidateQueries({ queryKey: queryKeys.artifacts(runId) });
      }
      if (event.event_type.startsWith("tool.approval") || event.event_type === "tool.approved" || event.event_type === "tool.rejected") {
        void queryClient.invalidateQueries({ queryKey: queryKeys.approvals(runId) });
      }
      if (event.event_type.startsWith("terminal.")) {
        const sessionId = event.payload.session_id;
        if (typeof sessionId === "string") {
          void queryClient.invalidateQueries({ queryKey: queryKeys.terminal(sessionId) });
        }
      }
    }

    return () => source.close();
  }, [enabled, queryClient, runId]);
}
