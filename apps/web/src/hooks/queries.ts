import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type {
  ApprovalDecisionPayload,
  CreateRunPayload,
  CreateTerminalPayload,
  RegisterArtifactPayload,
  GenerateReportsPayload,
  NodeStatus,
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
  terminal: (sessionId: string) => ["terminal", sessionId] as const,
  nodes: (status?: NodeStatus) => ["nodes", status ?? "all"] as const,
  tools: (nodeId: string) => ["tools", nodeId] as const,
};

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

export function useRun(runId: string) {
  return useQuery({
    queryKey: queryKeys.run(runId),
    queryFn: () => api.getRun(runId),
    enabled: Boolean(runId),
  });
}

export function useRunEvents(runId: string) {
  return useQuery({
    queryKey: queryKeys.events(runId),
    queryFn: () => api.listEvents(runId),
    enabled: Boolean(runId),
  });
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
    cancel: useMutation({
      mutationFn: () => api.cancelCurrentExecution(runId),
      onSuccess: refresh,
    }),
    message: useMutation({
      mutationFn: (message: string) => api.appendMessage(runId, message),
      onSuccess: refresh,
    }),
  };
}

export function useRefreshTools(nodeId = "local") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.refreshTools(nodeId),
    onSuccess: (snapshot) => {
      queryClient.setQueryData(queryKeys.tools(nodeId), snapshot);
    },
  });
}


export function useUpdateTool(nodeId = "local") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      toolId,
      payload,
    }: {
      toolId: string;
      payload: UpdateToolPayload;
    }) => api.updateTool(nodeId, toolId, payload),
    onSuccess: (snapshot) => {
      queryClient.setQueryData(queryKeys.tools(nodeId), snapshot);
    },
  });
}
