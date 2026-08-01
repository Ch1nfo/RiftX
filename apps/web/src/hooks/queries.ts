import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { api, RiftXAPIError } from "../api/client";
import type {
  ApprovalDecisionPayload,
  CreateRunPayload,
  CreateTerminalPayload,
  RegisterArtifactPayload,
  GenerateReportsPayload,
  NodeStatus,
  RunActionList,
  RunActionListItem,
  RunEventList,
  UpdateFindingPayload,
  UpdateToolPayload,
  RunStatus,
} from "../api/types";

export const queryKeys = {
  runs: (status?: RunStatus) => ["runs", status ?? "all"] as const,
  run: (runId: string) => ["run", runId] as const,
  events: (runId: string) => ["run-events", runId] as const,
  executions: (runId: string) => ["run-executions", runId] as const,
  findings: (runId: string) => ["run-findings", runId] as const,
  artifacts: (runId: string) => ["run-artifacts", runId] as const,
  reports: (runId: string) => ["run-reports", runId] as const,
  approvals: (runId: string) => ["run-approvals", runId] as const,
  actionRoot: (runId: string) => ["run-actions", runId] as const,
  actions: (runId: string) => ["run-actions", runId, "list"] as const,
  action: (runId: string, actionId: string) =>
    ["run-actions", runId, "detail", actionId] as const,
  terminal: (sessionId: string) => ["terminal", sessionId] as const,
  nodes: (status?: NodeStatus) => ["nodes", status ?? "all"] as const,
  tools: (nodeId: string) => ["tools", nodeId] as const,
  modelProfiles: ["model-profiles"] as const,
};

const ACTION_PAGE_SIZE = 50;
const ACTION_QUERY_RETRY_LIMIT = 1;

function retryActionQuery(failureCount: number, error: Error): boolean {
  if (error instanceof RiftXAPIError && [401, 403].includes(error.status)) {
    return false;
  }
  return failureCount < ACTION_QUERY_RETRY_LIMIT;
}

export function useNodes(status?: NodeStatus) {
  return useQuery({
    queryKey: queryKeys.nodes(status),
    queryFn: () => api.listNodes(status),
    refetchInterval: 10_000,
  });
}


export function useRuns(status?: RunStatus) {
  return useQuery({
    queryKey: queryKeys.runs(status),
    queryFn: () => api.listRuns(status),
    refetchInterval: 10_000,
  });
}

export function useModelProfiles() {
  return useQuery({
    queryKey: queryKeys.modelProfiles,
    queryFn: () => api.listModelProfiles(),
  });
}

export function useModelProfileControl(adminToken = "") {
  const queryClient = useQueryClient();
  const refresh = () =>
    void queryClient.invalidateQueries({ queryKey: queryKeys.modelProfiles });
  return {
    setDefault: useMutation({
      mutationFn: (profileName: string) =>
        api.setDefaultModelProfile(profileName, adminToken.trim()),
      onSuccess: refresh,
    }),
    remove: useMutation({
      mutationFn: (profileName: string) =>
        api.deleteModelProfile(profileName, adminToken.trim()),
      onSuccess: refresh,
    }),
  };
}

export function useRun(runId: string) {
  return useQuery({
    queryKey: queryKeys.run(runId),
    queryFn: () => api.getRun(runId),
    enabled: Boolean(runId),
  });
}

export function useRunEvents(runId: string) {
  return useQuery<RunEventList>({
    queryKey: queryKeys.events(runId),
    queryFn: () => api.listEvents(runId),
    enabled: Boolean(runId),
    // Run events are immutable and append-only. A refetch that started before
    // an SSE batch can otherwise complete later with an older snapshot and
    // erase stop acknowledgements that already advanced the stream cursor.
    structuralSharing: (previous, incoming) =>
      mergeRunEventLists(
        previous as RunEventList | undefined,
        incoming as RunEventList,
      ),
  });
}

export function useRunActions(runId: string) {
  return useInfiniteQuery({
    queryKey: queryKeys.actions(runId),
    queryFn: ({ pageParam }) =>
      api.listRunActions(runId, pageParam ?? undefined, ACTION_PAGE_SIZE),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage: RunActionList) =>
      lastPage.has_more ? lastPage.next_cursor : null,
    enabled: Boolean(runId),
    retry: retryActionQuery,
  });
}

export function useRunAction(runId: string, actionId: string) {
  return useQuery({
    queryKey: queryKeys.action(runId, actionId),
    queryFn: () => api.getRunAction(runId, actionId),
    enabled: Boolean(runId && actionId),
    retry: retryActionQuery,
  });
}

export function flattenRunActionPages(
  pages: readonly RunActionList[] | undefined,
): RunActionListItem[] {
  const seen = new Set<string>();
  const items: RunActionListItem[] = [];
  for (const page of pages ?? []) {
    for (const item of page.items) {
      const key = `${item.run_id}:${item.action_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      items.push(item);
    }
  }
  return items;
}

export function mergeRunEventLists(
  previous: RunEventList | undefined,
  incoming: RunEventList,
): RunEventList {
  if (!previous?.items.length) {
    const normalized = normalizeRunEvents(incoming.items);
    return normalized === incoming.items
      ? incoming
      : { ...incoming, items: normalized };
  }
  const afterSequence = previous
    ? Math.min(previous.after_sequence, incoming.after_sequence)
    : incoming.after_sequence;
  if (
    afterSequence === incoming.after_sequence &&
    incoming.items.length >= previous.items.length &&
    isOrderedUniqueRunEvents(incoming.items) &&
    previous.items.every((event, index) => event === incoming.items[index])
  ) {
    // HTTP/SSE structural sharing may already hand us the complete ordered
    // snapshot. Preserve it without rebuilding the high-cardinality history.
    return incoming;
  }

  const previousItems = isOrderedUniqueRunEvents(previous.items)
    ? previous.items
    : normalizeRunEvents(previous.items);
  const incomingItems = normalizeRunEvents(incoming.items);
  if (!incomingItems.length) {
    return afterSequence === previous.after_sequence && previousItems === previous.items
      ? previous
      : { after_sequence: afterSequence, items: previousItems };
  }
  if (
    previousItems.length &&
    compareRunEvents(previousItems[previousItems.length - 1]!, incomingItems[0]!) < 0
  ) {
    return {
      after_sequence: afterSequence,
      // The live path is normally append-only: O(history + batch allocation),
      // no Map and no full-history sort every 32 ms.
      items: [...previousItems, ...incomingItems],
    };
  }

  const items: RunEventList["items"] = [];
  let previousIndex = 0;
  let incomingIndex = 0;
  while (previousIndex < previousItems.length || incomingIndex < incomingItems.length) {
    const previousEvent = previousItems[previousIndex];
    const incomingEvent = incomingItems[incomingIndex];
    if (!previousEvent) {
      items.push(incomingEvent!);
      incomingIndex += 1;
      continue;
    }
    if (!incomingEvent) {
      items.push(previousEvent);
      previousIndex += 1;
      continue;
    }
    const order = compareRunEvents(previousEvent, incomingEvent);
    if (order < 0) {
      items.push(previousEvent);
      previousIndex += 1;
    } else if (order > 0) {
      items.push(incomingEvent);
      incomingIndex += 1;
    } else {
      // Run Event sequences are immutable. Preserve the cached object if a
      // reconnect or late HTTP response repeats the same durable record.
      items.push(previousEvent);
      previousIndex += 1;
      incomingIndex += 1;
    }
  }
  if (
    afterSequence === incoming.after_sequence &&
    items.length === incoming.items.length &&
    items.every((event, index) => event === incoming.items[index])
  ) {
    return incoming;
  }
  return {
    after_sequence: afterSequence,
    items,
  };
}

function normalizeRunEvents(
  source: RunEventList["items"],
): RunEventList["items"] {
  if (isOrderedUniqueRunEvents(source)) return source;
  const ordered = [...source].sort(compareRunEvents);
  const unique: RunEventList["items"] = [];
  for (const event of ordered) {
    if (runEventKey(unique[unique.length - 1]) !== runEventKey(event)) unique.push(event);
  }
  return unique;
}

function isOrderedUniqueRunEvents(source: RunEventList["items"]): boolean {
  for (let index = 1; index < source.length; index += 1) {
    if (compareRunEvents(source[index - 1]!, source[index]!) >= 0) return false;
  }
  return true;
}

function compareRunEvents(
  left: RunEventList["items"][number],
  right: RunEventList["items"][number],
): number {
  return left.run_id.localeCompare(right.run_id) || left.sequence - right.sequence;
}

function runEventKey(event: RunEventList["items"][number] | undefined): string {
  return event ? `${event.run_id}:${event.sequence}` : "";
}

export function useExecutions(runId: string) {
  return useQuery({
    queryKey: queryKeys.executions(runId),
    queryFn: () => api.listExecutions(runId),
    enabled: Boolean(runId),
  });
}

export function useFindings(runId: string) {
  return useQuery({
    queryKey: queryKeys.findings(runId),
    queryFn: () => api.listFindings(runId),
    enabled: Boolean(runId),
  });
}

export function useFindingControl(runId: string) {
  const queryClient = useQueryClient();
  return {
    update: useMutation({
      mutationFn: ({
        findingId,
        payload,
      }: {
        findingId: string;
        payload: UpdateFindingPayload;
      }) => api.updateFinding(findingId, payload),
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: queryKeys.findings(runId) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.events(runId) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.actionRoot(runId) });
      },
    }),
  };
}

export function useArtifacts(runId: string) {
  return useQuery({
    queryKey: queryKeys.artifacts(runId),
    queryFn: () => api.listArtifacts(runId),
    enabled: Boolean(runId),
  });
}

export function useArtifactControl(runId: string) {
  const queryClient = useQueryClient();
  return {
    register: useMutation({
      mutationFn: (payload: RegisterArtifactPayload) =>
        api.registerArtifact(runId, payload),
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: queryKeys.artifacts(runId) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.events(runId) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.actionRoot(runId) });
      },
    }),
  };
}

export function useReports(runId: string) {
  return useQuery({
    queryKey: queryKeys.reports(runId),
    queryFn: () => api.listReports(runId),
    enabled: Boolean(runId),
  });
}

export function useReportControl(runId: string) {
  const queryClient = useQueryClient();
  return {
    generate: useMutation({
      mutationFn: (payload?: GenerateReportsPayload) =>
        api.generateReports(runId, payload),
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: queryKeys.reports(runId) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.artifacts(runId) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.events(runId) });
      },
    }),
  };
}

export function useApprovals(runId: string) {
  return useQuery({
    queryKey: queryKeys.approvals(runId),
    queryFn: () => api.listApprovals(runId),
    enabled: Boolean(runId),
  });
}

export function useApprovalControl(runId: string) {
  const queryClient = useQueryClient();
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.approvals(runId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.events(runId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.actionRoot(runId) });
    void queryClient.invalidateQueries({ queryKey: ["runs"] });
  };
  return {
    approve: useMutation({
      mutationFn: ({
        approvalId,
        payload,
      }: {
        approvalId: string;
        payload?: ApprovalDecisionPayload;
      }) => api.approve(approvalId, payload),
      onSuccess: refresh,
    }),
    reject: useMutation({
      mutationFn: ({
        approvalId,
        payload,
      }: {
        approvalId: string;
        payload?: ApprovalDecisionPayload;
      }) => api.reject(approvalId, payload),
      onSuccess: refresh,
    }),
  };
}

export function useTerminal(sessionId: string) {
  return useQuery({
    queryKey: queryKeys.terminal(sessionId),
    queryFn: () => api.getTerminal(sessionId),
    enabled: Boolean(sessionId),
  });
}

export function useTerminalControl(runId: string) {
  const queryClient = useQueryClient();
  return {
    create: useMutation({
      mutationFn: (payload: CreateTerminalPayload = {}) => api.createTerminal(runId, payload),
      onSuccess: (terminal) => {
        queryClient.setQueryData(queryKeys.terminal(terminal.id), terminal);
        void queryClient.invalidateQueries({ queryKey: queryKeys.events(runId) });
      },
    }),
    close: useMutation({
      mutationFn: (sessionId: string) => api.closeTerminal(sessionId),
      onSuccess: (terminal) => {
        queryClient.setQueryData(queryKeys.terminal(terminal.id), terminal);
        void queryClient.invalidateQueries({ queryKey: queryKeys.events(runId) });
      },
    }),
  };
}

export function useTools(nodeId = "local") {
  return useQuery({
    queryKey: queryKeys.tools(nodeId),
    queryFn: () => api.listTools(nodeId),
  });
}

export function useToolAdminDetails(nodeId = "local", adminToken = "") {
  return useMutation({
    mutationFn: async (toolId: string) => {
      const snapshot = await api.listToolsForAdmin(nodeId, adminToken.trim());
      const tool = snapshot.tools.find((item) => item.definition.id === toolId);
      if (!tool) throw new Error(`Tool ${toolId} was not found in the administrator registry`);
      return tool.definition;
    },
  });
}

export function useCreateRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreateRunPayload) => api.createRun(payload),
    onSuccess: (run) => {
      queryClient.setQueryData(queryKeys.run(run.id), run);
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });
}

export function useRunControl(runId: string) {
  const queryClient = useQueryClient();
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.events(runId) });
    void queryClient.invalidateQueries({ queryKey: ["runs"] });
  };
  return {
    pause: useMutation({ mutationFn: () => api.pauseRun(runId), onSuccess: refresh }),
    resume: useMutation({ mutationFn: () => api.resumeRun(runId), onSuccess: refresh }),
    emergencyStop: useMutation({
      mutationFn: () => api.cancelRun(runId),
      onSuccess: refresh,
    }),
    message: useMutation({
      mutationFn: ({
        message,
        messageEventId,
      }: {
        message: string;
        messageEventId?: string;
      }) => api.appendMessage(runId, message, messageEventId),
      onSuccess: refresh,
    }),
  };
}

export function useRefreshTools(nodeId = "local", adminToken = "") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.refreshTools(nodeId, adminToken.trim()),
    onSuccess: (snapshot) => {
      queryClient.setQueryData(queryKeys.tools(nodeId), snapshot);
    },
  });
}


export function useUpdateTool(nodeId = "local", adminToken = "") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      toolId,
      payload,
    }: {
      toolId: string;
      payload: UpdateToolPayload;
    }) => api.updateTool(nodeId, toolId, payload, adminToken.trim()),
    onSuccess: (snapshot) => {
      queryClient.setQueryData(queryKeys.tools(nodeId), snapshot);
    },
  });
}
